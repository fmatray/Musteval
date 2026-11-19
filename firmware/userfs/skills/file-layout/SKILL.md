---
name: file-layout
description: Which path is writable, the workspace quota, and how to page or stage a file.
metadata:
  version: "1.0.0"
  keywords: "filesystem workspace quota staging paths"
allowed-tools: fs.list fs.read fs.write fs.delete
---

# Where files live

Two mounts, and the split is the rule:

| Path | Mount | Your access |
|---|---|---|
| `/workspace/...` | `wsfs` | read, write, delete — the only writable tree |
| `/skills/`, `/prompts/`, `/tools/`, `/config.json`, `/tasks.json` | `userfs` | read only |

Any path not under `/workspace/` resolves to `userfs`. That is why `fs.write` to
`/skills/x.md` is refused: not by a special case, but because it does not resolve
under `/workspace/`.

`/config.json` is not even readable (`E_TOOL`): it names the provider base URL and
the shape of every secret reference. No tool exposes its contents, so a value you
need has to come from the user or from what the system prompt already told you.

## Confinement is canonical, not a string prefix

`fs.write` and `fs.delete` run one resolver: normalise, then require the result to
be under `/workspace/`. So `workspace/x.txt` (no leading slash), `/workspace/../config.json`,
`../workspace/x.txt` and `/fs/config.json` all fail with `E_TOOL`. Spelling a path
creatively does not get you out, and none of these are worth retrying.

## The quota

| Limit | Value | On breach |
|---|---|---|
| Total under `/workspace/` | 64 KiB | `E_STORAGE` |
| File count | 64 | `E_STORAGE` |
| One file | 16 KiB | `E_STORAGE` |

Checked before the target is truncated, so a refused write leaves the existing file
untouched. Overwriting counts the new size only, so a file can shrink. Freeing space:
`fs.delete` frees exactly what was on disk, recomputed from the directory rather
than decremented.

`E_STORAGE` here is a quota refusal, not a flash fault. The fix is to delete
something with `fs.delete` and retry — unlike a genuine `E_STORAGE` from a failed
unlink, which changes nothing and is not your call to retry blindly.

## Reading a file larger than one window

`fs.read` returns at most 2048 bytes (`limit`, max 2048). `bytes` is the file's real
size and `truncated` says whether you got a prefix.

- Pass `offset` to read the next window. The device starts at the next character
  boundary, so page by the length of `content` you received, not by `limit`.
- `offset` must be below `bytes`; beyond it is `E_TOOL` — you have `bytes`, so this
  is a call to correct, not a fault.
- Never describe a truncated read as the whole file. If `truncated` is `true`, say
  you read a prefix, or page until you have the end.

## Writing a file

`fs.write` replaces the whole file. There is no append and no partial update: read,
modify in your head, write the whole thing back. `bytes` is what was written and
`created` distinguishes a new file from a replaced one.

Both `fs.write` and `fs.delete` require `tools.allow_dangerous: true`. The refusal
is `E_TOOL` and is terminal — report that writes are disabled and stop.

`fs.delete` without `recursive` refuses a non-empty directory with `E_TOOL`; an
empty one is removed. With `recursive: true` the whole tree goes and `entries` says
how many dirents were removed.

## Importing a file you cannot write

Skills, prompts and declarative tools reach the device only through the bridge's
`/fs put`, which stages chunks and renames the whole file on the final chunk. You
cannot call it, and `fs.write` cannot reach `/skills/`. If the user wants a new
skill installed, tell them to use `/fs put` from the bridge — do not try to write one
and do not write a copy into `/workspace/` as a substitute.
