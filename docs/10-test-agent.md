# 10 — Test Agent

Status: derived from `04-protocol.md` (frozen wire format) and `09-bridge-architecture.md`
(bridge arrangement). This document re-decides nothing: every frame it emits is a
member-for-member copy of the catalogue in `04`, and every placement rule comes from
`09`. What it specifies is a maintainer tool.

---

## 1. What it is

A protocol conformance checker and echo. It is **not** a device simulator, and the
distinction is load-bearing: a simulator would have to model config, NVS, the LLM,
WiFi, the scheduler and the flash layout. This tool models only the wire. It accepts
every bridge `→` device frame the protocol allows, validates it, and answers with the
reply `04-protocol.md` §3 says that frame gets.

Everything it answers is derived from `04-protocol.md`. It therefore cannot disagree
with the frozen catalogue: there is no second copy of a field list to drift, and a
change to `04` is a change the tool inherits by reading the same text. Where the tool
answers with canned values (`device`, `fw_version`, `boot_id`), those values are
constants, not policy.

## 2. Where it sits

It lives at `bridge/tools/test_agent.py`, in the off-tree tools directory that
`09-bridge-architecture.md` §2 declares is **not a layer**. It MAY import
`bridge/protocol/` (L0) and MUST NOT import any bridge layer above L0. Importing
`protocol` is what keeps it from becoming a second codec, which `09` §8 forbids; the
alternative — re-implementing encode and decode in the tool — would be exactly the
duplicate implementation the law is written against.

It ships with neither the app nor the library. No shipped module imports it, and it
is not an entry point of the `uv run bridge` script.

Because it imports L0 only, it can never mask a bug in `link` or `client`. The tool
sees bytes that those layers already produced and produces bytes they will consume.
A green session says the wire format and the peer's behaviour are right; it says
nothing about the bridge's internals, and the bridge's own test seams
(`09` §7) remain the only place where those are exercised.

## 3. Choosing the target

The bridge selects its peer with `--target usb|test`, default `usb`. `--port` keeps
its meaning under both targets: the serial device path.

| Target | `--port` | Guard before opening |
|---|---|---|
| `usb` | omitted → autodetect by VID/PID; given → must match the XIAO board | refuses a path that is a pseudo-terminal |
| `test` | required; the path the fake agent printed | refuses a path that resolves to a real USB CDC device |

The guards are the reason the flag exists instead of a bare `--port`. Without them, a
`usb` session can be pointed at a pty by accident and a `test` session can stream test
frames into real firmware. Both checks are cheap, run before a single byte is written,
and turn a silent wrong-peer session into an immediate error. Autodetection is skipped
entirely for `test`: there is no VID/PID to look for.

The fake agent prints its device path on stdout at startup, and `--target test`
demands `--port`, so there is no rendezvous file and no hidden state. An explicit path
is debuggable — it appears in the shell history and in the command line of a running
process — and it cannot go stale.

## 4. The pseudo-terminal

The default transport is `pty.openpty()`. The tool keeps the master fd and does raw
`os.read`/`os.write` on it; the slave path (`/dev/pts/N`) is what it prints for the
bridge to open with pyserial. The tool therefore needs no pyserial dependency at all,
which is why the two ends are genuinely asymmetric: there is no port to configure and
no baud rate is negotiated, because a pty has none.

Operational facts that follow from this:

- The master side is in raw mode: no echo, no line discipline translation, no signal
  characters. Bytes written are bytes read.
- A pty reports no modem-control lines, so pyserial may warn about missing CTS/DSR.
  `[ASSUMPTION]` pyserial opens a Linux pty slave without error.
- For a symmetric setup use
  `socat -d -d pty,raw,echo=0 pty,raw,echo=0` and point the tool at one path and the
  bridge at the other. This is needed only when testing reconnect by killing one end.
- **All human-readable diagnostics go to stderr.** Only NDJSON writes to the pty. A
  stray `print` on the wire is a framing bug, and the tool MUST NOT be able to cause
  one; keeping the two streams apart is how that is made structural rather than
  vigilant.

## 5. What it validates

Every inbound line climbs this ladder and stops at the first failure.

| # | Check | On failure |
|---|---|---|
| 1 | line fits the RX cap of 4096 bytes (`04-protocol.md` §1.2) | oversize path |
| 2 | valid UTF-8, then a single JSON object | parse path |
| 3 | envelope: `v` is 1, `type` and `id` are strings, `ts` is an integer | envelope path |
| 4 | direction: `type` is one of the seven bridge `→` device types | `E_PROTOCOL` with `detail.direction` = `bridge_to_device` |
| 5 | required members, per type | schema path |
| 6 | semantics: token presence matches the expectation, `secret.set.value` non-empty, `command.name` frozen | `E_CONFIG` for the value, `E_PROTOCOL` for the rest |
| 7 | `hello` arrived at most once since the last reset | duplicate `hello`, `E_PROTOCOL` |

The seven bridge `→` device types, and the members check 5 requires of each:

| Type | Required members |
|---|---|
| `hello` | `client`, `client_version`, `protocol`; `token` exactly when `--token` is set |
| `prompt` | `text` |
| `cancel` | none (`run_id`, `reason` optional) |
| `command` | `name` |
| `secret.set` | `key`, `value` |
| `ping` | none |
| `ack` | `ack_of` |

Check 7 comes from `04` §3.1: the bridge sends exactly one `hello` per port open.
A second one is a bridge bug, and a tool that accepted it would report a green run for
a session no real device would have. The latch is cleared by a `--script` reconnect or
a process restart, which is why it is stated as "since the last reset" rather than
"ever".

A device `→` bridge type arriving is a direction violation, not an unknown type: the
frame is well-formed and only the arrow is wrong, which is why the reply names the
direction. `04-protocol.md` §2.1 makes that distinction and `09` §3 makes direction
checking a decode concern, so the tool inherits it rather than inventing a code.

Two failure paths, both straight from `04` §1.2. There is no third.

- **addressable**: an `id` was recoverable → reply `command.result` with `ok:false`
  and `error:{code, message}`; `name` echoes the received `command.name` when one was
  parsed, and is `""` otherwise, because `04` §3.20 makes the member required. For
  checks 1–4 the code is `E_PROTOCOL` and `error.detail` is
  `{"direction":"bridge_to_device"}`.
- **unaddressable**: no recoverable `id` → reply `event` with `kind:"protocol.error"`
  and `data:{code, message, dropped_bytes}`, where `dropped_bytes` is the length of
  the line the tool refused to parse.

```json
{"v":1,"type":"command.result","id":"d-42","ts":1730563220,"reply_to":"b-4","name":"bogus","ok":false,"error":{"code":"E_PROTOCOL","message":"unknown command name","detail":{"direction":"bridge_to_device"}},"duration_ms":0}
```

```json
{"v":1,"type":"event","id":"d-43","ts":1730563221,"kind":"protocol.error","data":{"code":"E_PROTOCOL","message":"not valid JSON","dropped_bytes":61}}
```

## 6. What it replies

| Received | Reply |
|---|---|
| `hello` | `hello_ack` with `reply_to`. Canned: `device` = `test-agent`, `fw_version` = `test-1.0.0`, `protocol` = 1, fixed `boot_id`, `time_synced` = true, `limits` = `{rx_max:4096,tx_max:16384}`, `capabilities` with a short canned `tools` array, empty `mcp`, `dangerous_tools` false, `streaming` false. With `--token` set and `token` absent or wrong → `event` with `kind:"link.rejected"` instead. |
| `ping` | `pong` with `reply_to`. |
| `command` | `command.result` echoing `name`, `ok:true`, `duration_ms`, and `result` per §6.1: the canonical status object for `name:"status"`, `{"echo": <args>}` for every other name. |
| `sandbox.list`, `sandbox.read` | `command.result` with the canned workspace described in §6.2, so the Files screen (`05` §5.8) has something to show. Echoing `args` would leave it empty. `sandbox.write` and `sandbox.delete` echo, because the reply a client reads is `path` plus `created`/`deleted`, which the tool cannot invent. |
| `secret.set` | `command.result` with `name:"secret.set"` and `result:{key, stored:true, length}`. The value MUST NOT appear in any reply, log line or diagnostic — `04` §3.6 and `06-security.md` forbid it. Only its length comes back. |
| `prompt` | `agent.start` first, then `agent.final`. `agent.start` carries `reply_to`, a fresh `run_id`, `trigger:"manual"`, `source:"prompt"`, fixed `provider`/`model`, `input_bytes`, `queue_depth:0`. `agent.final` has the same `run_id` and `reply_to`, `text` = the echoed prompt text, a fixed `usage`, `rounds:1`, `truncated:false`, `todo_open:false`, plus fixed `provider`, `model` and `duration_ms`. |
| `cancel` | With a run in flight: `agent.error` with `code:"E_CANCELLED"`, that `run_id`, `retryable:false`. With nothing in flight: `event` with `kind:"cancel.noop"`. This mirrors `04` §3.4, which says `cancel` has no dedicated ack. |
| `ack` | Nothing. `04` §3.8 makes it advisory. |

```json
{"v":1,"type":"hello_ack","id":"d-1","ts":1730563201,"reply_to":"b-1","device":"test-agent","fw_version":"test-1.0.0","protocol":1,"boot_id":"00000001","time_synced":true,"limits":{"rx_max":4096,"tx_max":16384},"capabilities":{"tools":["time.now","sys.diagnostics","todo"],"mcp":[],"dangerous_tools":false,"streaming":false}}
```

### 6.1 The `status` exception

The bridge sends `command{name:"status"}` automatically after the handshake
(`05-bridge.md` §4.1 step 4), and `04-protocol.md` §4 fixes that command's `result`
to be the status object of §3.9. Echoing `args` there would be a protocol violation,
not a shortcut, and would leave the Status screen (F3) empty in every test session.

So `status` is the one name that does not echo. It returns this constant, which is
schema-valid against §3.9 and identical on every run:

```json
{"wifi":{"state":"off","ssid":null,"rssi":null,"ip":null,"failovers":0},"time":{"synced":true,"unix":1730563600,"tz":"UTC"},"llm":{"provider":"test","model":"test-echo","available":true,"last_error":null,"calls_hour":0,"calls_day":0,"limit_hour":null,"limit_day":null},"mcp":[],"agent":{"busy":false,"run_id":null,"queue_depth":0,"max_queue":2},"mqtt":[],"geo":null,"storage":{"userfs":{"used_bytes":0,"total_bytes":3932160},"wsfs":{"used_bytes":0,"total_bytes":1048576},"workspace_quota_bytes":65536},"mem":{"heap_free":0,"psram_free":0},"sys":{"safe_mode":false,"boot_failures":0,"tool_calls":0,"tool_failures":0},"uptime_s":0,"boot_id":"00000001"}
```

`boot_id` matches the `hello_ack` value, which `04` §3.9 requires. The counters are
zeros and `mem` is zeroed because there is no heap to read; a test asserting on
Status-screen layout needs every member present, not every member meaningful.
`wifi.state` is `off` and `geo` is `null` because the tool has no network.

The echo is the point. A `command` comes back with its own `args` inside
`result.echo`, and a `prompt` comes back as its own text in `agent.final.text`, so a
round-trip is verifiable by inspection rather than by trusting the tool.

### 6.2 The canned workspace

`sandbox.list` and `sandbox.read` answer from a fixed tree, the third name class that
does not echo. Every path is inside `/workspace/`; a path outside it is answered with
the addressable `E_CONFIG` error that `04` §4.17 names, so the tool exercises the
refusal the device performs rather than pretending the mount is unbounded.

```text
/workspace/          runs/  notes.md
/workspace/runs/     run_7.json
```

`notes.md` holds `# notes\n\nplain text, no markup.\n` and `run_7.json` holds
`{"run":"r-7","ok":true}`. A directory carries `bytes:0`, as `04` §4.17 requires, and a file carries its
real length, so a screen asserting on the size column reads a number that matches the
body it would have read.

A mutation (`sandbox.write`, `sandbox.delete`) does not change this tree. The tool
reports what it was asked to do and forgets it, because a checker that accumulates
state stops being deterministic on the second run.

## 7. Scenarios

| Flag | Effect | What it exercises |
|---|---|---|
| `--fail-on <type>[,<type>]` | replies with the addressable protocol error instead of the correct reply | `05-bridge.md` §9 error surfacing |
| `--delay-ms <n>` | holds each reply by `n` milliseconds | UI responsiveness and the 5 s `pong` timeout |
| `--no-hello` | never sends `hello_ack` | the 5 s handshake timeout and the backoff sequence in `05-bridge.md` §4.1 |
| `--token <t>` | requires the token in `hello` | `link.rejected` and the no-retry rule |
| `--script <file>` | reads a fixed list of replies from a file instead of the table | arbitrary sequences |

`--script` is the escape hatch for orders the table cannot produce: two `agent.final`
frames for one `prompt`, a `status` burst, a malformed line. Format is NDJSON, one
object per line, sent in order; each is completed with an envelope by the tool, so
the file carries no `v`, `id` or `ts`. An entry is consumed per inbound frame, and
the script is not a permanent replacement: once it is exhausted the table answers
again, so a scripted prologue cannot silently disable the rest of the peer.

Three entry shapes exist, and only the first is a normal reply:

| Entry | Effect |
|---|---|
| `{"type": ..., ...}` | reply with that type; `reply_to` is `auto` (the inbound `id`) unless the entry names one explicitly |
| `{"wait_ms": 500}` | consume the frame and sleep, emitting nothing |
| `{"type": "raw", "data": "..."}` | write `data` to the port verbatim, bypassing `encode` |

`raw` is the only way to put a line on the wire that `encode` would refuse — a
truncated object, invalid UTF-8, an oversize line — which is what checks 1 and 2
of §5 need to be exercised at the port rather than only in a unit test.

```json
{"type":"pong","reply_to":"auto"}
```

```json
{"type":"event","kind":"boot","data":{"fw_version":"test-1.0.0","boot_id":"00000001"}}
```

```json
{"wait_ms":500}
```

## 8. Determinism and limits

Reply `id` is a per-process counter with the `d-` prefix, as `04` §2 requires for the
device, so ids are reproducible across runs. `ts` is real time: assertions MUST NOT
compare it, and `04` §2 forbids using it for ordering anyway. `run_id` is a counter,
so the first run of a process is always the same run.

No network, no clock dependency beyond `ts`, and no files written; the only read is an
optional `--script` file.

Hard limits, stated so nobody over-reads a green run:

- It does not model latency of real hardware, PSRAM pressure, or flash write time.
  Nothing here can find a timing bug in the firmware.
- It does not model the 16384-byte TX truncation rule. Replies are shaped to fit, so
  `agent.final.truncated` is always `false` and `full_bytes` never appears.
- It never sends `status` unsolicited, so the periodic refresh is untested; the
  handshake-time `status` command is answered with the constant of §6.1.
- It sends no `log`, `agent.tool`, `agent.tool_result`, `task.fired`, `gpio.edge` or
  `mqtt.message` frames, so those UI surfaces are untested by a session.

## 9. Non-goals

- Not a device simulator. No config, NVS, WiFi, scheduler or agent loop.
- No LLM, MQTT or network code of any kind.
- No second protocol implementation: it imports `protocol` rather than re-deriving
  the codec, and a change that would break that import is the wrong change.
- No golden-file fixtures. Replies are computed, so there is nothing to re-record
  when a canned value changes.
- Never a dependency of the shipped bridge or the protocol library. If a shipped
  module ever needs something from it, the something belongs in `protocol`.
