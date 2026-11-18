---
name: memory-hygiene
description: Store durable facts in memory.* instead of files, within the key and size bounds.
version: 1.0.0
keywords: [memory, persist, kv, nvs, recall]
tools: [memory.get, memory.set, memory.list, memory.delete]
max_body_bytes: 4096
---

# Durable memory

`memory.*` is where one fact survives across runs. It is NVS, not a filesystem:
`fs.list` cannot see it, no path reaches it, and no value written there can become a
config value or a secret.

## Which place for which fact

| The fact | Where |
|---|---|
| Something to recall in a later run ("the user's timezone") | `memory.set` |
| A file the user will open, or task output | `/workspace/` via `fs.write` |
| Progress through the current request | `todo` |
| Something that must happen later on its own | `task.set` |
| A credential | Never you — the user runs `secret.set` |

Memory is not a scratch pad for the current run. If you will not need it after this
answer, do not store it; a namespace that fills with run-local notes is a namespace
whose entries no one trusts.

## Key rules

- 1 to 15 bytes, `[A-Za-z0-9_]` only. No hyphens, no dots, no spaces.
- Matched byte-exactly and case-sensitive, so `User` and `user` are two entries.
  Pick one casing convention and keep it — usually lowercase with underscores.
- These prefixes are reserved and refused with `E_TOOL`: `sys_`, `cfg_`,
  `secret_`, `rt_`. Nothing is written when one is refused.
- At most 32 keys. At the 32nd, `memory.list` shows the keys; delete one with
  `memory.delete`, then retry.

## Size rules

- One value is at most 512 UTF-8 bytes. JSON escaping happens after the bound, so
  the result stays under the 4 KiB tool-result cap.
- The namespace total is `tools.memory_max_bytes` (256..4096, default 2048).
- Overwriting counts only the new length, so a shorter rewrite can fit while the
  store is full, and rewriting shrinks the total.
- Breaching any bound is `E_TOOL`, and nothing is committed. A refused write leaves
  the previous value in place, so a failed `memory.set` never erases what it was
  going to replace.

## Reading and writing

- `memory.get` on a missing key is `E_TOOL`, not an empty string — an empty string
  is a legal stored value, so the two must stay distinguishable. Call
  `memory.list` when you are unsure whether a key exists.
- `memory.set` returns `stored: true` on success; `bytes` is the UTF-8 length.
- `memory.list` returns keys with value lengths, never the values. Use it to decide
  what to read or replace without loading anything.
- `memory.delete` on an absent key is a success with `deleted: false`. It is safe to
  call without a preceding list, and `deleted` tells you whether anything happened.

`E_STORAGE` is the one failure you cannot fix: an NVS fault. Do not retry it; report
it. Everything else is `E_TOOL` and is a call you can correct.

## Hygiene

- Store the fact, not the conversation that produced it. A 500-byte transcript under
  one key is the usual cause of a full namespace.
- One key, one fact. `prefs` holding three unrelated things cannot be updated
  without rewriting all three.
- Delete what you replaced. Nothing expires on its own; the store only shrinks when
  you call `memory.delete` or the user erases flash.
