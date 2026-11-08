# AGENTS.md — Repo Conventions

This document defines the working rules for AI agents contributing to the Musteval codebase.

## Core Principles

1. **Doc-First Development**: No implementation change is allowed without first updating the corresponding specification in `docs/`.
2. **The Overview is Law**: `docs/00-overview.md` is the authoritative source of truth. Any drift in lower-level docs or code must be resolved in favor of the Overview.
3. **No Multirole / No Subagents**: The agent is a single-persona entity. Do not introduce multi-agent orchestrators, planning-worker splits, or recursive agent spawning.
4. **Deterministic Formatting**: Use canonical JSON (sorted keys, no trailing commas) for all config and protocol examples.
5. **Tasks Are Tracked, Not Remembered**: Any non-trivial task — more than one edit, or any command run against the repo — MUST be opened with the `todo` tool and closed before yielding. Every item ends `completed`, or `abandoned` via `drop` when it is deliberately not done; no item is left `pending`, `in_progress` or `blocked`. A question that only needs an answer is exempt. On the device this is not a convention: the agent loop enforces the same rule before it sends `agent.final` — the close-out half mechanically, the open-a-list half by prompt plus a re-prompt when a multi-round run never tracked anything (`docs/02-firmware.md` §4.1, `docs/03-config.md` §prompts, `docs/07-roadmap.md` M4).

## Engineering Standards

### Documentation
- **Language**: English only. Every document, comment, test name, commit message, and reply is written in English, whatever language the conversation is held in.
- **Format**: Use plain Markdown.
- **Visuals**: Use Mermaid.js for diagrams and ASCII tables for data. No images unless absolutely necessary.
- **Consistency**: Always use the frozen contract names for config keys, protocol messages, and module APIs.
- **Complexity**: Avoid over-engineering. Prefer the simplest possible logic that fulfills the feature requirements.

### Code
- **Language**: ESP-IDF C (Firmware), Python 3.12 (Bridge).
- **Style**: Follow ESP-IDF coding guidelines for C; PEP 8 and type hints for Python.
- **Serialization**: All communication over the USB link must use the NDJSON framing specified in `docs/04-protocol.md`.
- **Secrets**: Never echo secrets (API keys, passwords) in logs or terminal output. Use redaction.

### Tests
- **Every rule gets a test at the cheapest layer that can fail it.** A rule broken by a pure function gets a unit test; a rule that needs a board gets an on-target test. Do not pay for hardware to check a string.
- **Assert behaviour, not wiring.** A test that a function was called, that a mock echoed its input, or that a file contains a line is not a test. Assert the observable result.
- **Deterministic.** No network, no wall clock in assertions, no `sleep` where a constructor timeout will do. A flaky test is a broken test: it is fixed or deleted, never retried.
- **Doubles are named and few.** `FakeTransport` for the link, `bridge/tools/test_agent.py` (`docs/10-test-agent.md`) for the wire end to end, `llm` and `geo` stubs for firmware. A new double needs a reason the existing ones cannot serve.

## Test Strategy

Five suites, one per kind of thing that can break. Each has an owner, a command, and a gate.

| Suite | Runs on | Covers | Command |
|---|---|---|---|
| Bridge unit | host | `protocol`, `link`, `client`, `commands`, and the `ui` modules that are pure (`render`, `help`, `files`, `onboard`) | `uv run pytest bridge/tests` |
| Bridge end to end | host | `link` + `client` + `ui` over a real serial device, no board | `uv run pytest bridge/tests/e2e` (the fixture starts `bridge/tools/test_agent.py`) |
| Test agent | host | `bridge/tools/test_agent.py`: the `10-test-agent.md` §5 ladder and §6 replies, including every refusal | `uv run pytest bridge/tests/test_test_agent.py` |
| Firmware host | host, `linux` target | pure logic: NDJSON codec, config schema, tool-argument validation, skill and geo parsing, and the `store` decision helpers (`store_delete_action`, `store_move_verdict`, `store_path_is_under`, `store_is_staging_entry`) | `idf.py --preview set-target linux build`, then run the produced unity binary; coverage via `firmware/test_host/coverage.sh` (gate 80% line) |
| Firmware on target | XIAO ESP32-S3 | NVS and `secrets`, GPIO and ADC, scheduler timers, WiFi failover, brownout, boot guard | Not built yet: `firmware/test/on_target/` does not exist, so hardware-bound facts are proven today by a scripted board probe over `/dev/ttyACM0` and recorded in the reply. A suite lands with pytest-embedded |

A module that calls ESP-IDF hardware APIs is not forced onto the host: its firmware-side unit test is the on-target suite. Writing a host test for it means testing a stub, which proves nothing about the code that ships.

### Coverage gates

High coverage is a floor, not a goal. A suite at 95% that has never failed on a real bug is worth less than one at 85% that caught the last regression. Never add a test to move a number.

| Scope | Gate | Why this number |
|---|---|---|
| `bridge/protocol/` | 100% line and branch | small, pure, and every branch is a protocol rule from `docs/04-protocol.md` |
| `bridge/link/`, `bridge/client/`, `bridge/commands.py` | 90% line | state machines with named failure paths; the tail is unreachable I/O error branches proven on target |
| `bridge/ui/` | no line gate | behaviour is asserted through Textual's pilot; line coverage of event handlers is noise |
| `bridge/tools/test_agent.py` | 90% line | the device double is what e2e trusts; a §5 rung it skips turns a real protocol bug into a mysterious bridge timeout |
| firmware, host-testable modules | 80% line | C error paths are long; the remaining 20% is usually ESP-IDF glue |
| hardware-bound modules | no line gate | coverage measured against a stub is a measurement of the stub |

Enforcement: `pytest-cov` for Python, and `firmware/test_host/coverage.sh` for the firmware build, which runs `gcovr --fail-under-line 80` over a build made with `idf.py -DCOVERAGE=ON build`. A gate that is not enforced by a command is a suggestion, so both run in the same step as the suite.

### Non-regression rules

1. **Every bug fix ships a test that fails before the fix and passes after, in the same commit.** If such a test is genuinely impossible, the commit body says what was verified instead and why the test could not exist.
2. **A test is never weakened or deleted to make a change pass.** If an assertion was wrong, it is corrected in its own commit whose body explains why the old expectation was wrong. Re-pinning an assertion to whatever the code now does is how a suite stops meaning anything.
3. **The docs are the fixture corpus.** A test decodes every `json` example under `docs/04-protocol.md` §3 through `protocol.decode_line` and asserts the decoded type matches its section heading. A protocol edit that breaks the codec fails a test, not a reviewer's memory.
4. **Milestone acceptance suites never regress.** A red acceptance test at M(n) blocks M(n+1). It is never marked skipped, xfail'd, or moved to a later milestone.
5. **The codec is fuzzed.** Random bytes, truncated lines, a 5 KiB line, invalid UTF-8 and deeply nested valid JSON must raise `ProtocolError` or decode cleanly. Never a crash, never a hang, never a partially consumed line that desynchronises the stream.
6. **Cross-doc invariants are tests, not review comments.**

| Invariant | Test |
|---|---|
| `04` §2.1's device `→` bridge set equals `05` §10's row set | docs test |
| every `04` §3 example decodes to the type in its own heading | docs test |
| every built-in tool's registry schema equals its `**Schema sent to the model**` fence in `docs/tools/` | docs test |
| every `config.json` example in `03` satisfies the bounds in its `## config.json` §Full schema table (a test-side schema encodes that table) | docs test |
| the `05` §6.2 keybinding table equals the `Ctrl` bindings on each screen class | docs test |
| the `09` grep laws: `textual` only under `bridge/ui/`, `pyserial` only under `bridge/link/` | import-graph test, once `bridge/` exists |
| the `08` dependency law: no same-layer import except the seven enumerated edges (five at L0, two at L2) | source-scan test, once `firmware/` exists |

The last two are contracts today and tests the day the directories exist. They are listed so they are not lost at the moment they become checkable.

## Documentation Updates

A change is not done until the document it contradicts is corrected in the same commit. Splitting them leaves a commit where the spec and the code disagree, and that is the one state no bisect survives.

- **Doc-first** (Core Principle 1): the specification edit lands before or with the implementation, never after.
- **One owner per fact.** Never restate a frozen fact in a second document. Edit the owner and point at it from everywhere else.

| Fact | Owner |
|---|---|
| Requirements, decisions, glossary | `docs/00-overview.md` |
| Hardware, pinout, power | `docs/01-hardware.md` |
| Firmware module APIs, task model, boot | `docs/02-firmware.md` |
| Config and task schemas | `docs/03-config.md` |
| Frames, envelope, error codes, limits | `docs/04-protocol.md` |
| Bridge screens, keys, slash commands | `docs/05-bridge.md` |
| Threat model, secret handling | `docs/06-security.md` |
| Milestones, acceptance criteria | `docs/07-roadmap.md` |
| Firmware arrangement | `docs/08-code-architecture.md` |
| Bridge arrangement | `docs/09-bridge-architecture.md` |
| Test agent | `docs/10-test-agent.md` |
| Per-tool contracts | `docs/tools/<domain>.md` |

- **Renumbering shifts `§` references in other files.** After moving a section, grep the file's own name across the repo and fix every `§` that pointed at the old number. A dangling `§7` reads as valid and is a silent lie.
- **A new document** is added to `README.md`'s index, to `docs/00-overview.md` §9 if it carries a requirement, and to any doc that should point at it.
- **No placeholders.** `TBD`, `TODO`, `FIXME` and `XXX` are forbidden. When a fact is not known, write `[ASSUMPTION]` and state the consequence of being wrong.

## Git

The repository was re-initialised, so these rules define the convention from the
first commit rather than describing a history. There is no earlier scheme to
imitate or preserve. The default branch is `main`; `master` does not exist locally
or on the remote.

- **One logical change per commit.** A code slice, its test and the doc it contradicts are one commit. Two unrelated documents are two commits.
- **Subject**: `Add ...`, `Fix ...`, `Document ...`, `Remove ...`. Imperative mood, capitalized, no trailing period, 72 characters maximum.
- **Body** when the subject cannot carry the why: what changed, why, and which frozen contract moved. Wrap at 80.
- **Never commit a red tree.** The Verification Workflow below runs before `git commit`, not before `git push`.
- **Stage paths, not `git add -A`.** Never commit `.venv/`, `__pycache__/`, `build/`, `managed_components/`, `sdkconfig.old`, coverage output, editor files, or any secret.
- **Never rewrite shared history.** No `--amend` and no `rebase` on pushed commits. Unpushed local history may be tidied before it leaves the machine.
- **Doc and code for the same frozen name move together**, per Documentation Updates. If the doc edit cannot ship yet, the code change waits.

## Verification Workflow

Run this before `git commit`. Every step is mechanical; none is a matter of opinion.
A step that cannot run yet is named in the commit body, not silently skipped.

1. **Doc gates.** Every `json` fence parses on its own (or, for an NDJSON fence, on
   every line). Every relative link and every `#anchor` resolves using the GitHub
   slugger: `re.sub(r'[^\w\- ]', '', heading.strip()).replace(' ', '-').lower()`.
   Every bare-fence ASCII mockup line is width-stable within its own block.
2. **Hygiene.** `grep -rnE '\b(TBD|TODO|FIXME|XXX)\b' --include='*.md' .` returns
   exactly two hits, both in this file and both the rule text that quotes the four
   tokens in order to forbid them. Any other hit, anywhere, is a violation: the count
   is fixed precisely so this file cannot become a blind spot where a real placeholder
   hides. No emoji. Prose uses `→`, never `->`.
3. **Requirement mapping.** Every row in `docs/00-overview.md` §9 still names a
   document that exists and a section that answers it.
4. **Suites.** The affected suites from the Test Strategy table pass, along with the
   coverage gates that apply to the touched packages. A change to a frozen contract
   runs all four.
5. **Fit.** Every change under `firmware/` is followed by `idf.py size`, and the numbers
   go in the reply. The green build already gates fit — `app` depends on
   `app_check_size` (`esptool_py/CMakeLists.txt`, active because
   `CONFIG_APP_BUILD_TYPE_APP_2NDBOOT=y`) and the bootloader on `bootloader_check_size`,
   both of which fail on overflow — so this step is not a second gate. It exists because
   "it fits" without the figure is a claim, and because the app has a fixed 3 MiB
   `factory` partition: the trend matters long before the margin runs out. Report image
   size against that partition, plus DIRAM and IRAM headroom.
6. **Smoke.** Run the thing. A test that has never been observed failing is a
   hypothesis; a feature that has never been observed working is a claim. For UI
   work, that means the actual surface, not a screenshot review.
7. **Clean tree.** No scratch scripts, no unused scaffold, no coverage output, no
   `.env`. Delete what was only there to help.

A commit that touches a frozen contract without moving the doc, or moves the doc
without the code, fails step 3 or step 4 and does not get committed.
