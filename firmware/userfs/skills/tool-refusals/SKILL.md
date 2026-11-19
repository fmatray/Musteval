---
name: tool-refusals
description: Read a refusal code and decide: fix the call, wait, or stop.
metadata:
  version: "1.0.0"
  keywords: "error refusal tool terminal gate"
---

# Reading a refusal

A refused tool call arrives as a result with `ok: false` and a code. It never aborts
the run; it is information. The mistake is retrying a code that cannot change.

## The three kinds

Only these codes ever reach you, because only a tool call produces them:

| Kind | Codes | What to do |
|---|---|---|
| Wrong argument | `E_TOOL` (bad value, bad path, wrong shape) | Fix the argument and call again. The code is yours to correct. |
| State, outside the run | `E_WIFI`, `E_GEO`, `E_MQTT` (disconnected), `E_TIMEOUT` | Wait for the state to change — usually not within this run. Report it. |
| Terminal | `E_TOOL` from a closed gate, `E_CONFIG`, `E_STORAGE` (real fault), `E_INTERNAL` | Stop. Nothing you can call fixes it. Say what the user must do. |

`E_CANCELLED` means the run is ending under you; do not fight it.

Codes such as `E_BUSY`, `E_SAFE_MODE`, `E_LLM_RATE` and `E_AUTH` are answers to a
bridge command or a run, never to a tool call. If you see one, it is the run
speaking, not a tool you can retry.

## The ones worth naming

**`E_TOOL` from a closed gate.** `tools.allow_dangerous: false` refuses
`gpio.write`, `i2c.write`, `fs.write`, `fs.delete`, `task.set` and `mqtt.publish`.
The message names the key. There is no alternative tool and no argument that opens
it: report that actuation is disabled and stop. The same code from a bad argument is
the opposite case — fix and retry.

**`E_STORAGE`.** Two very different things. As a quota refusal (`/workspace/` full)
the fix is `fs.delete` and a retry. As a mount or unlink failure, nothing was
changed and retrying proves nothing. The message says which; when it does not, say
you could not tell rather than retrying.

**`E_CONFIG`.** The device's configuration does not support the call — `geo.locate`
with `geo.enabled: false`, for example. No argument fixes it; the user edits
`config.json` and runs `reload`.

**`E_INTERNAL`.** An unclassified firmware fault. Not yours to diagnose or retry
inside this run; report it.

**`E_TOOL` from `skill.read`.** The skill name does not resolve, or the body is
larger than a tool result can carry. The refusal does not hint at the nearest name,
deliberately, so do not guess a second spelling repeatedly — if the index does not
list it, it is not there.

## Loops

Two identical refusals in a row mean the third attempt cannot differ. Change the
argument, change the tool, or report. A run that retries a fixed refusal burns the
token budget and produces an answer that is later than not trying.

## What never changes a refusal

- Re-issuing the same call with the same arguments.
- Wrapping it in a different tool that reaches the same gate.
- Writing a file into `/workspace/` as a stand-in for something you were not allowed
  to do.
- Inventing a result. If a call was refused, the answer says it was refused and why.

## Reporting a refusal

Name the code, say what it means in one clause, and say what the user can do. For a
gate: which config key. For `E_CONFIG`: which key and that `reload` applies the fix.
For `E_STORAGE` quota: what to delete. Do not pad it with an apology or a second
attempt dressed as a diagnosis.
