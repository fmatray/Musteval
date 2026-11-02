# Todo tools

Heap-only agent checklist. The model records its own plan and progress, and nothing
about it touches flash. `todo` has no gate and no config key: it is available in
every run (`../06-security.md` §6). Bounds are refusals, not gates.

| Tool | Gate | Errors |
|------|------|--------|
| `todo` | none -- always available | `E_TOOL` on validation failure or bound breach |

## `todo`

The params object is a **single op at its top level** -- `op` plus that op's fields,
with no `args` wrapper and no `ops` array. One call, one operation, one returned list.

The checklist lives in RAM only. It never reaches LittleFS, and it is cleared by
`reload` (`../04-protocol.md` §4.2) and by reboot. No frame carries the list: the
bridge's `/todo` re-renders only the last `todo` tool result it observed
(`../05-bridge.md` §7), so a fresh bridge session shows nothing until the device
calls `todo` again (`../02-firmware.md` §2 `agent`).

**Operations**

| Op | Targets | Effect |
|----|---------|--------|
| `init` | whole list | Replaces the list, from `list` or from flat `items` plus optional `phase`. `list: []` clears everything. |
| `start` | one task (required) | Marks that task `in_progress`; any other `in_progress` task is demoted to `pending`, so exactly one task is ever active. |
| `done` | task, phase, or all | Marks targets `completed`. |
| `drop` | task, phase, or all | Marks targets `abandoned`. |
| `rm` | task, phase, or all | Removes targets from the list. |
| `block` | task or phase (required) | Marks actionable targets `blocked`, storing `reason`. `completed` and `abandoned` are untouched. |
| `unblock` | task or phase (required) | Turns `blocked` targets back to `pending` and clears the blocker. |
| `append` | one phase | Appends `pending` tasks to `phase`, creating the phase if missing. |
| `view` | nothing | Read-only echo of the current list: no write, no normalization. |

**Arguments**

| Name | Type | Required | Bounds |
|------|------|----------|--------|
| `op` | string | yes | One of the nine names above. |
| `list` | array of `{phase, items}` | `init` only | At most 8 entries; `phase` at most 40 characters; each `items` holds at least 1 and each item at most 120 characters; at most 32 tasks in total. |
| `phase` | string | `append`; `init` when using flat `items` | At most 40 characters. On `done`/`drop`/`rm`/`block`/`unblock` it selects every task of that phase; `start` never takes a phase. |
| `task` | string | `start`; or `block`/`unblock` when no `phase`; optional elsewhere | At most 120 characters. Matched by exact content. |
| `items` | array of strings | `append`; `init` when not using `list` | Each item at most 120 characters; total of 32 tasks across all phases. |
| `reason` | string | no | Collapsed to a single trimmed line; only read by `block`. |

**State model**

```
TodoPhase = { name, tasks }
TodoItem  = { content, status, blocker? }
```

`status` is one of `pending`, `in_progress`, `completed`, `abandoned`, `blocked`.
`content` is unique across the whole list, and `name` is unique across phases. Rows
below are the task's status before the op; `unchanged` means the op skips that task.

| Current | start | done | drop | block | unblock | rm | append |
|---------|-------|------|------|-------|---------|----|--------|
| `pending` | `in_progress` | `completed` | `abandoned` | `blocked` | unchanged | removed | unchanged |
| `in_progress` | `in_progress` | `completed` | `abandoned` | `blocked` | unchanged | removed | unchanged |
| `blocked` | `in_progress` | `completed` | `abandoned` | `blocked` | `pending` | removed | unchanged |
| `completed` | `in_progress` | `completed` | `abandoned` | unchanged | unchanged | removed | unchanged |
| `abandoned` | `in_progress` | `completed` | `abandoned` | unchanged | unchanged | removed | unchanged |

**Targeting and normalization**

Targets resolve in this order, and the first non-empty match wins: exact `task`
content, then every task in the phase named by `phase`, then every task in every
phase. An empty match is an error, never a silent no-op.

After every successful mutation, and never after `view`, the list is normalized to at
most one `in_progress` task. If several are `in_progress`, the first in phase/task
order keeps the status and the rest become `pending`. If none is `in_progress`, the
first `pending` task is promoted. `blocked` tasks are skipped by that promotion, so a
list may legitimately have no active task.

**Bounds**

| Limit | Value | On breach |
|-------|-------|-----------|
| Phases | 8 | `E_TOOL`; list unchanged |
| Tasks in total | 32 | `E_TOOL`; list unchanged |
| Task content | 120 characters | `E_TOOL`; list unchanged |
| Phase name | 40 characters | `E_TOOL`; list unchanged |

Over-long input is refused, never truncated, so the list the model holds always
matches the list on the heap. Every mutating op is all-or-nothing: it applies in full
or not at all, and any error discards whatever that op had already changed. The
returned list and the stored list are then both the pre-call list.

**Result**

Every mutating op returns the resulting phase/task tree in this shape; `view`
returns the current tree unchanged, without writing it.

```json
{"phases":[{"name":"Tasks","tasks":[{"content":"read the sensor data sheet","status":"completed"},{"content":"write the I2C driver","status":"in_progress"},{"content":"benchmark the driver","status":"pending"}]}]}
```

A `blocked` task carries its blocker as one more member:
`{"content":"flash the board","status":"blocked","blocker":"waiting on the serial adapter"}`.

**Schema sent to the model**

```json
{
  "type": "object",
  "properties": {
    "op": {"type": "string", "enum": ["init", "start", "done", "drop", "block", "unblock", "rm", "append", "view"]},
    "list": {"type": "array", "items": {"type": "object", "properties": {"phase": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, "required": ["phase", "items"]}},
    "phase": {"type": "string"},
    "task": {"type": "string"},
    "items": {"type": "array", "items": {"type": "string"}},
    "reason": {"type": "string"}
  },
  "required": ["op"]
}
```

**Relationship to oh-my-pi**

The op set, parameter shape, state model, targeting rules and normalization are
taken from oh-my-pi's `todo` tool, so a model that knows one already knows the
semantics of the other. What differs is the surface around it. Four differences are
deliberate and are not oversights:

| Area | oh-my-pi | here | Why |
|---|---|---|---|
| Failure surface | `isError: true` plus a text summary prefixed `Errors: ...`, with `details.phases` still populated | `ok: false` with `E_TOOL` in `agent.tool_result` | One error convention across every tool on this device; a structured code is what the bridge and the run loop already read |
| Result payload | text summary for the model, `details.{phases, storage, op, completedTasks}` for the host | bare `{"phases": [...]}` | The device has no host-side detail channel inside a tool result, and the text summary would duplicate the tree the model already receives |
| Op inference | `lenientArgValidation` infers `op` from `list`/`items` | omitted `op` is `E_TOOL` | Inference makes a malformed call silently succeed; the schema already requires `op` |
| Persistence | `session` file when one exists, else memory | memory only | No session file exists on the device; `storage` would always be `"memory"`, so the field is omitted rather than constant |

A fifth deliberate divergence is enforcement. oh-my-pi leaves close-out to the
model, whereas the device loop refuses to finalize while the close-out check fails:
a task is still `pending`, `in_progress` or `blocked`, or the run has taken two or
more tool rounds without opening a list at all. It re-prompts and takes another
round until the check passes or `llm.max_tool_rounds` runs out, then reports
`todo_open: true` (`../02-firmware.md` §4.1). A one-round run that never needs a
list — a plain question — is compliant and finalizes normally.

Atomic discard is kept exactly: like oh-my-pi, an op that fails validation or names
nothing discards every mutation it made, and `view` never writes.

## Errors

`E_TOOL` covers every refusal:

- Unknown `op`, or an `op` outside the nine names.
- A field of the wrong type.
- A required field missing for the op: `list`/`items` for `init`, `task` for
  `start`, `phase` and `items` for `append`, `task` or `phase` for `block` and
  `unblock`.
- A task or phase name that matches nothing. The message says tasks are addressed by
  exact content, not by an id or an index, so the model re-reads the list and passes
  the text it saw.
- Duplicate content anywhere in the list on `append`.
- A duplicate phase name on `init`.
- Any of the four bound breaches above.

Every one of these leaves the list unchanged, including a failure in the middle of a
multi-target op. The refusal travels as `ok:false` with `E_TOOL` in
`agent.tool_result` (`../04-protocol.md` §3.13) and never aborts the run: the model
can call `view` and retry.

## See also

- `../00-overview.md` §7 -- frozen list of built-in tool names.
- `../06-security.md` §6 -- authorization model; `todo` has no gate.
- `../05-bridge.md` §7 -- `/todo` re-renders the last observed result.
- `../02-firmware.md` §2 `agent` -- heap ownership and the agent loop.
