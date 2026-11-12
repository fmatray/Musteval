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

### `E_CANCELLED` from a run in flight

`07-roadmap.md` §M4 requires `cancel` to interrupt the current run and answer
`E_CANCELLED`. The reply path was implemented but had a hole: `s_cancel` was checked
before each round and around each tool call, never after the provider call returned,
so a `cancel` arriving during that call was dropped and the run completed normally.
Observed three times on this board before the fix — `agent.final` with the full
answer, tens of seconds after the cancel.

With the check added after `llm_query`, the same sequence now ends as
`agent.error`/`E_CANCELLED` with the run's `run_id`, and the following run completes
normally.

## M4.5 The tool criteria were proven live

The board reached its provider during this audit, so the criteria that need a tool
call are no longer gaps. What was observed on `/dev/ttyACM0`, in one session:

- **A tool round.** `time.now` was called and its result used:
  `agent.start` → `agent.tool` (`time.now`) → `agent.tool_result ok:true` →
  `agent.final` reading `2026-11-08T02:26:07+0100`, `rounds:1`,
  `prompt_tokens:2805`.
- **Multi-turn.** Run 1 stored `4271`; run 2 asked for it and answered `4271`, with
  `prompt_tokens:1532` — a conversation, not a re-sent question.
- **Cancel.** `cancel` during the provider call ended the run as
  `agent.error`/`E_CANCELLED` with the run's `run_id`, and the next run completed
  normally, so an aborted run leaves no half turn behind.
- **The system prompt reaches the model.** Asked who it was, the model answered
  `I am Musteval.` — the 18 bytes of `/prompts/system.md`.

### Fixed by inspection: the loop's history contract

Seven defects were found by reading board-only code, or by the board itself once a
provider was reachable. Each is fixed; the host-testable ones carry a test that
fails without the fix (`test_m4.c`, `test_m3.c`).

- The run's prompt never entered the history, so from round 1 the request carried
  tool results with no user turn, and a second run carried every earlier answer and
  no earlier question (`02-firmware.md` §4.1 step 4). It is now pushed before the
  first round and `user_prompt` stays NULL from then on.
- `agent.system_prompt` is a path (`CFG_PATH`, default `/prompts/system.md`), and it
  was being sent verbatim as the system message. It is now read from `userfs`.
- The same key had no default applied: `cfg_get` returns NULL for an absent key, so
  an unconfigured device sent **no** system message at all. Every other setting in
  the run goes through a `*_or` helper; this one did not. It now falls back to
  `/prompts/system.md`.
- The request body was 8 KiB but had to hold a ~5 KiB tool array, an 8 KiB system
  prompt and the history at once. No request could be built, so every run answered
  `request body too large` — and because history is never reset, forever, until a
  reboot. The body is now `NET_BODY_MAX` (16 KiB) with a separate 8 KiB history cap,
  and a body that still does not fit drops history until it does.
- A cancel arriving during the provider call was discarded, because the only checks
  were before the call and around each tool. The run then finished as if nothing had
  been asked (`04-protocol.md` §3.4 requires `E_CANCELLED`). It is now checked again
  immediately after the call returns.
- History trimming dropped the oldest messages blindly, which could keep a
  `role:"tool"` result whose `assistant` call had just been dropped — a shape a
  provider rejects. A trim now advances past a leading tool message, and a
  truncation that would cut the pair at the tail drops the call too.
- A full history dropped the message being appended and returned an error every
  caller ignored, where `02-firmware.md` §4.2 says truncate the oldest entry. The
  append now evicts and appends.

One more was a false refusal: `fs.list` and `fs.read` were confined to `/workspace/`
through the write-path resolver, so `/skills/`, `/prompts/` and `/tools/` were
unreachable — which also made the skill index always empty. `06-security.md` §6.1
makes both mounts listable and readable; only `fs.write`/`fs.delete` are confined.

### Tool calls, observed

The whole built-in surface was driven through the model instead of called directly,
so each line below is a real round trip: `agent.tool` → `agent.tool_result` →
`agent.final`. These answered `ok:true` with the documented result shape:

| Tool | Observed |
|---|---|
| `time.now` | `2026-11-08T02:26:07+0100` |
| `fs.list` `/workspace` | five entries, `path` and `kind` per entry |
| `fs.read` `/prompts/system.md` | the file's exact 18 bytes |
| `memory.set` / `memory.get` | round-trips `platypus`, returns `stored:true` |
| `memory.list` | the key set |
| `sys.diagnostics` | `free_heap`, `min_free_heap`, `uptime_s`, `tool_calls` |
| `todo` `init` | the phase/task tree back |
| `task.list` | `E_TOOL` "the scheduler is not implemented before M5" |

Refusals, which are criteria too, answered `ok:false` with the documented code:

- `fs.read` `/config.json` → `E_TOOL` "fs.read: config.json is not readable by a tool".
- `fs.list` `/workspace/nope_dir` → `E_TOOL` "fs.list: no such directory".
- `fs.write` with `tools.allow_dangerous: false` → `E_TOOL` naming the setting.
- `skill.read` an absent name → `E_TOOL` naming the skill.

Two real defects came out of this sweep, both fixed:

- **`fs.list` panicked the board.** `call_fs` built `store_entry_t entries[64]` on
  the stack — 4.6 KiB against the agent task's 8 KiB — so listing a directory with
  entries in it overflowed and rebooted. The serial line carried
  `A stack overflow in task agent has been detected` and no frame at all. The
  buffer now lives in PSRAM, allocated once, because a static array would spend
  4.6 KiB of the 34 KiB DIRAM margin instead.
- **A tool refusal was reported as `E_STORAGE`.** `04` §5 maps "tool refused or
  failed" to `E_TOOL`, and every `docs/tools/*.md` §Errors agrees, but
  `ESP_ERR_NOT_FOUND` fell through to the storage arm. A missing path, an absent
  skill and a missing memory key all answered `E_STORAGE`, which tells the bridge
  the flash failed. Fixed in the mapping; `E_STORAGE` is now only the quota and
  mount arm the docs reserve it for.

### Memory's documented bounds, observed as unenforced

`tools/memory.md` §Limits fixes three rules: a key is 1–15 bytes of `[A-Za-z0-9_]`,
those four prefixes are reserved, and the namespace holds at most 32 keys. The
code enforced none of them — it accepted `[a-z0-9_.-]{1,64}`, never looked at a
prefix, and never counted keys. `memory.set` also omitted the documented
`stored:true`. All four are fixed and the first two are host-tested
(`a memory key obeys the documented alphabet and length`, `a reserved memory prefix
is refused and named`), failing before the fix.

### The second board session: every delayed fix observed

The board returned on `/dev/ttyACM0` a day later. The four fixes the earlier session
had left unflashed are now observed, and the session found two more defects.

Observed on the board, in this order:

- **`sandbox.delete` recursive during a run → `E_BUSY`.** With `agent.busy: true`
  read from `/status`, `{"path":"/workspace/pt","recursive":true}` answered
  `{"code":"E_BUSY","message":"recursive delete refused while a run is in flight"}`.
- **The prompt queue's third entry → `E_BUSY` with its depth.** Three prompts
  during one run answered
  `{"code":"E_BUSY","message":"agent run in flight; queue full (2), prompt rejected","retryable":true,"detail":{"max_queue":2,"queue_depth":2}}`.
- **`tasks.run` in safe mode → `E_SAFE_MODE`**, not the unwired-scheduler message,
  while `sys.safe_mode: true` and `boot_failures: 4`.
- **`template.get {"name":"../brief.md"}` → `E_PROTOCOL`**, "name is not
  `[A-Za-z0-9._-]+.md`".
- **A `prompt` with no `text` → `E_PROTOCOL`**, "prompt.text is required",
  `retryable:false`.
- **Memory's bounds.** `memory.set sys_x` → `E_TOOL` "the sys_ prefix is reserved for
  firmware state"; `memory.set has.dot` → `E_TOOL` "key must be 1-15 bytes of
  `[A-Za-z0-9_]`"; `memory.set ok_key hello` → `stored:true`.
- **`fs.read` on an absent path → `E_TOOL`**, "fs.read: no such readable text file" —
  not `E_STORAGE`.
- **`geo.locate` → `E_TOOL`**, "geo.locate is not implemented before M6".
- **`agent.max_input_bytes`.** 3072 bytes was accepted with `input_bytes: 3072`;
  3073 was refused `E_PROTOCOL`, "prompt exceeds agent.max_input_bytes".
- **A prompt round trip.** `say pong` → `agent.final` "pong", `rounds:0`,
  `prompt_tokens:1340`.
- **Multi-turn.** Run 1 was asked to remember `4271`; run 2 answered `4271` with
  `prompt_tokens:1557`. A third run calling `time.now` answered
  `2026-11-08T09:31:24+0100`, `rounds:1`.

### The two defects this session found

Both were drifts between a documented default and the code, and both were proven by
behaviour before being fixed:

- **`agent.max_input_bytes` was 8192 in the code and 3072 in `03-config.md`.** One
  constant was doing two jobs: the documented default *and* the input buffer's cap.
  A 3500-byte and a 4000-byte prompt were accepted (`input_bytes: 4000`) where the
  documented default refuses above 3072. The default and the buffer cap are now two
  constants, and the resolution lives in `agent_settings.c` so a host test fails it.
- **`llm.max_tool_rounds` was 6 in the code and 8 in `03-config.md`.** Corrected,
  and covered by the same test.

The test asserts the resolved value, not the constant: comparing a constant to
itself would have passed with either number.

### The defect that made every prompt fail

`E_LLM_UNAVAILABLE` with `ollama: http 400: {"error":{"message":"invalid character
',' looking for beginning of value"}}` blocked this whole session. The body was not
malformed when assembled: the device's own parser accepted it, and so did Python
after the device logged it back. The board's parser, pointed at the same bytes with
a bracket-depth scan, named the byte — offset 75 of 5615, `"content":"You are
Musteval.\n"},,{`.

The cause was `agent_history_truncate(s_history, &s_history_used, 0)`, which leaves
the buffer holding `[]` with `*used == 2`. `agent_history_append` read `*used == 0`
as "nothing written yet", so it overwrote the `]` with `,` and produced `[,{…}]`.
Every run that followed a finished run sent that body, so the provider refused every
request after the first. The append now treats a buffer with no messages as empty.
`an append into an emptied history stays a valid array` fails with `[,{…}]` before
the fix.

The lesson is worth keeping: **two independent parsers accepted a body a third
rejected by a byte offset.** "It parses" is not proof of what crossed the wire.

A read-only pass over the same command paths found four more. All four were built and
host-tested in that session and have since been observed on the board, above:

- **A `prompt` with no `text` answered `E_INTERNAL`** "the agent loop is not
  started" with `retryable:true`, so a malformed frame was invited to be resent.
  `04` §4 makes a missing required argument `E_PROTOCOL`. Reaching it needed
  `agent_submit` to stop returning one code for both a full queue and a queue that
  never existed.
- **The `/tools/*.json` loader read its HTTP members from the file's root** while
  `03-config.md` §tools and `tools/web.md` put them under `http`, so every
  conforming file was dropped as malformed. `http.headers` was also emitted empty
  whatever the file said.
- **`sandbox.delete` had no `E_BUSY` guard** for the recursive form `04` §4.19's
  table promises, so the host could remove a subtree a live run was reading.
- **`tasks.run` in safe mode answered the unwired message**, not the `E_SAFE_MODE`
  `02-firmware.md` §safe mode names, so the bridge never learned the way out.

The first, second and fourth were unreachable by a host test while the code sat in
board-only `main.c`, `tools.c` and `agent.c`; each rule moved into a pure file, and
every one of them fails its new test when reverted.

### Still not reachable on this board

- **HTTP allowlist and response projection.** A declarative tool outside
  `tools.http_allow_hosts` answering `E_TOOL`, and a `response_path` subtree. The
  gate half is host-tested; the projection half needs a declarative tool, and no
  `/tools/*.json` can exist on this image yet.
- **`weather_now` and `web_search`.** `00-overview.md` §7 ships them as ordinary
  `/tools/*.json` files in a default `userfs` image that does not exist; building
  it is now provided by `bridge/tools/build_userfs.py`.
- **Peripherals.** `adc.read`, `i2c.scan`, `i2c.read`, `i2c.write` — the bus is not
  configured, so those three are not advertised at all. The pin allowlist and the
  argument bounds are host-tested.
- **`fs.put` into `/skills/` and its per-directory caps.** A bridge command, not a
  tool call, and the cap is a quota the suite covers as pure logic.
- **`memory.*` surviving a reboot.** Needs a reboot between the write and the read;
  the NVS namespace is exercised, the survival across a reset is not.

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

## M4.7 The onboarding feed

The wizard is hand-testable without credentials because a scan and a config write
need no provider. Recover from safe mode first if the board is in it.

- [X] `bin/bridge --send "/sys reset-counter"`, then `bin/bridge --send /status`,
      shows `sys.safe_mode: false`. A scan brings the station interface up itself,
      so no configured network is required for the next step.
- [X] `wifi.scan` against the board returns the networks the radio hears,
      strongest-first, each with `ssid`, `rssi` and `auth`, and `truncated` false.
      This was run as a raw `command` frame on `/dev/ttyACM0`, not through the
      wizard: 11 entries, strongest `-53`, `auth` a `04` §4.24 word. The radio heard
      more access points than it reported, the difference being the hidden and the
      duplicate-SSID ones; that collapse is asserted exactly in
      `firmware/test_host/main/test_m3.c`, where the input is known.
- [X] `wifi.status`, `wifi.disconnect`, `wifi.connect` and `wifi.reconnect` on the
      board each answer the `04` §3.9 wifi object in `04` §4.26's shape. Run on
      `/dev/ttyACM0` as raw `command` frames: `status` reported `connected` on
      `StoneBox` at `-67` dBm with `192.168.1.175`; `disconnect` answered `off` with
      `ssid`, `rssi` and `ip` all null in 24 ms and was **still** `off` 25 s later
      (§4.2's auto-reconnect did not undo it); `connect` answered `connected` in
      3 416 ms and `reconnect` in 3 428 ms, both back on the same IP. Both join forms
      returned after their own name, which is the point of the row: the reply name is
      the command asked, not a constant.
- [X] `todo.apply` (`04` §4.27) as `/todo`'s frame, on the board over `/dev/ttyACM0`
      as raw `command` frames: `view` on a fresh boot answered `{"phases":[]}`, which
      is `tools/todo.md` §State's truth and not a fault; `init` created phase `auth`
      with two tasks; `start` moved only the named task to `in_progress`, demoting
      the one it replaced; `done` then `block` on the phase left the `completed` task
      alone and set `blocker` on the other; `rm` deleted one task and kept the phase;
      a target naming nothing answered `E_TOOL` with the list untouched; and bare
      `init` cleared it. Re-driven through `bin/bridge --send`, the real command path:
      `/todo init auth`, `/todo append auth "wire jwt" "rotate key"`, and `/todo start
      wire jwt` each built the tree they name, while `/todo append` and `/todo append
      auth` were both refused host-side with the usage string instead of a frame.
      Every refusal names what is wrong rather than the old generic `todo refused`:
      `append needs a phase`, `append needs items`, `start needs a task`, `duplicate
      content`, `item is longer than 120 characters`, `phase is longer than 40
      characters`, `at most 8 phases`, `a phase needs items`, `duplicate phase name`.
      The command is the `todo` tool's own apply path, so `/todo done 3` and the
      model's `{"op":"done","task":"3"}` are one code path — asserted by
      construction, not by comparison.
- [X] `/onboard` showing that scan in the SSID dropdown beside a hand-typed field,
      and the region dropdown pre-selecting the stored zone's region. Driven on the
      board through a pty: `bin/bridge`, then `/onboard`, and step 1 drew with
      `StoneBox` in the dropdown, the manual `or type an ssid` field under it, and the
      region `Select` populated. This board has no `ntp` block, so the region fell to
      the table's first entry rather than to a stored zone; the stored-zone
      pre-selection is asserted in `bridge/tests/test_ui_onboard.py` against the board
      dump's `CET-1CEST,M3.5.0,M10.5.0/3` shape.
- [X] `Ctrl+G` advancing every step on an unconfigured device, and each step's line
      naming what that pass leaves unset. Driven in the same pty: three presses took
      the wizard `Network and time` → `Provider` → `Model` with nothing written, and
      step 1's line read `no stored zone; Ctrl+G leaves it unconfigured` — the board
      holds a `wifi.networks` entry and no `ntp` block, which is exactly the half-and-
      half case the line has to name. Asserted at the key in
      `test_pressing_pass_on_an_unconfigured_step_dismisses_it`.
- [X] The progress screen appearing while the device is asked something slow. Seen on
      the board: `reading the device's config` during the wizard's `config.get`, and
      `scanning for networks` during `wifi.scan`. `reloading the device` is asserted in
      `test_the_reload_shows_a_progress_screen_while_it_waits`, not observed on the
      board this round: every step was passed, so no reload ran.
- [X] `bin/bridge --send "/config set ntp.timezone CET-1CEST,M3.5.0,M10.5.0/3"`
      succeeds on a device whose `config.json` has no `ntp` object, reporting
      `requires_reboot: false`; `bin/bridge --send "/config get ntp"` then shows
      the key.
- [X] `bin/bridge --send "/config set wifi.networks.3.ssid x"` returns `E_CONFIG`
      and writes nothing, because a set creates a missing object but never an array
      index.
- [ ] `/onboard` step 1 with a real SSID and passphrase, the chat line reading
      `network: joined <ssid>` and `time: zone <tz> applied`, then
      `bin/bridge --send /status` agreeing with both. **Gap**: needs the credentials
      of the network, which are the reader's, not this file's. The verdicts
      themselves are asserted against canned `status` replies in
      `bridge/tests/test_ui_onboard.py`; what is unproven is that the board's real
      `reload` produces those replies.
- [ ] The provider dropdown filling `base_url` from `05` §7.1's catalog, and the
      model list arriving for a provider that publishes one. **Gap**: the fetch is an
      HTTPS request carrying the typed key and there is no key here, so the shape is
      asserted in `bridge/tests/test_providers.py` with the transport injected.
- [ ] A whole `llm.providers` array written with `token_param` and
      `extra_body: {"reasoning_effort":"medium"}`, then read back through
      `config.get`. **Gap**: `/config set` sends its value as a JSON *string*
      (`bridge/commands.py`), and an array is not a string, so the only hand route to
      this write is `/onboard`, which is the row above. The device side was proven
      over a raw frame instead: accepted, `requires_reboot: false`, read back
      verbatim, then restored to the prior array.

## Host suite

The logic that cannot be reached from the bridge is asserted here. Build first with
the command in `AGENTS.md`'s Test Strategy table: `cd firmware/test_host`, remove
`build`, then `idf.py --preview set-target linux` followed by
`idf.py -DCOVERAGE=ON build`.

- [X] `cd firmware/test_host && ./build/musteval_test_host.elf` prints
      `215 Tests 0 Failures 0 Ignored`. The M4 cases are the ones tagged `[m4]` in
      `main/test_m4.c`, and every name this file quotes is in that binary's output.
- [X] `./coverage.sh` passes its 80 % line gate, reporting 92 % over the
      host-built sources.

The image built after these fixes is 1 176 901 bytes: 37.4 % of the 3 MiB `factory`
partition, 62.6 % free. DIRAM is 296 699 of 341 760 bytes (86.82 %, 45 061 B of
headroom); IRAM is 16 384 bytes, 100 % of its budget, which is the pre-existing
figure and not a regression from this work.

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
