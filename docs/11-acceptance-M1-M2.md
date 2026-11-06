# Manual Acceptance, M1 and M2

`07-roadmap.md` §M1 and §M2 hold the criteria; this file is the hand-run
procedure that exercises them against a real board. Each action is a checkbox;
each names the surface it touches, because a criterion proven on the wrong
surface proves nothing about the device.

Run from the repository root, with the board on `/dev/ttyACM0` and the venv
present (`.venv/bin/python`). Commands are shown exactly as typed.

## Preconditions

- [ ] `bin/bridge --send /status` prints a JSON object. If it does not, the
      board is not on the link and nothing below will work.

Read its `boot_id`. Keep it: several checks below compare it to prove the device
did not reboot.

## M1: the link

### M1.1 Bridge connects, `hello`/`hello_ack` exchanged

- [ ] `bin/bridge --send /status` prints a JSON object and exits `0`.

`--send` performs the full handshake before the command, so a printed object is
the `hello`/`hello_ack` round trip succeeding. There is no separate "connected"
banner in headless mode.

### M1.2 `ping`/`pong` round trip under 10 ms

- [ ] `bin/bridge`, press `F3` for Status.
- [ ] Type `/ping` in the chat bar. The footer changes from `PING  no pong yet` to a
      number: measured 5.5 ms on an idle link, and every later `/ping` replaces it.

The footer is a live measurement: the heartbeat refreshes it every ten seconds, so
leave the screen open and the number moves on its own. Two facts about the budget,
measured on this board with 40 back-to-back pings and 30 with a 150 ms pause between
them:

| Pattern | median | p90 | min | max |
|---|---|---|---|---|
| Back to back | 2.0 ms | 33.9 ms | 1.2 ms | 51.4 ms |
| 150 ms between pings | 22.4 ms | 32.0 ms | 1.5 ms | 34.1 ms |

- [ ] The footer's value falls in the range the table shows for whichever pattern
      you are producing. One `/ping` typed after a pause is the idle case.

The budget is met when the device is already reading and missed when it is asleep.
The read call blocks for up to 100 ms and returns only when bytes arrive, so the
idle figure is a wake-up cost, not transfer time; a raw serial read with no bridge
in the path reproduces it, and the bridge's own contribution is the floor, about
1.2 ms. Record both numbers: M1's 10 ms is met back to back and missed at idle, and
that gap belongs to the device read loop.

`bin/bridge --send /ping` exits `0` and prints nothing. A pong is a heartbeat
frame, not a `command.result`, so headless has no body to show. That is why this
check is a screen and not a command.

### M1.3 `status` returns device state

- [ ] `bin/bridge --send /status` prints keys `agent`, `boot_id`, `geo`, `llm`,
      `mcp`, `mem`, `mqtt`, `storage`, `sys`, `time`, `uptime_s`, `wifi`.

### M1.4 Input validation: oversize and malformed lines

The bridge refuses to build these frames, so a user cannot type them and they are
not hand-testable through the TUI. They are covered by the host suite, which asserts
both halves of `04` §1.2:

- [ ] `cd firmware/test_host && ./build/musteval_test_host.elf` passes: cases
      "an oversize line still exposes its id prefix", "an oversize line without an
      id reports the bytes it dropped" and "a line that is not JSON names the parse
      offset" are in that binary. Build first with the command in
      `AGENTS.md`'s Test Strategy table. If the binary is absent,
      `.venv/bin/python -m pytest bridge/tests/test_docs.py -p no:cacheprovider`
      covers the protocol examples instead.

On the board the observable half is recovery: a refused line costs one reply, not
the link. Watch `boot_id` in `/status` (chat bar, or `F3`):

- [ ] `bin/bridge --send "/config set device.log_level loud"` answers a
      `command.result` with `ok:false` and `E_CONFIG`: the device answered a refusal
      and stayed up.
- [ ] `bin/bridge --send /status` still answers and its `boot_id` is unchanged.

The bytes for an oversize line, a malformed line and their `dropped_bytes` counters
are asserted by the host suite and cannot be typed: the bridge refuses to build such
a frame, and `test_agent.py` is a peer of the device, not a way around that.

### M1.5 Log hook: `ESP_LOGx` arrives as NDJSON `log`

A command that succeeds logs nothing, so the trigger must be one that warns.

- [ ] `bin/bridge --json --log-file /tmp/frames.ndjson --send "/config set device.log_level loud"`
- [ ] `grep -v '^$' /tmp/frames.ndjson | grep '"type":"log"'` prints a line with
      `"level":"warn"`, `"tag":"musteval"`, `"msg":"config.set rejected: device.log_level: not an allowed value"`.

The frame carries `t_ms` (uptime milliseconds), not wall time: `ts` is `0` until
SNTP completes. The TUI shows the same frames in the Logs screen (`F5`); lines
produced before `hello` are not replayed.

### M1.6 Headless run against the test agent, no board

- [ ] Terminal 1: `.venv/bin/python bridge/tools/test_agent.py --port /tmp/ta > /tmp/ta_out &`
      then `sleep 3`. The agent prints the pty it opened on its first stdout line.
- [ ] Terminal 2: `.venv/bin/bridge --target test --port "$(head -1 /tmp/ta_out)" --send /status`
      prints a status object and exits `0`, with no board attached.

`--target test` is required: `--target usb` refuses a pty by design
(`10-test-agent.md` §3). The agent writes the `/dev/pts/N` path to stdout and a
human line to stderr; `head -1` takes the path.

## M2: configuration and storage

### M2.1 An unresolved `secret_ref` loads degraded, naming the key

There is no hand-testable route to this state today, and that is a gap in the
product rather than in this procedure. `config.set` refuses an unresolved reference
outright, and the only writer that can place a whole document — `fs.put` — needs its
bytes built over the wire, which no screen does: the Files screen uploads through
`sandbox.write` and cannot reach `/config.json`. The degraded path is therefore
reachable only by a script, and its firmware side has no host test either.

What a user can observe is the refusal, which is the same rule seen from the other
side:

- [ ] `bin/bridge --send /config set wifi.networks.0.password_ref nvs:no_such_key`
      prints `E_CONFIG` with "a secret reference in the result does not resolve".
- [ ] `bin/bridge --send /config get wifi.networks.0.password_ref` still returns the
      old value: the rejected write changed nothing.

Restoring and re-reading a good document is covered by M2.2, which is the same
`reload` path.

### M2.2 `secret.set` then `reload`

`secret.set` takes its value from a masked modal, so it is a TUI action, not a
`--send` one.

- [ ] `bin/bridge`, then type `/secret set wifi_test` in the chat bar.
- [ ] The prompt masks the value; it never appears on screen or in the terminal.
- [ ] `/reload` prints `{'reloaded': ['config'], 'warnings': []}`: the ref resolves
      again without a reboot. Measured; `secret.set` answers
      `{'key': 'wifi_test', 'length': 16, 'stored': true}` and never echoes the value.

### M2.3 `config.get` redacts

- [ ] `bin/bridge --send /config get wifi.networks.0.password_ref` prints
      `{'path': 'wifi.networks.0.password_ref', 'value': 'nvs:wifi_test'}`.

The value is the `nvs:` reference, never the stored secret. A leak would print the
passphrase itself in `value`.

### M2.4 Filesystem baselines

`/workspace/` is writable; `/prompts/`, `/skills/` and `/tools/` accept only their
declared file extensions. `fs.put` takes its bytes over the wire, so a slash command
cannot express one.

- [ ] `bin/bridge --send /sandbox list` prints
      `{'entries': [{'bytes': 0, 'dir': True, 'name': 'd'}, ...], 'path': '/workspace/'}`
      — measured; the directory and its files are both listed.
- [ ] `bin/bridge --send "/fs put /prompts/x.md"` prints `no file loaded to upload`
      — measured. The refusal is the bridge's, before anything reaches the wire, so
      this step proves the guard exists and proves nothing about the device.
- [ ] `F7` for Files, `Ctrl+O` uploads a host file into `/workspace/`; the chat log
      prints one line naming the destination, and the file appears in the table.
      The screen browses `/workspace/` only, so `/prompts/` and the other
      extension-restricted roots cannot be reached from it: the allowlist below
      them is asserted by the host suite, not hand-checked here.

### M2.5 Config writes are atomic

- [ ] `bin/bridge --send /config set wifi.networks.0.ssid ""` is rejected with
      `E_CONFIG`, and `bin/bridge --send /config get wifi.networks.0.ssid` still
      returns the previous value.

`config.set` validates a clone and keeps the old document on failure.

## Do not

- Do not clear `wifi.networks` entirely: the device then has no network to join
  and `net_connect` answers `E_WIFI` on every boot.
- Do not write `llm.providers` to an empty array: `llm_init` finds no provider and
  `status.llm.available` stays false with `last_error` set.
- Do not power-cycle during a `config.set`; the write is a whole-file replace.
