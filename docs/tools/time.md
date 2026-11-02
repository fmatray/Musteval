# Time tools

Read the device clock. The clock is disciplined by SNTP against `ntp.servers[]`,
tried in order until one sync succeeds, and re-synced every `ntp.sync_interval_s`.
`time.now` has no gate and is available in every run.

| Tool | Gate | Errors |
|---|---|---|
| `time.now` | none | `E_TOOL` on runtime failure |

Gate matrix: `../06-security.md` §6.

Clock state, as documented:

- Boot step 6 syncs via SNTP before the link starts, and `ntp_synced` fires on the
  first valid update (`../02-firmware.md` §3.1, §5.1). A boot with no reachable NTP
  server therefore has no disciplined clock at the point the link comes up.
- `hello_ack.time_synced` and `status`'s `time.synced` expose whether SNTP has
  completed. Frame `ts` is `0` when the clock is unsynced (`../04-protocol.md`).
- `ntp.timezone` is a POSIX TZ string applied with `setenv("TZ", ...)`; it shifts
  local wall-clock rendering. `schedule` triggers with a bare `HH:MM:SS` use it and
  are inert before the first sync.

## `time.now`

Returns the current instant as Unix seconds plus an ISO-8601 string. No arguments,
no side effects, no storage access.

Before the first successful sync the clock is not disciplined; the docs do not state
what value `time.now` returns in that window. [ASSUMPTION] It still returns the
current RTC/uptime-derived value rather than failing, since no error is documented
for an unsynced clock — callers that need a trustworthy instant should check
`time.synced` from `status` instead of trusting this result.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| — | — | — | The schema declares no properties. |

**Result**

```json
{"unix": 1730563213, "iso": "2026-11-02T12:00:13Z"}
```

`unix` is integer Unix seconds. [ASSUMPTION] `iso` is the same instant rendered in
UTC with a `Z` suffix, independent of `ntp.timezone`; the docs do not say whether
the timezone offset is applied to this string.

**Schema sent to the model**

```json
{"type": "object", "properties": {}}
```

## Errors

- `E_TOOL` — runtime failure of the clock read. No input can cause an error: the
  schema takes no properties.
- An unsynced clock is not an error; see the `[ASSUMPTION]` above.

## See also

- `../02-firmware.md` §3.1 — boot sequence, clock step
- `../03-config.md` — `ntp.servers`, `ntp.timezone`, `ntp.sync_interval_s`
- `../02-firmware.md` §7.1 — built-in tool table
- `../04-protocol.md` — `time_synced` and `status` clock fields
