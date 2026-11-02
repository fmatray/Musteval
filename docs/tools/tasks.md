# Tasks tools

Two tools over `/tasks.json`: `task.list` reads the schedule, `task.set` authors
one task. `task.set` requires `tools.allow_dangerous: true` and is bounded by
`tools.task_max`; neither tool can delete a task. Gate matrix:
`../06-security.md` §6.

| Tool | Gate | Errors |
|---|---|---|
| `task.list` | none | `E_TOOL` |
| `task.set` | `tools.allow_dangerous` + `tools.task_max` | `E_TOOL` refusal or schema, `E_STORAGE` write failure |

## `task.list`

Returns the tasks currently in `/tasks.json`. Read of `userfs`, so no gate.
Runtime counters (e.g. `fired_count`) live in NVS namespace `runtime` keyed by
task id and are never written into the file (`../03-config.md` §tasks).

`[ASSUMPTION]` each entry in the result is the stored task object as it appears
in `/tasks.json`. The corresponding `tasks.list` command in the protocol returns
an enriched view including runtime state (`../04-protocol.md` §4.4).

**Arguments**

None. The schema is an object with no properties.

**Result**

```json
{
  "tasks": [
    {"id": "nightly-digest", "enabled": true, "description": "Summarize the day at 22:00 local time", "trigger": {"type": "schedule", "kind": "at", "at": "22:00:00"}, "repeat": {"mode": "forever", "count": 0}, "missed": "run_once"},
    {"id": "button-press", "enabled": false, "description": "Button on GPIO9 pulled low", "trigger": {"type": "gpio", "pin": 9, "level": 0}, "repeat": {"mode": "forever", "count": 0}, "missed": "skip"}
  ]
}
```

**Schema sent to the model**

```json
{"type":"object","properties":{}}
```

## `task.set`

Creates or replaces one task and writes `/tasks.json`. It validates the input
against the task schema in `../03-config.md` §tasks and refuses if the resulting
count would exceed `tools.task_max`. Result returns the task id.

The tool is add/update only. A task object carrying any key outside the schema is
rejected whole as `E_TOOL` and the file is never partially updated.

`store_task_write()` writes the whole `/fs/tasks.json` atomically by writing to
a temp file then renaming; this is held under a mutex and refused during `reload`
(`../02-firmware.md` §7.2).

**Arguments**

| name | type | required | bounds |
|---|---|---|---|
 | `task` | object | yes | one `/tasks.json` entry; closed schema, `id` `[a-z0-9_-]{1,15}` |

**Result**

```json
{"id": "nightly-digest"}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"task":{"type":"object"}},"required":["task"]}
```

Note: `task.set` and `todo` are the two nested built-in schemas; the `task` object
mirrors `/tasks.json` verbatim (`../02-firmware.md` §7.1).

## Trigger values

 The `trigger.type` is exactly one of the nine values in `../00-overview.md` §5.

| `trigger.type` | Extra fields | Notes |
|---|---|---|
| `boot` | none | fires once after boot and config validation |
| `wifi_connected` | none | fires on every transition to connected |
| `ntp_synced` | none | fires on first successful sync after boot |
| `serial_connected` | none | fires when bridge link opens |
| `gpio` | `pin` int, `level` int | fires when GPIO reaches `level` |
| `mqtt_message` | `server_id`, `topic`, `payload_contains`, `cooldown_s` | topic MUST be covered by server's `topics[]` allowlist |
| `schedule` | `kind` `at`\|`every`, `interval_s`, `first_at` | see below |
| `manual` | none | only via `tasks.run` command (`../04-protocol.md` §4.5) |

 | `event` | `name` string | fires when `event.emit` names it (§5.5 of `../02-firmware.md`) |
`schedule` triggers use `trigger.kind` as a selector:

- `kind=at`: requires `at` (ISO-8601 instant or 'HH:MM:SS' daily). Requires NTP.
- `kind=every`: requires `interval_s` (60..604800); optional `first_at` (Unix epoch).

## Authority split

The agent can add, modify, enable, and disable tasks. It cannot delete. There
is no delete operation in the tool and no field that reads as one; `enabled: false`
retains the entry. Deletion is host-only via the `tasks.delete` command.

| Caller | Origin | Gate | Cap | Add | Update | Enable/disable | Delete |
|---|---|---|---|---|---|---|---|
| `task.set` tool | model | `tools.allow_dangerous` | `tools.task_max` | yes | yes | yes | no |
| `tasks.set` command | bridge | none | schema 16-entry cap | yes | yes | yes | no |
| `tasks.delete` command | bridge | none | schema 16-entry cap | no | no | no | yes |

Write paths are identical for all three, sharing one atomic path to flash
(`../02-firmware.md` §7.2). `tasks.delete` drops the id's keys from NVS
namespace `runtime` before the rename commits (`../06-security.md` §6.3).

Replacement is total: `enabled` defaults to `true` when omitted, so a rewrite
without it re-enables a disabled task (`../04-protocol.md` §4.6).

## Errors

 - `E_TOOL` — `tools.allow_dangerous` is `false`; resulting task count exceeds
   `tools.task_max`; or argument fails schema validation (unknown key, bad id,
   bad trigger).
 - `E_STORAGE` — `[ASSUMPTION]` the atomic write to `/fs/tasks.json` failed; file
   left as before.
 - `E_BUSY` — `store_task_write()` is refused when `reload` is in progress.
   This is the only failure of `task.set` that is neither `E_TOOL` nor `E_STORAGE`.
## See also

- [Task schema](../03-config.md#task-schema)
- [Trigger schema](../03-config.md#trigger-schema-frozen-enum)
- [Schedule sub-schema](../03-config.md#schedule-sub-schema)
- [Action schema](../03-config.md#action-schema)
- [tools config: task_max](../03-config.md#tools)
- [`task.set` and `store_task_write()`](../02-firmware.md) §7.2
- [`store_task_write()` carve-out](../06-security.md) §6.3
- [Command catalog: `tasks.set` and `tasks.delete`](../04-protocol.md) §4.6, §4.7
- [Tool surface index](README.md)
