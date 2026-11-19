---
name: scheduler-authoring
description: Author a persistent task with task.set: trigger, repeat and missed policy.
metadata:
  version: "1.0.0"
  keywords: "task schedule automation trigger repeat"
allowed-tools: task.list task.set
---

# Authoring scheduled tasks

`task.set` is the only way to persist automation. It creates or replaces one task
in `/tasks.json`; the firmware re-arms the trigger immediately, with no `reload` —
with one exception, below.

## Before writing

1. `task.list` first. It returns the stored entries with their `action` included,
   so you can recognise a task by its prompt before overwriting it.
2. Pick the id. `[a-z0-9_-]{1,15}`, unique. Reusing an existing id replaces it.
3. Ask whether this should be a task at all. A one-shot instruction belongs in the
   answer, not in a schedule that will fire tomorrow with no one listening.

## Choosing the trigger

| Need | `trigger.type` | Extra fields |
|---|---|---|
| On every boot | `boot` | none |
| Once the station has an IP | `wifi_connected` | none |
| After the first SNTP sync | `ntp_synced` | none |
| When the bridge link opens | `serial_connected` | none |
| At a wall-clock time | `schedule` | `kind:"at"`, `at` |
| Every N seconds | `schedule` | `kind:"every"`, `interval_s` |
| When a pin reaches a level | `gpio` | `pin`, `level` (0/1) |
| On a broker message | `mqtt_message` | `server_id`, `topic`, plus optional `payload_contains`, `cooldown_s` |
| On `event.emit` | `event` | `name` |
| Only when the user asks | `manual` | none |

`kind:"at"` takes an ISO-8601 instant or `HH:MM:SS` for the next daily occurrence.
Both need NTP: before the first sync the task is inert and the wait is not counted
as a miss. `kind:"every"` requires `interval_s` in 60..604800, and `first_at`
(epoch seconds) shifts the first fire.

A `gpio` task is the exception to "no `reload` needed": pins are armed only when the
task file loads, so a GPIO task you write now watches its pin only after the user
runs `reload` or reboots. Do not claim it is live.

## Repeat and miss

- `repeat.mode:"once"` fires and then stops. Choose it explicitly for anything that
  should happen today rather than every day; it is never what you get by omission.
- `repeat.mode:"count"` with `repeat.count` in 1..1000 fires that many times.
- `repeat.mode:"forever"` is the default when `repeat` is omitted, so omitting it on
  an `every` trigger is how a device ends up announcing itself every hour for a week.
  Say that out loud before you write one.

`missed` decides what a fire that passed while the device was off or unsynced does:

- `"skip"` (default) drops it. Correct for a status digest whose value is stale.
- `"run_once"` fires it once on the next sync, however many were missed. Wrong if
  the action has a side effect the user would not want twice.

## Writing the call

```json
{"task": {"id": "nightly-digest", "description": "Summarise the day", "trigger": {"type": "schedule", "kind": "at", "at": "23:30:00"}, "action": {"kind": "prompt", "prompt": "Summarise today."}, "repeat": {"mode": "forever", "count": 0}, "missed": "skip"}}
```

- `id`, `trigger` and `action` are required. `enabled` defaults to `true`, so a
  rewrite that omits it re-enables a disabled task.
- The task schema is closed. One key outside it rejects the whole call with
  `E_TOOL` and nothing is written, so never invent a field for a future use.
- `action.kind` is `"prompt"` (literal text) or `"template"` (a file under
  `/prompts/`, plus `action.vars`). Actions never carry a secret.

## Limits and authority

- At most `tools.task_max` tasks (default 16, never above the 16-entry schema cap).
  An update replaces in place and consumes no slot, so a full schedule can still be
  edited, including to disable an entry.
- You cannot delete a task. There is no delete field and no delete tool. Setting
  `enabled: false` is the only removal you have; ask the user to run
  `tasks.delete` on the bridge if a task must truly go.
- `task.set` requires `tools.allow_dangerous: true`. A refusal naming that key is
  final: report it and stop, do not retry.

A successful call returns the id. That is the whole confirmation.
