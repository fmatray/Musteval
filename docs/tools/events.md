# Events tools

One tool, `event.emit`, lets the model raise a named application event. An emitted
event has two destinations and satisfies both on the same call: the scheduler, which
runs every enabled task whose `event` trigger names it, and the bridge, which receives
an `event` frame with `kind: "agent.event"`.

| Tool | Gate | Errors |
|---|---|---|
| `event.emit` | none | `E_TOOL` on schema failure |

Gate matrix: `../06-security.md` §6.

## `event.emit`

Raises an event of type `<type>` carrying the optional `data` string. The tool is
stateless: nothing is stored between calls, and repeated emission of the same `type`
is a fresh event each time.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `type` | string | yes | 1..32 chars, `[a-z0-9._-]` |
| `data` | string | no | 0..512 chars |

The `type` charset is the same one the `event` trigger's `name` uses
(`../02-firmware.md` §5.5), so any emitted type is a possible trigger name. A `type`
that is empty, longer than 32 characters, or contains a character outside
`[a-z0-9._-]` is refused as `E_TOOL` before the event exists.

**Result**

```json
{"ok":true,"task_hits":2}
```

`task_hits` is the integer count of enabled tasks that the emission fired. `ok: true`
means the scheduler ran and the `agent.event` frame was handed to the link; it is not
a claim that anything is listening on the other side of the USB port.

**Schema sent to the model**

```json
{"type":"object","properties":{"type":{"type":"string"},"data":{"type":"string"}},"required":["type"]}
```

The model-facing subset is `type: "object"`, `properties`, `required`, plus `enum`
for a closed value set, nothing else (`../02-firmware.md` §7.1). Neither `pattern` nor
`maxLength` is in it, so the `[a-z0-9._-]` charset and the 32/512 length bounds are
prose here and enforced by the tool, not by the schema. `type` is not an `enum`: the
value set is open.

## Delivery

Both paths run before the result is returned.

1. **Scheduler.** Every enabled task whose trigger is
   `{"type":"event","name":<type>}` with a `name` equal to the emitted `type` is
   fired. Matching is exact and case-sensitive; a task is considered once, and the
   firing path is the same serialized path as any other trigger
   (`../02-firmware.md` §5.5). A `type` that matches no task fires nothing, is not an
   error, and still produces the frame below.
2. **Bridge.** The device emits one `event` frame (`../04-protocol.md` §3.19):

   ```json
   {"v":1,"type":"event","id":"d-21","ts":1730571021,"kind":"agent.event","data":{"type":"led.changed","data":"gpio21=1","task_hits":2}}
   ```

   `data.type` repeats the argument, `data.data` repeats the optional string, and
   `data.task_hits` is the same integer the tool result reports. The frame is emitted
   even when `task_hits` is `0`, so the bridge sees every emission the model makes.

Emission is not a way to request work: the tool does not wait for the fired tasks, and
their runs queue behind the current one like any other trigger (`../02-firmware.md`
§3.3). An `event` task that calls `event.emit` again therefore re-enters through the
scheduler queue rather than recursively, and the queue depth bounds it.

`event.emit` performs no I/O of its own and needs no broker, clock or network: it is
available on every run, including one started before WiFi associates.

## Errors

- `E_TOOL` — `type` missing, empty, over 32 chars, or outside `[a-z0-9._-]`; `data`
  over 512 chars; or any unknown property. The call is rejected without execution and
  no frame is emitted.
- A schema-valid call has no other failure mode. There is no storage or network step
  to fail, so `E_STORAGE`, `E_GEO` and `E_LLM_UNAVAILABLE` never come from this tool.

## See also

- `../00-overview.md` §5 — frozen trigger enum, including the `event` member
- `../00-overview.md#5-frozen-trigger-enum` — the canonical nine-value list
- `../02-firmware.md` §5.5 — `event` trigger matching and firing
- `../04-protocol.md` §3.19 — `event` frame (device → bridge)
- `../03-config.md` §tasks — trigger schema for a task that listens to an event
