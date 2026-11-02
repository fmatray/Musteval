# 09 — Bridge Architecture

Status: derived from `docs/00-overview.md` (authoritative) and `docs/05-bridge.md`.
This document does not re-decide any frozen surface: the frame catalogue lives in
`04-protocol.md`, the screen behaviour and keybindings in `05-bridge.md`. It decides
**how the bridge code is arranged**: packages, dependency direction, extension
points, and what may be lifted out of it.

Scope: the host process under `bridge/`. The firmware is covered by
`08-code-architecture.md`; the two documents share a shape on purpose. A reader who
has not read `08` needs nothing from it here.

---

## 1. What this document is

Two documents describe the bridge and they answer different questions.

| Document | Question | Owns |
|---|---|---|
| `05-bridge.md` | What does the bridge *do*? | Screens, keybindings, slash commands, handshake and heartbeat timings, error banners, secrets handling |
| `09-bridge-architecture.md` (this) | How is it *arranged*? | Packages, import direction, public surfaces, ports, test seams, reusability |
| `04-protocol.md` | What goes on the wire? | Envelope, NDJSON framing, the 21 message types, the 16 error codes, limits |

The split matters because behaviour survives a rewrite and arrangement does not. A
new screen adds no layer. A new layer would change every import in the tree, which
is why it needs an argument here and a keybinding change does not.

This document references message types (`hello`, `hello_ack`, `agent.final`,
`event`, `command.result`) and error codes (`E_BUSY`, `E_PROTOCOL`, `E_LLM_RATE`)
by name only. Their fields and semantics are frozen in `04-protocol.md`;
restating them here would create a second copy that can drift.

---

## 2. Layering

Five layers, strictly one-directional. A package belongs to exactly one layer and
may import only strictly lower layers, or a port it owns.

| Layer | Package | Responsibility | May import |
|---|---|---|---|
| L0 | `bridge/protocol/` | **The protocol module.** Envelope build, parse and validate; NDJSON line framing; type and error-code enums; `reply_to` correlation ids. Pure functions and dataclasses. | stdlib `json`, `dataclasses`, `enum` only |
| L1 | `bridge/link/` | `Transport` ABC; `SerialTransport` (pyserial, reader thread into an asyncio queue); `FakeTransport` for tests. | `protocol`, pyserial |
| L2 | `bridge/client/` | `Session`: request/reply correlation, heartbeat and ping-pong, reconnect backoff, link state machine, frame-sink dispatch. Framework-agnostic. | `protocol`, `link` |
| L3 | `bridge/ui/` | Textual `App`, screens, widgets. Consumes `Session` events. | `protocol`, `client`, Textual |
| L4 | `bridge/__main__.py` | CLI flags, wiring, the `uv run bridge` entry. | everything |

`bridge/commands.py` sits at **L2** beside `client/`: slash-command string →
`list[Frame]`, pure, importing `protocol` only. It does not import Textual, `link`
or `client`, which is what makes it testable without an App and is why it is not
filed under `ui/`.

`bridge/tools/` is **off-tree and not a layer.** It holds maintainer scripts
(`build_userfs.py`, `05-bridge.md` §11) that run on a developer machine, and it may
import anything or nothing. It ships with neither the app nor the library, so it is
outside the dependency law rather than exempt from it.

```
L4   __main__.py    CLI flags, wiring, entry point
      |
L3   ui/            Textual App, screens, widgets
      |
L2   client/        Session: correlation, heartbeat, backoff
     commands.py    slash command -> frames (leaf at L2)
      |
L1   link/          Transport ABC, SerialTransport, FakeTransport
      |
L0   protocol/      Frame codec, enums, ids
```

> **A module may import only strictly lower layers, or a port it owns.**

Three consequences, each checkable without reading the code:

1. **No upward imports.** `protocol` imports nothing from the tree. `ui` is
   imported by `__main__` and by nothing else.
2. **`commands.py` is a pure leaf below `client`.** It imports `protocol` only, and
   is imported by `ui` and by no other package. `client` does not import it: the
   session moves frames, it does not author them, so a same-layer `client` →
   `commands` edge would be the first illegal one.
3. **`__main__` is the only composition root.** No package constructs another
   package's dependencies; `__main__` builds `SerialTransport`, hands it to
   `Session`, then hands `Session` to the App. This is also how `ui` reaches
   `commands.py` without either importing the other.

---

## 3. The protocol module

`bridge/protocol/` is the centre of the tree. It is one package, not a file,
because it has four kinds of knowledge and each must be testable alone: the
envelope, the line framing, the enumerations, and id allocation.

It is **pure**. No socket, no thread, no file, no environment variable, no global
counter reached from another thread, no logging side effect. A function takes bytes
or a `Frame` and returns bytes or a `Frame`. That is the whole contract, and it is
what allows the same package to run inside a TUI, a one-shot CLI, a pytest run,
or a mock device.

```python
# Illustrative public surface, not an implementation.
from dataclasses import dataclass

class ProtocolError(Exception):
    code: str    # one of the 16 frozen codes, e.g. "E_PROTOCOL"
    detail: str

@dataclass(frozen=True)
class Frame:
    type: str                  # one of the frozen type names
    id: str                    # "b-<counter>" for bridge-originated frames
    ts: int                    # 0 when the clock is unsynced
    body: dict                 # type-specific members, catalogued in 04
    reply_to: str | None = None

def new_id(prefix: str) -> str: ...            # counter is module state, not I/O
def encode(frame: Frame) -> bytes: ...         # one line, trailing b"\n"
def decode_line(line: bytes) -> Frame: ...     # raises ProtocolError
```

The constants live here too, because they are protocol facts rather than policy:
`VERSION = 1`, `MAX_RX_BYTES = 4096`, `MAX_TX_BYTES = 16384`, and the two
direction allowlists that `04-protocol.md` §2.1 defines. Direction checking is a
decode concern: a `pong` arriving bridge-to-device is malformed input, not a
session bug.

### 3.1 What it explicitly does not own

- **The frame catalogue.** `04-protocol.md` §3 owns field names, required members
  and examples. `protocol` exposes them as validated dataclasses or plain dicts; it
  does not define new ones.
- **Retry policy.** Whether a failed `command` is retried, how many times, and
  whether an error code is retryable at all are `client` decisions.
- **Rendering.** `protocol` may say a line is a `log`; it may not say the line is
  red, truncated, or scrolled. Presentation is `ui`.
- **Time.** A `Frame.ts` is passed in by the caller. The codec does not read the
  clock, which is the reason a golden-file test can compare bytes exactly.

### 3.2 Redaction lives here, not in `ui`

`05-bridge.md` §8 permits `--json` to log the bridge's own frames, and two
bridge→device frames carry a credential: `hello.token` and `secret.set.value`.
Redaction therefore has to run on the way *out*, at the layer that owns the byte
stream, which is `link` — below `ui` and below `client`. Putting it in `ui` would
break the law, because `link` (L1) would then depend on an L3 package.

So redaction is a **pure L0 function in the codec package**, beside `encode`:

```python
def redact_line(line: bytes) -> bytes: ...
```

It takes a raw line and returns that line with credential-bearing members replaced
by a placeholder. `link` calls it on every logged line and never learns which types
exist — the type names stay inside `protocol`.

This keeps one rule with one implementation. `ui` still masks input and refuses to
render a `secret.set` result (`05-bridge.md` §8), but that is display hygiene, not
the logging boundary, and the two must not be merged: a frame can be logged without
ever being displayed.

**Owner summary**, so no layer is ambiguous about it:

| Concern | Owner | Mechanism |
|---|---|---|
| Deciding what a credential is | `protocol` (L0) | `redact_line()` |
| Writing the log, when `--json` is on | `link` (L1) | Called on every line it reads or writes, before the sink |
| Parsing the flag | `__main__` (L4) | Passes a bool into `link` at construction; holds no protocol logic |
| Masking input, hiding results | `ui` (L3) | Display only, never the logging boundary |

`client` takes no part: it never handles raw bytes, and a frame it can see has
already been redacted on the way in.

---

## 4. Module anatomy

Every package has the same four parts: a public surface, a private implementation,
a test seam, and a list of things it must not do.

### 4.1 `bridge/protocol/` (L0)

Public surface: `Frame`, `ProtocolError`, `encode`, `decode_line`, `redact_line`,
`new_id`, `VERSION`, `MAX_RX_BYTES`, `MAX_TX_BYTES`, the type enum and the
error-code enum.

Must not: open the port, sleep, retry, print, read config, or import anything above
L0. It may not depend on `link`, even for the `Transport` type.

### 4.2 `bridge/link/` (L1)

Public surface: the `Transport` ABC (`open`, `close`, `read_line`, `write_line`)
and the two implementations. `read_line` is `async`; `SerialTransport` runs
pyserial blocking reads in one dedicated thread and hands complete lines to an
`asyncio.Queue`. `FakeTransport` is scripted from a list of inbound lines and
records outbound ones.

Debug logging happens here, because this is the only layer that sees bytes in both
directions. With `--json` set, each line is passed through `redact_line()` before it
reaches a log sink; without the flag, no frame is written anywhere. `link` still does
not parse JSON or know a type name — redaction is opaque to it.

Must not: parse JSON, correlate ids, know which frame types exist, or decide when
to reconnect.

### 4.3 `bridge/client/` (L2)

Public surface: `Session` with `connect()`, `close()`, `request(frame)` (returns
the correlated reply), `send(frame)` (fire-and-forget, for `ack`),
`install_sink(cb)`, and a read-only `state` enum.

`Session` owns the timings stated in `05-bridge.md` §4 (5 s `hello_ack` wait, ping
interval, 5 s pong wait, backoff sequence) as constructor arguments. It owns
request/reply correlation: `request` allocates an id, stores the future, and
resolves it when a frame arrives whose `reply_to` matches. It owns the rule that
a `link.rejected` event is terminal and is not retried.

Must not: import Textual, render, own CLI flags, or build slash-command frames.

### 4.4 `bridge/commands.py` (L2)

Public surface: one function, `parse(text: str, state: CommandState) ->
list[Frame]`, plus a `CommandError` for malformed arguments. It maps the wire-bound
rows of `05-bridge.md` §7 to frames.

Commands whose body needs cached device state (`/tasks set`, `/tasks enable`) take
that state as an argument; the module stores nothing, so a stale cache produces
a frame that re-creates the task, as specified in `05-bridge.md` §7.

**`(Local)` commands are not here,** and that is deliberate. `05-bridge.md` §7 marks
seven rows `(Local)`, plus `/diag` which reuses the `status` frame for local
rendering: each renders state the bridge already received
(`hello_ack`, `status`, `agent.final`) and sends no frame. A pure function that
imported only `protocol` could not produce the text they show, so filing them here
would force this module to depend on cached state — and `commands` importing `ui`
is an upward edge the law forbids. They are `ui` surface, resolved by `ui` against
`ui`'s own cache, with no protocol involvement at all.

Must not: import Textual, call a `Transport`, await anything, or send a frame
itself.

### 4.5 `bridge/ui/` (L3)

Public surface: `BridgeApp`, one screen class per `05-bridge.md` §5 surface, and the
widgets those screens compose. The App installs itself as the frame sink at start
and routes each decoded frame to exactly one screen, per `05-bridge.md` §10.

Must not: open a serial port, decode bytes, maintain a second copy of protocol
state, or build frame bodies except by calling `commands.parse`.

### 4.6 `bridge/__main__.py` (L4)

Public surface: none; it is a script. It parses `--port` and `--json`, constructs
`SerialTransport`, then `Session`, then `BridgeApp`, and runs. `--json` is
forwarded to `link` as a constructor flag and is otherwise inert here: `__main__`
decides *whether* frames are logged, `link` decides *how* (§3.2), and `protocol`
decides *what is a secret*. No layer does another's part.

`--target usb|test` (`10-test-agent.md` §3) selects the peer rather than the
transport: `link`'s
`SerialTransport` opens either one identically, and only the guard applied before
opening differs. Selection is therefore composition, which is `__main__`'s job, not
a fourth port.

Must not: contain protocol logic, command parsing, screen code, or open a port itself.
It chooses *which* path to hand over; `link` owns everything after that.

---

## 5. Ports

A port earns its indirection only if it breaks a cycle or names a consumer that
cannot be imported. Exactly two qualify.

| Port | Owner | Implementers | Because |
|---|---|---|---|
| `Transport` | `link` (ABC) | `SerialTransport`, `FakeTransport` | Breaks `client` → pyserial, and makes `Session` testable with no hardware |
| Frame sink | `client` (installs a callback) | `ui` | Breaks `client` → `ui`; `Session` reports decoded frames without knowing who draws them |

`Transport` is an ABC rather than a function-pointer table because Python gives
interfaces for free and the implementer set is closed: serial and fake. The frame
sink is a plain callable installed at start.

### 5.1 There is no third port

The two tests are mechanical: (1) Does it break a cycle? (2) Does it name a
consumer that cannot be imported? Anything else is a call.

Specifically, none of these became ports:

- **No `Clock` port.** Intervals and timeouts are constructor arguments with the
  `05-bridge.md` defaults, so a test passes `pong_timeout=0.01` and controls time
  by sequence. A clock port would add an abstraction to make a test cheaper that
  is already cheap.
- **No `Renderer` port inside `ui`.** Textual is the renderer; a port would let a
  second renderer in, and there is no second renderer.
- **No `CommandParser` port.** `commands.py` is pure and imports only `protocol`;
  `ui` imports it directly (§2 consequence 2). It is not a port because the
  consumer set is one package, and a single consumer never needs indirection.

The rejected `Clock` port is recorded for the same reason `08-code-architecture.md`
records its deleted clock capability: so the next person does not reinvent it.

---

## 6. Reusability

The requirement is that the wire half of the bridge works with no TUI present. It
is met by two grep-checkable laws, not by intent.

1. **`textual` appears only under `bridge/ui/`.** Zero imports elsewhere.
2. **`pyserial` appears only under `bridge/link/`.** Zero imports elsewhere. (The
   distribution is `pyserial`; the import name to search for is `serial`).

```sh
# Both must print nothing.
grep -rn 'textual' bridge/ --include='*.py' | grep -v '^bridge/ui/'
grep -rn '^\(import\|from\) serial\b' bridge/ --include='*.py' | grep -v '^bridge/link/'
```

Both laws are enforced by the layering table rather than by discipline: `client`
may import `protocol` and `link` and nothing else, so a Textual screen in `client`
would be an L2 import of an L3 package — the first illegal edge.

| Lift | Gets you | Use case |
|---|---|---|
| `protocol` alone | The codec: encode, decode, validate, allocate ids | A log replayer, a fixture generator, a second host tool |
| `protocol` + `link` | Codec plus a real serial connection | A script that talks to a device and prints raw frames, no UI |
| `protocol` + `link` + `client` | Full headless client: handshake, ping-pong, correlation, reconnect | A test harness, a CLI, a CI smoke test against hardware |

`commands.py` travels with every row: it is pure and imports only `protocol`, so a
headless tool can reuse slash-command parsing to turn `/status` into a `command`
frame without importing a widget.

What **cannot** be lifted is `bridge/ui/`. It is Textual-shaped by definition:
screen composition and reactive attributes are the framework's vocabulary.
Extracting a framework-neutral view model would be a sixth layer for a consumer
that does not exist.

---

## 7. Testability

Each layer is testable without a device, and each has exactly one seam.

| Layer | Test input | Assertion |
|---|---|---|
| `protocol` | A directory of golden NDJSON lines | Decode → `Frame` → encode reproduces the line byte for byte |
| `link` | `FakeTransport` with a scripted inbound list | Outbound lines are recorded verbatim |
| `client` | `FakeTransport` plus a scripted frame sequence | State transitions, correlation and backoff happen in order |
| `commands` | Command strings and a `CommandState` literal | Returned frames match the `05-bridge.md` §7 mapping |
| `ui` | Textual's pilot | Screens mount, keys switch screens, routed frames land on the right widget |
| docs | `04-protocol.md` §2.1 vs `05-bridge.md` §10 | The set of device `→` bridge types equals the set of rows in the traceability table |
| `redact_line` | A `hello` with `token` and a `secret.set` with `value` | Neither value appears in the output; every other member survives byte-identical |
| `link` + `client`, end to end | `bridge/tools/test_agent.py` over a pty, `--target test` | A full `hello` → `hello_ack` → `ping` → `command` cycle, plus the `--no-hello` and `--token` failure scenarios, without hardware (`10-test-agent.md`) |

The `client` cases worth naming, because each is a rule rather than a happy path:

- **Heartbeat timeout.** Three missed pongs move the session to `DEGRADED`; the
  fourth triggers the backoff sequence, not a fourth ping.
- **`link.rejected`.** The session moves to `REJECTED` and stops. No retry is
  scheduled, no backoff timer fires, and the sink is told once.
- **`E_BUSY`.** A `prompt` rejected with `E_BUSY` resolves the pending request
  with that error code; the session stays `CONNECTED`.
- **Late reply.** A `reply_to` with no pending future is discarded silently.

---

## 8. Non-goals

Recorded so they are not reintroduced as "just one more package".

- **No local agent logic, inference, history or key storage.** `05-bridge.md` §1
  already forbids all four; the architectural restatement is that no layer above
  L1 has a reason to touch a credential, and `protocol` has no file access at all.
- **No plugin framework.** The extension points are `commands.parse` and a screen;
  both are ordinary imports. A registry would be a third port with no named
  consumer.
- **No event bus.** `Session` calls one installed sink. A pub/sub broker would let
  any package reach any other and would make the §2 law unenforceable, for the
  same reason `08-code-architecture.md` gives.
- **No dependency-injection container.** `__main__` constructs three objects and
  passes them down. That is the whole graph.
- **No async-everywhere rewrite.** The pyserial reader thread is correct and
  stays. Serial reads block; a dedicated thread converts that into an
  `asyncio.Queue`, which costs one thread and keeps the UI loop free.
- **No second protocol implementation.** A mock device, a test harness and the real
  client all import `protocol`. Re-implementing the codec in tests is how two
  definitions of the envelope appear.
- **No device simulator.** `bridge/tools/test_agent.py` validates and echoes frames
  (`10-test-agent.md`); it models no config, NVS, LLM or network. A simulator would
  be a second firmware and would drift from it.
