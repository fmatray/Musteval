# Memory tools

Durable key/value memory for the agent, held in NVS namespace `memory` and never in
LittleFS. Four tools: `memory.get`, `memory.set`, `memory.list`, `memory.delete`.
They give the model a place to remember one fact across runs without writing scratch
files into `/workspace/`, where entries would pollute task output, count against the
64 KiB workspace quota (`../06-security.md` §6.2) and have no per-key delete story.
No filesystem tool can enumerate or read NVS, so a stored entry is invisible to
`fs.list` and unreachable by any path (`../03-config.md` §1).

None of the four carries a gate. Bounds are refusals, not gates: an argument the
model got wrong is `E_TOOL`, a capacity bound the model can clear by deleting an
entry is also `E_TOOL`, and `E_STORAGE` is reserved for an actual NVS read/write
fault. Gate matrix: `../06-security.md` §6.

## Tools

| Tool | Gate | Errors |
|---|---|---|
| `memory.get` | none | `E_TOOL`, `E_STORAGE` |
| `memory.set` | none | `E_TOOL`, `E_STORAGE` |
| `memory.list` | none | `E_TOOL`, `E_STORAGE` |
| `memory.delete` | none | `E_TOOL`, `E_STORAGE` |

## Storage model

One NVS namespace, `memory`, on the `nvs` partition (`../01-hardware.md` §4). That
partition is `0x6000` = 24 KiB of 4 KiB pages and also holds `secrets`, `runtime` and
`sys`, so the budget below is a share of it, not a partition of its own. NVS is not
a mount point: there is no path to it, so `store_map_path()` and the workspace
resolver (`../02-firmware.md` §2 `store`) do not apply and no `..` or absolute-path
input has anything to reach.

| Limit | Value | On breach |
|---|---|---|
| Keys in the namespace | 32 | `E_TOOL` |
| Key | 1–15 bytes, `[A-Za-z0-9_]` | `E_TOOL` |
| Single value | 512 UTF-8 bytes | `E_TOOL` |
| Total value bytes | `tools.memory_max_bytes` (256..4096, default 2048) | `E_TOOL` |
| Reserved key prefixes | `sys_`, `cfg_`, `secret_`, `rt_` | `E_TOOL` |

The total is the config key, not a constant: `../03-config.md` §tools owns it, and
the ceiling of that range is the namespace's share of NVS. Breaching it is `E_TOOL`
rather than `E_STORAGE` because the model must `memory.delete` before it can retry —
it is a call the model can fix, not a flash fault.

`[ASSUMPTION]` The three numeric limits are policy, not NVS ceilings: the default
2 KiB of payload across at most 32 entries is a small share of the 24 KiB partition
and leaves the other three namespaces the rest. Only the total is user-tunable, up
to 4096; the per-value and key-count bounds are constants. They are cheap to raise
after a bench measurement and cannot be raised by the model at runtime.

The 15-byte key length and the `[A-Za-z0-9_]` alphabet are the NVS key limits that
also bind `secrets` (`../06-security.md` §8), stated once here so both namespaces
agree. A key is matched byte-exactly and is case-sensitive, so `User` and `user` are
two entries.

The reserved prefixes are a forward reservation inside the `memory` namespace, not a
reference to the other namespaces, which are already unreachable from this tool. A
later firmware addition that keeps model memory in NVS needs a key space the model
cannot pre-empt, and refusing those prefixes now means it can take one without
migrating stored entries. A colliding key is refused with `E_TOOL` and nothing is
written; an existing key with a reserved prefix is never created by this tool, so
there is nothing to preserve.

Overwrite counts only the new value's length against the total, so raising a bound
and rewriting values can shrink the store, and a value can be replaced by a shorter
one while the store is full. The total is recomputed by summing the namespace rather
than decremented per write, for the same reason the workspace quota is
(`../06-security.md` §6.2): a failed commit must not drift the accounting.

**Why `memory.set` and `memory.delete` are not gated.** Each write lands in one
namespace the model already owns, is bounded to 4 KiB of payload, and can reach no
path, no config key, no task, no prompt and no secret: `cfg_resolve_secret()` reads
only namespace `secrets` (`../06-security.md` §8), so a `memory` entry can never be
promoted into a `nvs:` reference and no value written here is ever handed to `net`,
`llm` or `mcp`. The blast radius is the agent's own bounded scratch memory plus NVS
wear, and both entry and eviction are one call away (`memory.list`, `memory.delete`),
so a wrong write is recoverable in the same run. Requiring `tools.allow_dangerous`
for it would gate a convenience store at the same level as `gpio.write`, which is
actuation, and `fs.write`, which writes a filesystem.

**RAM.** No value is held resident between calls. Each call resolves exactly one
entry through the `store` NVS accessors into a per-call buffer, so the four tools add
no static or persistent allocation and a stored value costs heap only for the
duration of its own call. `memory.list` reads key metadata only and never loads a
value, which is why listing a full namespace stays far smaller than reading it.

Results are bounded so they cannot exceed the 4 KiB tool-result discipline. The worst
`memory.get` result is one 512-byte value; JSON escaping can expand a byte to at most
`\u00XX` (six characters), so the serialised value is at most 3072 bytes and the
whole result stays under 4096 with no truncation and no `truncated` flag. The worst
`memory.list` result is 32 entries of a 15-byte key and a byte count, about 1 KiB.
Unlike `fs.read`, neither tool can emit a partial payload.

The store survives `reload` and reboot, because NVS is not RAM. Values are erased by
`memory.delete` for one key, or by `idf.py erase-flash` for everything. Values are
model-authored, so nothing here is redacted: no resolved secret can reach the model
to be stored in the first place (`../06-security.md` §7).

## `memory.get`

Reads one entry and returns its value. A value is whatever the model last stored
under that key, including the empty string. A key that is absent is `E_TOOL` rather
than `{"value": ""}`, because an empty string is a legal stored value and returning
it for a missing key would make "absent" and "stored empty" indistinguishable.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `key` | string | yes | 1–15 bytes, `[A-Za-z0-9_]`, not a reserved prefix |

**Result**

```json
{"key": "user_timezone", "value": "Europe/Paris"}
```

`value` is the stored string, unchanged.

**Schema sent to the model**

```json
{"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
```

## `memory.set`

Stores one value under one key, replacing any existing value for that key. Writes are
whole-value: there is no append, no partial update and no merge, so a shorter
argument shortens the entry and the store can shrink.

A refused write leaves the previous value in place. Validation, the reserved-prefix
check, the per-value bound, the key-count bound and the total-bytes projection all
run before the commit, so an over-quota call cannot erase what it was going to
replace.

`bytes` is the UTF-8 byte length of `value`, matching `fs.write`'s `bytes_written`
rather than the `length` that `secret.set` reports. `stored` is always `true` on a
successful call, so the model can confirm the write without inspecting `ok`.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `key` | string | yes | 1–15 bytes, `[A-Za-z0-9_]`, not a reserved prefix |
| `value` | string | yes | 0–512 UTF-8 bytes, no NUL |

**Result**

```json
{"key": "user_timezone", "stored": true, "bytes": 12}
```

**Schema sent to the model**

```json
{"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}
```

## `memory.list`

Lists every key in the namespace with its value length. Order is not specified: NVS
iteration order follows the entry layout on flash, not insertion, and no sort is
applied. `bytes` is the UTF-8 byte length of the stored value, which is enough for the
model to decide what to read or replace without loading any value.

An empty namespace returns `{"keys": []}` and is a success, not an error.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| — | — | — | The schema declares no properties. |

**Result**

```json
{"keys": [{"key": "user_timezone", "bytes": 12}, {"key": "boot_note", "bytes": 0}]}
```

**Schema sent to the model**

```json
{"type": "object", "properties": {}}
```

## `memory.delete`

Removes one key. Deleting an absent key is a success with `deleted: false`, not an
error: the caller's intent is satisfied, and a model that lost track of the key set
can clear a key without a preceding `memory.list`. `deleted` distinguishes the two
outcomes so the model does not report a removal that never happened.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `key` | string | yes | 1–15 bytes, `[A-Za-z0-9_]`, not a reserved prefix |

**Result**

```json
{"key": "user_timezone", "deleted": true}
```

**Schema sent to the model**

```json
{"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
```

## Errors

`E_TOOL` covers every argument the model got wrong:

- `memory.get`, `memory.set` or `memory.delete` with a missing `key`, a key longer
  than 15 bytes, an empty key, or a key containing a byte outside `[A-Za-z0-9_]`.
- A key with a reserved prefix: `sys_`, `cfg_`, `secret_`, `rt_`. The message names
  the prefix and says the key space is firmware-reserved.
- `memory.get` on a key that is not in the namespace. The message says the key is
  absent and names `memory.list`, so the model can re-read the key set and retry
  rather than guess spellings.
- `memory.set` with a `value` that is not a JSON string, or that contains a NUL.

`E_TOOL` also covers every capacity bound the model can clear itself:

- `memory.set` with a value longer than 512 UTF-8 bytes, or that would push the
  namespace past `tools.memory_max_bytes` total value bytes, or into a 33rd key.
  Projection happens before the commit and the previous value survives. These are
  `E_TOOL` because `memory.delete` is the fix, so the call is retryable without
  operator action.

`E_STORAGE` is reserved for the one failure the model cannot fix:

- An NVS read, write or erase fault on the `memory` namespace.

Every refusal leaves the namespace unchanged. The refusal travels as `ok:false` with
the code in `agent.tool_result` (`../04-protocol.md` §3.13) and never aborts the run:
the model can call `memory.list` and retry. A wrong argument or an over-cap write is
`E_TOOL`; a flash fault is `E_STORAGE`, and the two are not interchangeable
(`../06-security.md` §6).

## See also

- [Frozen tool surface](../00-overview.md#7-frozen-tool-surface) -- the twenty-five
  built-ins, including these four names.
- [Authorization model](../06-security.md#6-authorization-model) -- the sole gate
  authority; none of these tools is gated.
- [NVS secret handling](../06-security.md#8-nvs-secret-handling) -- why `memory` keys
  can never become a `nvs:` reference.
- [Flash partitions](../01-hardware.md#4-flash-partitions) -- the 24 KiB `nvs`
  partition shared by four namespaces.
- [Storage map](../03-config.md#1-storage-map) -- LittleFS paths and NVS namespaces,
  and why neither reaches the other.
- [Filesystem tools](filesystem.md) -- the `/workspace/` store that `memory` replaces
  for recall-oriented writes, and the quota it counts against.
- [Todo tools](todo.md) -- the RAM-only counterpart: plan state that must not outlive
  the run.
