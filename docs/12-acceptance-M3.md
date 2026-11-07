# Manual Acceptance, M3

`07-roadmap.md` §M3 holds the criteria; this file is the hand-run procedure that
exercises what can be exercised against a real board today. Each action is a
checkbox and names the surface it touches.

Four of M3's five criteria cannot be fully reached from the bridge: the failover
half, the `ntp_synced` event, the prompt run, and the TLS and fallback pair. Each
is stated as a gap in place, with the reason and the milestone that closes it,
rather than given a procedure that would not prove it. The firmware host suite
covers the logic underneath.

Run from the repository root, with the board on `/dev/ttyACM0` and the venv
present (`.venv/bin/python`). Commands are shown exactly as typed.

## Preconditions

- [ ] `bin/bridge --send /status` prints a JSON object. If it does not, the board
      is not on the link and nothing below will work.

Read its `boot_id`: the reboot below changes it, so it is how you tell a fresh boot
from the one you were already talking to. Read `sys.boot_failures` too. Every boot
increments it, and only `reload` or `sys.reset_counter` clears it, so a run of
reboots trips safe mode on the fourth boot. Before every reboot below, clear it:

- [ ] `bin/bridge --send "/sys reset-counter"` answers `{"previous":N,"reset":"boot_failures","safe_mode":false}`.

Safe mode skips the radio, the clock and the model, so a boot in that state proves
nothing about M3. If `/status` shows `sys.safe_mode: true`, run the command above
and reboot before continuing.

## M3.1 WiFi association and the `E_WIFI` path

The board's `wifi.networks[0]` names an SSID this host cannot serve, so the
expected outcome is a named refusal, not a connection. That is the half of the
criterion a reader without a hotspot can still prove.

- [ ] `bin/bridge --send /status` shows `wifi.state: failed`, `wifi.ssid: null`,
      `wifi.ip: null` and `wifi.failovers: 1`.

One refused network reports `failovers: 1`, the number of entries tried. With a
connected link the same field holds the index of the network that answered, so a
zero is ambiguous and one or more is not.

The boot log is the surface that names the failure. The bridge must be attached
while the device boots: `link_start` runs after the log hook is installed, so
frames emitted before the handshake are not replayed.

- [ ] `bin/bridge`, dismiss the splash with `Esc`, `F2` for Chat.
- [ ] In the chat bar, type `/sys reset-counter` and press Enter, then `/reboot`
      and press Enter. Logs (`F5`) gains the lines below, in this order. `F5`
      follows the tail, so they appear within a second of the device restarting.

Neither command needs a second bridge: one process on `/dev/ttyACM0` is the only
one that can drive the port.

| Level | Tag | Message |
|---|---|---|
| `warn` | `net` | `wifi: t: no association (timeout 15000 ms), backoff 1000 ms` |
| `error` | `net` | `wifi: every configured network refused (E_WIFI)` |
| `warn` | `musteval` | `boot: wifi: no configured network joined (E_WIFI)` |
| `warn` | `musteval` | `boot: ntp: ESP_ERR_NOT_FOUND` |
| `warn` | `musteval` | `boot stops before the scheduler: counter stays at N` |

`N` is `sys.boot_failures` after this boot, so it reads `1` when you ran the reset
just before. It is the same number `/status` reports, and it is the one that reaches
the safe-mode threshold. The `net` tag is the module; the `musteval` lines are the
boot sequence deciding what to do about it. `E_WIFI` appears in both because
`ESP_ERR_INVALID_STATE` would read as a programming error rather than "no network"
(`02-firmware.md` §4.2).

### Gap: failover between two networks

`07-roadmap.md` §M3 asks for a wrong password on the first network logging `E_WIFI`
and the second connecting. This board has one network and the bridge cannot add a
second one by hand.

The device accepts it. Driven directly over the session, `config.set` with
`path: "wifi.networks"` and a two-entry array answers
`{"applied":true,"path":"wifi.networks","requires_reboot":false}` and the read-back
returns both entries. Three hand routes were tried and all fail, for the same
reason: every typed command sends its value as a JSON **string**.

- `/config set wifi.networks [...]` answers `E_CONFIG` / `value rejected by the
  config schema`, because a string is not an array.
- `/config set wifi.networks.1.ssid other` answers `E_CONFIG` / `no such path in
  config.json; a set never creates a parent key`, because `json_path_set` replaces
  an existing member and never grows an array or an object.
- `/onboard` is the one command that sends a real list, and its plan writes exactly
  one network.

Until the bridge has a route that sends a list, the two-network criterion is proven
only by the direct call above and by the host suite, which asserts the ordering and
the backoff schedule (`test_m3.c`: "networks are ranked highest priority first",
"equal network priorities keep their config order", "the retry backoff repeats its
last entry"). The association itself needs an access point; this host has none.

## M3.2 Clock sync and `time.now`

- [ ] `bin/bridge --send /status` shows `time.synced: false` and `time.tz: UTC0`
      while `wifi.state` is `failed`.

`time.tz` is `UTC0` rather than `UTC`: `UTC` is a zone name the C library may not
know, and `UTC0` is always a valid POSIX string (`03-config.md` §ntp).

- [ ] `bin/bridge --send /status` twice, a minute apart. `time.unix` grows by
      roughly the elapsed seconds and stays far below the current epoch.

An unsynced board reports the RTC's own count, not wall time. The field has the
same name either way, which is why `time.synced` exists: a reader who sees a small
number must be able to tell which one they are looking at. Two measurements pin
the behaviour, and neither is a fault:

- `time.unix` is **not** `uptime_s`, and it survives a reboot. Measured here:
  `uptime_s: 91`, `time.unix: 60071` before a reboot, then `uptime_s: 7`,
  `time.unix: 60088` after it. The RTC keeps counting across a software reset while
  the boot timer does not, so comparing the two fields is not a check of anything.
- `time.unix` advances one second per second, but its origin is the board's, not
  1970, until SNTP sets it. Measured: `uptime_s: 59`, `time.unix: 60174`, then
  `uptime_s: 65`, `time.unix: 60179`. So it runs, and it is not the real time.
  `time.synced: false` says that outright; there is no other field that does.

### Gap: `ntp_synced` does not fire

`07-roadmap.md` §M3 requires the `ntp_synced` event. The event is a task trigger,
and the task engine is `sched`, which lands in M5; the boot sequence logs
`boot stops before the scheduler`. No code in `firmware/` emits the trigger today:
`clock` records the first successful sync in a module flag and nothing reads it.
The clock's timekeeping is proven on the host (`test_m3.c`: "the clock sync
interval is clamped to 60..86400 s"), and `clock_synced` is proven here by
`time.synced`. The event itself has no consumer until M5.

### Gap: clock discipline needs a synced clock

The criterion says "clock discipline verified via `time.now`". There is no
`time.now` handler in `firmware/main/main.c` and no `tools` registered in
`hello_ack` (`capabilities.tools` is an empty array), so the tool answers nothing.
Discipline also needs a live SNTP server, which needs an associated network, which
this host cannot provide. With credentials this becomes: associate, wait for
`time.synced: true`, then read `time.unix` twice and compare against a host clock.

## M3.3 The LLM status shape

- [ ] `bin/bridge --send /status` shows `llm.limit_hour: 100` and
      `llm.limit_day: 1000` on a board whose `/config.json` has no `llm` limits.

Those are the compiled defaults, which is the observable half of "the budget is
enforced": the numbers the module will charge against are visible before any call.
`llm.provider` and `llm.model` are `null` and `llm.available` is `false`, because
they describe the last **successful** call, not the configured chain. A device that
has never answered a call reports `null`, and that is not a fault.

- [ ] `bin/bridge --send /status` keeps `llm.calls_hour: 0` and
      `llm.calls_day: 0`. Nothing charges the budget until a call is attempted.

### Gap: prompt → `agent.start` → `agent.final`

`07-roadmap.md` §M3 requires a non-streaming prompt to run to `agent.final`. The
agent loop is `07-roadmap.md` §M4, and the device says so:

- [ ] `bin/bridge --send "hello"` prints `{'code': 'E_INTERNAL', 'message': 'agent
      loop not implemented before M4', 'retryable': True}`.

`llm_query` exists and is tested on the host, but nothing in `firmware/` calls it.
The request body, the escaping, the endpoint append and the chain walk are proven
by `test_m3.c` ("the request body has the frozen shape", "prompt text is escaped
into the request body", "a control byte is escaped as a unicode escape", "a body
that does not fit is refused, not truncated", "extra_body members join the request
object", "tools are sent only when offered", "the endpoint appends
/chat/completions verbatim", "the provider chain is walked once"). What is not
proven anywhere is a call leaving the device, because no command can start one.

### Gap: TLS refusal and provider fallback

Both criteria (`E_LLM_UNAVAILABLE` on a self-signed certificate; the second
provider used transparently) are properties of a call, and no call can be started.
The verification itself is unconditional in code: `net_post` attaches
`esp_crt_bundle`, and there is no path that disables it, so there is no plaintext
fallback to find. A reader who wants the negative half can break the chain instead
of the certificate — point `llm.providers.0.base_url` at a host that refuses, and
the fallback walks to provider 2. That still needs an associated network.

## M3.4 The spend cap is boot-only, and the reply says otherwise

This is a defect, recorded so a reader who hits it does not think their write
failed. It has no hand-run route today, which is why it is here rather than above.

`llm.max_calls_per_hour` is read by `llm_init`, which runs once at boot. A
`config.set` of that path answers `requires_reboot: false`, so the bridge prints
`llm.max_calls_per_hour applied` and the reader is told the new value is live. It
is not: `/status` keeps reporting `llm.limit_hour: 100` until the device reboots.

The board's `/config.json` has no `llm.max_calls_per_hour`, and a typed set cannot
create one, so the defect cannot be reached from the bridge at all:

- [ ] `bin/bridge --send "/config set llm.max_calls_per_hour 2"` answers `E_CONFIG`
      / `no such path in config.json; a set never creates a parent key`.

The staleness was measured by driving the session directly, with the whole `llm`
object rather than a leaf: `{"applied":true,"path":"llm","requires_reboot":false}`,
then `/status` still `limit_hour: 100`, then `limit_hour: 2` after a reboot.

`04-protocol.md` §4.9 says `false` for `requires_reboot` means the new value is
active immediately, and the comment in `handle_config_set` names `wifi.*` and
`ntp.servers` as the boot-only families. `llm.max_calls_per_hour` belongs in that
list and is not in it.

Until that is fixed, treat a `config.set` of any M3 key as needing a reboot and
verify with `/status` rather than trusting the reply.

## Host suite

The logic that cannot be reached from the bridge is asserted here. Build first with
the command in `AGENTS.md`'s Test Strategy table: `cd firmware/test_host`, remove
`build` and `sdkconfig`, then
`idf.py -DCOVERAGE=ON --preview set-target linux build`.

- [ ] `cd firmware/test_host && ./build/musteval_test_host.elf` prints
      `118 Tests 0 Failures 0 Ignored`. The M3 cases are the ones tagged `[m3]` in
      `main/test_m3.c`, and every name this file quotes is in that binary's output.

The binary takes no filter argument, so it runs every case; `grep` its output for a
name above to see the individual line.

The suite proves ordering, backoff, budget windows, body shape, escaping, the
endpoint, and the chain walk. It does not prove a radio associating, a certificate
failing or an HTTP response arriving: those need the board and a network.

## Do not

- Do not reboot more than three times without `bin/bridge --send "/sys reset-counter"`.
  The fourth boot enters safe mode and skips the radio, the clock and the model, so
  every M3 check above then reads as "not configured" rather than "not working".
  `bin/bridge --send /reload` also clears the counter.
- Do not read `status.llm.provider: null` as "no provider configured". It reports
  the last successful call, and no call can succeed before M4. Use `config.get llm`
  to see the chain.
- Do not expect a boot log from a bridge attached after the boot. The log hook is
  installed before the handshake and frames are not replayed.
- Do not clear `wifi.networks`: `config.set` cannot grow an array back, and the
  device then has no network to join on any future boot.
