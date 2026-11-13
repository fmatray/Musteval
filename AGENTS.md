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
| Firmware host | host, `linux` target | pure logic: NDJSON codec, config schema, tool-argument validation, skill and geo parsing, and the `store` decision helpers (`store_delete_action`, `store_move_verdict`, `store_path_is_under`, `store_is_staging_entry`) | `idf.py --preview set-target linux build`, then run the produced unity binary; coverage via `firmware/test_host/coverage.sh` (gate 97% line) |
| Firmware on target | XIAO ESP32-S3 | NVS and `secrets`, GPIO and ADC, scheduler timers, WiFi failover, brownout, boot guard | Not built yet: `firmware/test/on_target/` does not exist, so hardware-bound facts are proven today by a scripted board probe over `/dev/ttyACM0` and recorded in the reply. A suite lands with pytest-embedded |

A module that calls ESP-IDF hardware APIs is not forced onto the host: its firmware-side unit test is the on-target suite. Writing a host test for it means testing a stub, which proves nothing about the code that ships.

### Coverage gates

High coverage is a floor, not a goal. A suite at 95% that has never failed on a real bug is worth less than one at 85% that caught the last regression. Never add a test to move a number.

Every scope is gated at **97% line**. A branch gate applies only where the table says so.

| Scope | Gate | Why this number |
|---|---|---|
| `bridge/protocol/` | 100% line and branch | small, pure, and every branch is a protocol rule from `docs/04-protocol.md` |
| `bridge/link/`, `bridge/client/`, `bridge/commands.py` | 97% line and branch | state machines with named failure paths; the remaining 3% is I/O error branches that only a real port can provoke |
| `bridge/ui/` | 97% line and branch | the pilot drives real event handlers, so an uncovered handler is an untested screen; the tail is Textual dispatch the framework owns |
| `bridge/tools/` | 97% line | the device double and `build_userfs.py` are what e2e trusts; a skipped rung turns a protocol bug into a mysterious bridge timeout |
| firmware, host-testable modules | 97% line | C error paths are long, but an untaken branch is still an unread refusal; ESP-IDF glue is the documented exception below |
| hardware-bound modules | 97% line | measured on the target, not against a stub: the on-target suite is what counts here |

`bridge/__main__.py` is excluded: it is argument parsing and process setup, and its behaviour is asserted end to end by `bridge/tests/e2e/`.

A scope that cannot reach the gate is not exempted by editing this table. Either the code loses branches that no caller can reach, or the gap is named here with the reason it is unprovable — a silent lower number is how a gate stops meaning anything.

Enforcement: `bridge/coverage.sh` for Python and `firmware/test_host/coverage.sh` for the firmware build, which runs `gcovr --fail-under-line 97` over a build made with `idf.py -DCOVERAGE=ON build`. **Both are commands, not intentions.** A gate that is not enforced by a command is a suggestion, so both run in the same step as the suite, and a change that lowers a scope below its gate fails that step.

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

## Lessons

Each rule below is a bug this repository has already shipped, caught in review rather
than by a gate. They are here because the same mistake recurred across separate
changes; read them as the failure modes this codebase actually has.

- **A report is a hypothesis; the command is the fact.** A delegated slice reported
  "244 tests pass, coverage 87%". The suite held 238 tests, six of them failing, and
  coverage was 83%: the slice had edited fixtures it never re-ran. Run the suite
  yourself before believing any claim about it, including your own from ten minutes
  ago.
- **A message string is copied from the source, never from memory.** An acceptance
  record listed `at most 128 tasks`; the code emits `at most 32 tasks`, and the
  record also omitted four refusals that existed. `grep` the format string, then
  transcribe it.
- **An error path is documented by reading the branch, not by naming the code.**
  `docs/tools/todo.md` promised `E_TOOL` for an oversize result long after the
  implementation had changed to a successful result carrying a note — because the
  documented behaviour was the plausible one, not the coded one.
- **A documented argument list is derived from what the code reads.** The `04` and
  `05` catalogue rows for `todo.apply` listed `content`, `from` and `to`, which no
  firmware function reads, and omitted `list`, which it does. Enumerate the `arg_*`
  and `json_get` calls, then write the row.
- **An assertion compares against the real bound, not a literal that fits.** A test
  declared `char why[96]` for a message the code can make 156 bytes long, so the
  assertion failed on a correct message. Use the defining constant — `TOOLS_ERR_MAX`
  — so the test tracks the bound instead of a remembered copy of it.
- **A comment that explains why an invariant holds goes stale when the invariant
  moves.** `op_init_nested` carried a comment claiming pre-validation is what makes
  `init` atomic, while the real mechanism was a scratch commit in `todo_apply`. When
  atomicity moves, the comment moves in the same change or is deleted.
- **One owner per file while work is in flight.** Two agents editing
  `bridge/tools/test_agent.py` at once silently dropped one side's nested-`list`
  handling, and it had to be found by reading the diff. Claim files explicitly before
  editing, and review every foreign diff line by line before staging it.
- **`set-target` belongs to `firmware/test_host/`, not `firmware/`.** Running it under
  `firmware/` rewrites `sdkconfig` and `dependencies.lock` to the `linux` target and
  the board build stops matching. The board target is `esp32s3`; only the host suite
  switches.
- **An acceptance record is re-run, not preserved.** A recorded `/todo init auth`
  became a refused command when the verb gained a required phase. When the contract
  moves, the recorded evidence is re-executed and rewritten, even though the old
  transcript still looks valid.
- **A gate with no command is a comment.** `AGENTS.md` specified per-package Python
  coverage gates that nothing enforced; `pyproject.toml` had no `fail_under` and no
  script ran `--cov-fail-under`. If a number in this file is not checked by a command
  you can run, it is not a gate.
- **A coverage number is measured on the tree that will be committed.** Two runs were
  read as facts while a file underneath them was being edited: one parsed the new
  `modals.py` AST against the old bytecode and reported 211 missed lines for a file
  with 11, and a later `-k`-filtered run measured only what that filter imported. A
  stale `coverage.json` is discarded, never interpreted — check the file's mtime
  against the last edit before reading a single number from it.
- **An unreachable guard costs the gate, and deleting it is the fix.** Four guards in
  `bridge/ui/modals.py` and one in `client/session.py` could not be entered by any
  caller: a copy-pasted `_choices`, an attribute written and never read, base-class
  stubs every subclass overrides, and a `REJECTED` re-check that an earlier `except`
  already answered. Each was reachable only from a test written to reach it. The gate
  pays for behaviour, so the branch goes and its reason is named in a comment.

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
   runs all four. The gates are two commands and both must exit 0: `sh
   bridge/coverage.sh` from the repo root, and `firmware/test_host/coverage.sh` from
   `firmware/test_host/` after a `-DCOVERAGE=ON` build. Run them; do not infer a
   number from a previous run, because the code moved since.
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
7. **Static analysis.** `semgrep scan --config p/security-audit --error bridge firmware`.
   A finding is a red step; `--error` makes the exit code do the gating, so the step is
   not a report to be read and shrugged at. `p/security-audit` is the one pack that
   covers both of this repo's languages, C and Python, and the scan takes about ten
   seconds. The roots are named rather than left to the default walk so the step
   cannot start scanning a directory that appears at the root later. Untracked
   tooling (`.agents/`, `.omp/`) is outside both roots and semgrep reads only what git
   tracks.
   A `PartialParsing` error is not a finding: semgrep's C parser cannot read a
   `PRIu32`/`PRId64`/`PRIx32` string concatenation, which several sources here use, and
   the exit code stays 0 with them present. A new error of any other type is a red step.
   The pack is fetched from the registry on every run — semgrep keeps no local rule
   cache — so the step needs the network, and a machine without one is the case `A
   step that cannot run yet is named in the commit body` already covers. It is a
   `pipx` install, not a project dependency, because it is a check on the sources,
   not something they import.
8. **Clean tree.** No scratch scripts, no unused scaffold, no coverage output, no
   `.env`. Delete what was only there to help.

A commit that touches a frozen contract without moving the doc, or moves the doc
without the code, fails step 3 or step 4 and does not get committed.
