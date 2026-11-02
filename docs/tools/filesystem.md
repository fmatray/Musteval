# Filesystem tools

Four tools over the two LittleFS mounts: `userfs` at `/fs` (read-only to tools) and
`wsfs` at `/workspace` (the only tool-writable location). `fs.list` and `fs.read`
are always available; `fs.write` and `fs.delete` require `tools.allow_dangerous: true`
and a target that resolves under `/workspace/`. Gate matrix:
`../06-security.md` §6.1.

| Tool | Gate | Errors |
|---|---|---|
| `fs.list` | none, both mounts listable | `E_TOOL` |
| `fs.read` | none, both mounts readable | `E_TOOL` |
| `fs.write` | `tools.allow_dangerous` + resolved path under `/workspace/` | `E_TOOL` refusal, `E_STORAGE` quota |
| `fs.delete` | `tools.allow_dangerous` + resolved path under `/workspace/` | `E_TOOL` refusal, `E_STORAGE` quota or directory target |

Paths are agent-visible: `/workspace/...` maps unchanged onto the `wsfs` mount, any
other path maps to `/fs/...` on `userfs` (`../03-config.md` §1). Nothing outside
those two mounts is reachable, and NVS has no mount point.

## `fs.list`

Lists one directory on either mount. `path` defaults to the root of the agent
namespace (`/`), which is `userfs`. Listing `/workspace` or any directory beneath it
returns the `wsfs` tree. Entries are one level deep; the call does not recurse.
`dir` is `true` for a subdirectory and `false` for a file.

**Arguments**

| name | type | required | bounds |
|---|---|---|---|
| `path` | string | no | agent-visible absolute path inside `/fs` or `/workspace` |

**Result**

```json
{
  "entries": [
    {"name": "notes.md", "size": 412, "dir": false},
    {"name": "scratch", "size": 0, "dir": true}
  ]
}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"path":{"type":"string"}}}
```

## `fs.read`

Reads a file from either mount. Reading is not gated by workspace confinement: the
agent may read its own prompts, skills and `/tools/*.json` under `/fs`, which is
intentional (`../06-security.md` §6.1). `/config.json` holds only `nvs:<key>` secret
references, so reading it cannot disclose a secret (`../06-security.md` §7).

`offset` and `limit` are optional window selectors. `[ASSUMPTION]` `offset` is a byte
offset from the start of the file and `limit` is a maximum byte count, defaulting to
the whole remainder of the file. `[ASSUMPTION]` A result larger than the shared
tool-result cap is truncated before it reaches the model.

**Arguments**

| name | type | required | bounds |
|---|---|---|---|
| `path` | string | yes | agent-visible absolute path inside `/fs` or `/workspace` |
| `offset` | integer | no | byte offset, 0 or greater |
| `limit` | integer | no | maximum bytes to return, 1 or greater |

**Result**

```json
{
  "data": "line one\nline two\n"
}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"path":{"type":"string"},"offset":{"type":"integer"},"limit":{"type":"integer"}},"required":["path"]}
```

## `fs.write`

Writes one file under `/workspace/`, replacing the whole file. Writing requires
`tools.allow_dangerous: true` and a path accepted by `store_resolve_write_path()`;
either failing yields `E_TOOL` with no bytes written. Overwriting an existing file is
allowed and counts only the new size against the quota, so a file can shrink.

The quota is checked before the target is truncated: a write that would push
`/workspace/` past its total-bytes, file-count or per-file limit returns `E_STORAGE`
and leaves the existing file untouched (`../06-security.md` §6.2).

`/config.json` and `/tasks.json` are never writable through `fs.write`. They live on
`userfs` and have their own entry points (`config.set`, `task.set`); see
`../02-firmware.md` §7.2.

**Arguments**

| name | type | required | bounds |
|---|---|---|---|
| `path` | string | yes | resolved under `/workspace/` with at least one further segment |
| `data` | string | yes | file contents; final size within the per-file cap |

**Result**

```json
{
  "bytes_written": 21
}
```

`bytes_written` is the byte count of the file contents written.

**Schema sent to the model**

```json
{"type":"object","properties":{"path":{"type":"string"},"data":{"type":"string"}},"required":["path","data"]}
```

## `fs.delete`

Removes one file under `/workspace/`. Same double gate as `fs.write`. Deleting is not
recursive and a directory target is refused with `E_STORAGE` rather than unlinking
its contents. Quota bookkeeping is recomputed from the directory state after a
successful unlink, so a delete frees exactly what was on disk and a failed unlink
changes nothing (`../06-security.md` §6.2).

**Arguments**

| name | type | required | bounds |
|---|---|---|---|
| `path` | string | yes | resolved under `/workspace/`; must name a file, not a directory |

**Result**

```json
{
  "ok": true
}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}
```

## Write path resolution

`fs.write` and `fs.delete` share one resolver, `store_resolve_write_path()`
(`../02-firmware.md` §2, `store`). Refusal happens at resolution time, in this order,
before any open or unlink:

1. Reject an empty path, and reject any path that does not start with `/`.
2. Normalise: split on `/`, drop empty and `.` segments, and process `..` by popping
   one segment. A `..` that would pop above the root is a rejection, not a clamp.
3. Rebuild the canonical path and require the literal prefix `/workspace/` followed
   by at least one further segment. `/workspace/` alone is not a file target.
4. Reject any segment containing a NUL byte, and reject a path longer than the
   LittleFS limits cited in `../03-config.md`.

Because the check is a canonicalisation followed by a prefix requirement, it is not
string prefix matching: `workspace/../config.json`, `../workspace/x.txt`,
`/workspace/../config.json`, `/fs/config.json` and absolute paths into `userfs` all
fail with `E_TOOL` regardless of spelling. Mount normalisation is a second barrier:
`/workspace` is its own mount root, so `..` above it has nowhere to go.

## Errors

- `E_TOOL`: `fs.write` or `fs.delete` called while `tools.allow_dangerous` is
  `false`; write or delete target rejected by the resolver (empty, not absolute,
  escaping `..`, NUL, over-long, outside `/workspace/`); any runtime failure that is
  not a quota condition, including a `fs.list` or `fs.read` path that does not exist.
- `E_STORAGE`: a write whose final size would exceed the total-bytes, file-count or
  per-file quota under `/workspace/`, refused before truncation; a `fs.delete`
  target that is a directory.
- `E_INTERNAL`: `[ASSUMPTION]` unexpected firmware fault surfaced through the tool
  wrapper.

Confinement refusals are `E_TOOL`; quota refusals are `E_STORAGE`. The two are not
interchangeable (`../06-security.md` §6).

Refusals arrive as `agent.tool_result` with `ok: false` and never abort the run
(`../04-protocol.md` §3.13). Every call itself appears as an `agent.tool` frame
(`../04-protocol.md` §3.12).

## See also

- [Partition table and mount behaviour](../01-hardware.md#4-flash-partitions)
- [Storage map and agent-visible paths](../03-config.md#1-storage-map)
- [Workspace confinement](../06-security.md#61-workspace-confinement)
- [Workspace quota](../06-security.md#62-workspace-quota)
- [`store` module API](../02-firmware.md)
- [`agent.tool` and `agent.tool_result` frames](../04-protocol.md)
- [Tool surface index](README.md)
