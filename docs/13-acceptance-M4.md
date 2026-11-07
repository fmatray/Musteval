# Manual Acceptance, M4

`07-roadmap.md` §M4 holds the criteria; this file is the hand-run procedure that
exercises what can be exercised against a real board today. Each action is a
checkbox and names the surface it touches.

The honest summary: **the tool surface is reachable only through the model.** No
command, and no frame the bridge can send, asks the device to run a tool. Every tool
call arrives as a `tool_calls[]` entry in a provider reply, so a criterion about
`fs.write`, `gpio.read`, `memory.*`, `todo` or `sys.diagnostics` needs a live
provider — a WiFi network the board can join and a resolvable `api_key_ref`. The
board this file was written against has neither, so those criteria are stated as
gaps below, with the milestone or setup that closes them, rather than given a
procedure that would not prove them.

What *is* hand-testable without credentials is the run's skeleton: that a run
serializes, that a second prompt is refused, that the budget refuses before a
socket is opened, that `hello_ack` advertises the frozen tool surface, that the
frames carry the shape they are documented to carry, and that a `cancel` with
nothing in flight says so. Those are proven below on the board, and the logic
underneath all of them is asserted in the firmware host suite.

Run from the repository root, with the board on `/dev/ttyACM0` and the venv
present (`.venv/bin/python`). Commands are shown exactly as typed.

## Preconditions

- [X] `bin/bridge --send /status` prints a JSON object. If it does not, the board
      is not on the link and nothing below will work.

Read `sys.boot_failures`. Every boot increments it, and only `reload` or
`sys.reset_counter` clears it, so a run of reboots trips safe mode on the fourth
boot, and safe mode skips the radio, the clock, the model and `agent_start`:

- [X] `bin/bridge --send "/sys reset-counter"` answers `{"previous":N,"reset":"boot_failures","safe_mode":false}`.

- [X] `bin/bridge --send /status` shows `sys.safe_mode: false`.

A run started in safe mode answers `E_SAFE_MODE` rather than running, so every
check below a `safe_mode: true` reading proves nothing about M4.

## M4.1 Serialized runs: `E_BUSY`

The queue holds two prompts behind the one being run, so a burst of four is the
shortest burst that overflows it (`agent.h` `AGENT_MAX_QUEUE`).

- [X] Send four prompts back to back, e.g. four times
      `bin/bridge --send "hi"` from four shells, or one session writing four
      `prompt` frames.
- [X] Exactly one of them answers
      `{"code":"E_BUSY","message":"agent run in flight; queue full (2), prompt rejected","retryable":true}`
      with `detail: {"max_queue": 2, "queue_depth": 2}`.

Measured here: the fourth of four answered `E_BUSY` while the first three ran in
order — one `agent.start` and one terminal frame each, never two `agent.start`
before a terminal frame. That ordering is the "serialized" half of the criterion:
runs do not interleave.

## M4.2 The budget refuses before the socket

With `llm.max_calls_per_hour: 2` and the hour window already spent, the next
attempt answers `E_LLM_RATE` without opening a socket. The board's
`/config.json` has no `llm` limits, and a typed set cannot create one, so the
object is written whole:

- [X] Set the whole `llm` object with `max_calls_per_hour` smaller than
      `llm.calls_hour` (see `/status`), then `/reload`.
- [X] `bin/bridge --send /status` reports the new `llm.limit_hour`.
- [X] `bin/bridge --send "hi"` answers
      `{"code":"E_LLM_RATE","message":"call budget exhausted","retryable":true}`.

Measured here: `limit_hour: 2` after `reload`, and every prompt afterwards answered
`E_LLM_RATE` with an `agent.start` before it and no `agent.tool` between — a refusal
reached before any provider was contacted.

`max_calls_per_hour: 0` disables the window (`03-config.md` §llm), which is how to
take the budget out of the way when checking a different failure:

- [X] With `max_calls_per_hour: 0` and `max_calls_per_day: 0`, a prompt reaches the
      provider chain and fails there instead, here
      `{"code":"E_LLM_UNAVAILABLE","message":"p1: ESP_ERR_INVALID_STATE"}`.

## M4.3 The advertised tool surface

`hello_ack` carries the names the device will accept, which is the frozen set of
`00-overview.md` §7 minus the tools that are conditionally present.

- [X] `bin/bridge --send /status` and read the `hello_ack` line, or run the bridge
      and read the Chat header.
- [X] Twenty-two names are advertised, in §7 order, from `gpio.read` through
      `sys.diagnostics`.
- [X] `i2c.scan`, `i2c.read` and `i2c.write` are absent, because they are present
      only when the bus is configured (`tools/i2c.md`), and this board has no
      `i2c` block in `/config.json`.
- [X] `dangerous_tools` is `false` and `streaming` is `false`.

`dangerous_tools: false` is the read-only half of the write gate: the names are
advertised, and the gate refuses them at call time rather than hiding them, which
is `06-security.md` §6.

## M4.4 Frame shapes

- [X] A prompt that starts a run emits `agent.start` with `run_id`, `reply_to`,
      `source: "prompt"`, `trigger: "manual"`, `input_bytes` and `queue_depth`.
- [X] A prompt that cannot run emits `agent.error` with `code`, `message`,
      `retryable`, `run_id` and `reply_to`.
- [X] Every emitted frame carries an `id` matching `[A-Za-z0-9._-]{1,32}`.

The last point is a defect that was found and fixed on this board: the agent's five
emitters passed `NULL` as the frame id, `link_emit_body` rejects that, and the frame
was dropped silently. A run emitted `agent.start` and then nothing, which reads
exactly like a hang. Ids are now `a-<n>`, distinct from the link task's `d-<n>`.

- [X] `bin/bridge --send /cancel` with no run in flight answers
      `event {kind: "cancel.noop", data: {reason: "nothing to cancel"}}`.

### Gap: `E_CANCELLED` from a run in flight

`07-roadmap.md` §M4 requires `cancel` to interrupt the current run and answer
`E_CANCELLED`. Every run on this board fails within milliseconds — at the budget
or at the provider chain — so there is no window in which a `cancel` can land
mid-run. Reaching it needs a provider that answers slowly enough to be interrupted:
a joined network and a resolvable key. The reply path itself is implemented
(`agent.c` checks `s_cancel` before each round and each tool call) and is not
provable by hand here.

## M4.5 The tool criteria need a live provider

Each criterion below is a property of a tool call, and a tool call exists only
inside a provider reply. They are listed so a reader with credentials knows what to
run, and marked as gaps because this board cannot reach them.

### Gap: tool round, gating, path confinement, quota

With a network and a key:

- `time.now` and `gpio.read` execute and their results appear as
  `agent.tool_result` between `agent.tool` and `agent.final`.
- `gpio.write` answers `E_TOOL` while `tools.allow_dangerous` is `false`.
- `fs.write` to `/config.json`, to `/workspace/../config.json` and to
  `/workspace/a/../../config.json` each answers `E_TOOL`; `/workspace/sub/x.txt`
  succeeds; `/workspace/./a//b.txt` resolves to `/workspace/a/b.txt`.
- A write that would exceed 64 KiB or 64 files answers `E_STORAGE` and leaves the
  existing file intact.

The rules underneath are host-tested: `tools_gate.c` for the gate and the host
allowlist, `tools_spec.c` for argument validation, and `store_delete_action`,
`store_move_verdict`, `store_path_is_under` and `store_is_staging_entry` for the
sandbox. What is not proven is a live round trip.

### Gap: HTTP allowlist and response projection

A declarative tool pointed at a host outside `tools.http_allow_hosts` answers
`E_TOOL`; a `response_path` returns only that subtree, and an unresolvable one is
`E_TOOL` with no partial body. The allowlist half is host-tested
(`the gate allowlists http.get by exact host`, `the gate extracts the host and
refuses plain http`); the projection half needs a body from a network.

### Gap: `weather_now` and `web_search`

`00-overview.md` §7 ships these two as ordinary `/tools/*.json` files in the default
`userfs` image. That image does not exist yet, and building it is
`07-roadmap.md` §M5's deliverable (`bridge/tools/build_userfs.py`). So neither tool
is on this board, and neither criterion — a keyless result, a `brave_key` refusal,
no secret in any frame — can be reached. This is a documentation inconsistency to
resolve when the image lands: §7 lists them under M4's surface, §M5 owns the
mechanism that puts them there.

### Gap: peripherals

`adc.read` on D0/D1/D3 with raw and millivolts; `i2c.scan` finding a device on a
configured bus; `i2c.read` capping at 64 bytes; `i2c.write` observed by a following
`i2c.read`, refused at `allow_dangerous: false`, and a NACK surfacing `E_TOOL` with
the address in `detail`. All are tool calls, and the bus is not configured on this
board, so three of the twenty-five built-ins are not even advertised. The pin
allowlist and the argument bounds are host-tested (`the pin allowlist admits header
pins and nothing else`, `tools_validate enforces the ADC pin allowlist`,
`tools_validate enforces the i2c bounds`).

### Gap: memory, diagnostics, progress, close-out, import

`memory.*` round-trip across a reboot, the `tools.memory_max_bytes` cap, the
reserved-prefix refusal and the "no path into memory from `fs.read`" isolation;
`sys.diagnostics` per-scope subsets and the absence of a secret in them; the
`todo` ops, their bounds, the one-`in_progress` rule, the close-out re-prompt and
`todo_open`; `fs.put` into `/skills/` with its per-directory caps. Every one is
either a tool call or a consequence of a run, so every one is behind the provider.
`todo`'s rules are host-tested in `test_m4.c`, including the close-out verdicts and
the normalization rule; what those tests cannot prove is that a live run reaches
`agent.final` with `todo_open: false` for the right reason.

## M4.6 `reload` re-runs the services

A `reload` after a boot-time setting changed used to clear the safe-mode flag and
nothing else, leaving `safe_mode: false` while the radio, the clock, the model and
the agent loop were still not started — every prompt then refused for a reason no
frame named.

- [X] After `/reload`, `bin/bridge --send /status` shows `sys.safe_mode: false` and
      a prompt reaches the provider chain rather than answering `E_SAFE_MODE`.

- [X] A boot that trips safe mode answers `prompt` with
      `{"code":"E_SAFE_MODE","message":"boot tripped safe mode; reload after fixing the cause","retryable":false}`,
      which is the code `04-protocol.md` §4.11 documents and which was never emitted
      before.

## Host suite

The logic that cannot be reached from the bridge is asserted here. Build first with
the command in `AGENTS.md`'s Test Strategy table: `cd firmware/test_host`, remove
`build`, then `idf.py --preview set-target linux` followed by
`idf.py -DCOVERAGE=ON build`.

- [X] `cd firmware/test_host && ./build/musteval_test_host.elf` prints
      `156 Tests 0 Failures 0 Ignored`. The M4 cases are the ones tagged `[m4]` in
      `main/test_m4.c`, and every name this file quotes is in that binary's output.
- [X] `./coverage.sh` passes its 80 % line gate, reporting 88 % over the
      host-built sources.

The binary takes no filter argument, so it runs every case; `grep` its output for a
name above to see the individual line.

## Do not

- Do not reboot more than three times without `bin/bridge --send "/sys reset-counter"`.
  The fourth boot enters safe mode and skips the radio, the clock, the model and
  the agent loop.
- Do not read a silent link after a prompt as a hang. An `agent.start` with no
  terminal frame is a defect, and it was one: a frame with no `id` is dropped by
  `link_emit_body` without a log line. Check `/status` for `agent.busy` and
  `agent.run_id` before concluding anything about the run.
- Do not treat `llm.calls_hour` as meaningful after a boot loop. A run that spent
  the chain in a loop left the counter at a number far above the hourly cap; the
  counter is persisted, so it survives the reboot that fixes the loop.
- Do not test the tool surface by typing a tool name at the bridge. There is no such
  command; a bare line is a prompt.
