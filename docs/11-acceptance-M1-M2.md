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

- [ ] `.venv/bin/python -m pytest bridge/tests/test_link.py -p no:cacheprovider -k "short_line or latency"` passes.

This is the only place the 10 ms budget can be *observed*, and it is a host test
on purpose. Against real hardware the number is dominated by the device's read
loop, not the bridge: measured here, the same `ping` costs 1.1 ms when pings are
back to back and 13-33 ms when the host pauses between them, because
`usb_serial_jtag_read_bytes` blocks for up to 100 ms (`link_task.c`) and the
device only wakes when bytes arrive. A raw pyserial loop with no bridge in the
path reproduces the same tail, which is why this is not a bridge regression.

`07-roadmap.md` M1's 10 ms is met in the burst case and missed in the idle case.
Record which one you measured.

### M1.3 `status` returns device state

- [ ] `bin/bridge --send /status` prints keys `agent`, `boot_id`, `geo`, `llm`,
      `mcp`, `mem`, `mqtt`, `storage`, `sys`, `time`, `uptime_s`, `wifi`.

### M1.4 Input validation: oversize and malformed lines

The bridge refuses to build these frames itself, so the injection is raw bytes.
Save as `/tmp/m1_frames.py` and run `.venv/bin/python /tmp/m1_frames.py`:

```python
import asyncio, json, sys
sys.path.insert(0, "/home/saturne/Dev/Musteval")
from bridge.link import SerialTransport

async def main():
    t = SerialTransport("/dev/ttyACM0"); await t.open()
    await t.write_line(json.dumps({"v": 1, "type": "hello", "id": "b-h", "ts": 0,
                                   "client": "x", "client_version": "1",
                                   "protocol": 1}).encode())
    print("ack:", (await t.read_line())[:60])
    await t.write_line(b'{"v":1,"type":"command","id":"b-big","ts":0,'
                       b'"name":"status","args":{"x":"' + b"A" * 5000 + b'"}}')
    for _ in range(5):
        line = await t.read_line()
        if line and b'"reply_to":"b-big"' in line:
            print("oversize:", line.decode()); break
    await t.write_line(b'{"v":1,"type":"command","id":"b-bad", nope}')
    for _ in range(5):
        line = await t.read_line()
        if line and b'"reply_to":"b-bad"' in line:
            print("malformed:", line.decode()); break
    await t.close()

asyncio.run(main())
```

- [ ] `oversize:` line is a `command.result` with `"reply_to":"b-big"`,
      `"ok":false`, `"code":"E_PROTOCOL"`.
- [ ] `malformed:` line is a `command.result` with `"reply_to":"b-bad"` and
      `"message":"json at byte 39"` (measured on the board; the offset is where the
      parser stopped).
- [ ] `.venv/bin/bridge --send /status` still works afterwards and its `boot_id`
      equals the one recorded above: the link survived without a reboot.

A line with no recoverable `id` answers with the `protocol.error` event instead;
`dropped_bytes` is the real length:

```python
await t.write_line(b'{"v":1,"type":' + b"A" * 6000 + b"}")
```

- [ ] The event carries `"kind":"protocol.error"` and `"dropped_bytes":6015`.

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

`config.set` refuses an unresolved reference outright, so the bad document has to
arrive as a file. Saving `/tmp/m2_bad.py` and running it writes one:

```python
import asyncio, base64, json, sys
sys.path.insert(0, "/home/saturne/Dev/Musteval")
from bridge.link import SerialTransport

BAD = {"version": 1,
       "device": {"name": "board2", "log_level": "info"},
       "wifi": {"networks": [{"ssid": "t", "password_ref": "nvs:no_such_key"}]},
       "llm": {"providers": [{"id": "p1", "base_url": "https://a/v1", "model": "m",
                              "api_key_ref": "nvs:llm_test"}]}}

async def main():
    t = SerialTransport("/dev/ttyACM0"); await t.open()
    await t.write_line(json.dumps({"v": 1, "type": "hello", "id": "b-h", "ts": 0,
        "client": "x", "client_version": "1", "protocol": 1}).encode())
    await t.read_line()
    text = json.dumps(BAD, sort_keys=True, separators=(",", ":"))
    await t.write_line(json.dumps({"v": 1, "type": "command", "id": "b-p", "ts": 0,
        "name": "fs.put", "args": {"path": "/config.json", "encoding": "base64",
        "final": True, "data": base64.b64encode(text.encode()).decode()}}).encode())
    for _ in range(15):
        line = await t.read_line()
        if line and b'"reply_to":"b-p"' in line:
            print(line.decode()); break
    await t.close()

asyncio.run(main())
```

- [ ] `python /tmp/m2_bad.py` prints `"ok":true` and `"requires_reboot":true`.
- [ ] `bin/bridge --send /reload` prints
      `{'reloaded': ['config'], 'warnings': [{'message': 'password_ref: nvs:no_such_key is not set', 'scope': 'config'}]}`
      — measured. The key is named, the value is not.
- [ ] `bin/bridge --send /status` still answers and its `boot_id` is unchanged: the
      device kept the link instead of refusing to boot.

Restore by writing the good document back — `config.set` cannot express an array,
so the same script serves, with `password_ref` back to `nvs:wifi_test`:

- [ ] After restoring, `bin/bridge --send /reload` prints
      `{'reloaded': ['config'], 'warnings': []}`.

Note that a reboot is needed for the *boot* form of this check: `reload` proves the
degraded path, and the boot path additionally clears nothing but reads the same
file. `boot_id` changing is the evidence it rebooted.

### M2.2 `secret.set` then `reload`

`secret.set` takes its value from a masked modal, so it is a TUI action, not a
`--send` one.

- [ ] `bin/bridge`, then type `/secret set wifi_test` in the chat bar.
- [ ] The prompt masks the value; it never appears on screen or in the terminal.
- [ ] `/reload` prints `{"reloaded": ["config"], "warnings": []}`: the ref resolves
      again without a reboot.

### M2.3 `config.get` redacts

- [ ] `bin/bridge --send /config get wifi.networks.0.password_ref` prints
      `{'path': 'wifi.networks.0.password_ref', 'value': 'nvs:wifi_test'}`.

The value is the `nvs:` reference, never the stored secret. A leak would print the
passphrase itself in `value`.

### M2.4 Filesystem baselines

`/workspace/` is writable; `/prompts/`, `/skills/` and `/tools/` accept only their
declared extensions, and `fs.put` into them needs a payload the TUI supplies.

- [ ] `bin/bridge --send /sandbox list` prints `entries` including `r.txt` and `d1.txt`.
- [ ] `bin/bridge --send /fs put /prompts/x.md` prints `no file loaded to upload`:
      the slash command has no payload and refuses before the wire, so it proves
      nothing about the device. Use the Files screen (`F4`) instead: select a
      host file to upload into `/workspace/`, and confirm a write to `/prompts/`
      is refused with `E_CONFIG` (bad extension or built-in target).

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
