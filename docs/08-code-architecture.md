# 08 — Code Architecture

Status: derived from `docs/00-overview.md` (authoritative). This document does not
re-decide any frozen surface — the module set, protocol, config keys, tool surface and
error codes are fixed elsewhere. It decides **how the code is arranged**: layers,
dependency direction, where an extension point goes, and what costs energy.

Scope: the firmware (`app`, `cfg`, `store`, `net`, `clock`, `llm`, `agent`, `skills`,
`tools`, `sched`, `mcp`, `mqtt`, `geo`, `link`, `sys`). This document governs the
firmware only. The bridge is covered by `05-bridge.md` for behaviour and by
`09-bridge-architecture.md` for layering, which applies the same law with its own
layer names.

**The two rules this document exists to enforce.**

- **DRY** — one mechanism, one implementation. Where two features look alike, they
  share a port rather than each growing their own copy.
- **KISS** — the simplest structure that satisfies the frozen contracts. Every module
  that is not required by a frozen contract is a defect, not a courtesy.

Both are enforced by the dependency law in §2, which is checkable, rather than by
slogans, which are not.

---

## 1. Layering

Four layers plus a leaf. A module belongs to exactly one layer, and dependencies point
one way only — toward the leaves.

```
┌──────────────────────────────────────────────────────────┐
│ L4  app                                                  │  composition root
├──────────────────────────────────────────────────────────┤
│ L3  agent                                                │  orchestration
├──────────────────────────────────────────────────────────┤
│ L2  link   sched   tools                                 │  hosts / dispatch
├──────────────────────────────────────────────────────────┤
│ L1  llm  skills  mcp  mqtt  geo                          │  capabilities
├──────────────────────────────────────────────────────────┤
│ L0  cfg  store  net  clock  sys                          │  platform
└──────────────────────────────────────────────────────────┘
                    ports  ──  owned by the caller,
                                wired by app,
                                never a direct upward call
```

| Layer | Modules | May depend on | Rule |
|---|---|---|---|
| L0 Platform | `cfg` `store` `net` `clock` `sys` | each other, ESP-IDF | No business logic. No JSON-RPC, no prompts, no triggers |
| L1 Capability | `llm` `skills` `mcp` `mqtt` `geo` | L0, plus ports | Each answers a question or performs an effect. Cannot call a host |
| L2 Host | `link` `sched` `tools` | L0, L1, ports, the two L2 edges (§1.1) | Consumes capabilities. Cannot call `agent` |
| L3 Orchestration | `agent` | L0, L1, L2, ports | The only module allowed to hold run state |
| L4 Composition | `app` | everything | Wires, boots, owns tasks. Contains no policy |

`clock` sits at L0 deliberately. It can fail and be resynced, which argues for a
capability, but every layer needs the time — `llm` and `geo` for cache and window
buckets, `sched` for due times, `agent` for frame ordering. Making it a capability
would force a port so that L1 modules could reach it; making it L0 removes four
edges and one port. A capability that everything needs is a platform service.

### 1.1 The dependency law

> **A module may call only modules in a strictly lower layer, a module in the same
> layer on a permitted edge named below, or a port it owns.**

Two same-layer exceptions exist, and both are *enumerated*, not inferred:

- **L0 is a dependency set, not a tier.** `cfg` parses JSON with `store`'s serializer
  and `net` reads WiFi credentials through `cfg`, both one-directional in practice. The
  permitted L0 edges are exactly five: `cfg` → `store`, `net` → `cfg`, `clock` → `net`,
  `clock` → `cfg`, `clock` → `store`.
- **L2 is a host for three modules that must cooperate.** Emitting a frame and arming
  a timer are not capabilities that deserve indirection. The permitted L2 edges are
  exactly two: `sched` → `link` and `tools` → `sched`.

A sixth L0 edge or a third L2 edge needs to be added to these sentences, which is the
point — it makes the exception visible instead of implied. Anything else above L0 must
go through a port (§1.3).

Three consequences, all checkable by grep in CI:

1. **No upward calls.** `tools` never calls `agent`; `sched` never calls `agent`;
   `llm` never calls `tools`. Violations are a build error, not a review comment.
2. **No unnamed same-layer calls.** `mqtt` does not call `sched`; `mcp` does not call
   `llm`; `geo` does not call `clock` — it includes it, since `clock` is L0. The
   permitted L2 edges are directional and form no cycle:
   - `sched` → `link`, to emit `task.fired` and `gpio.edge`. `link` never calls `sched`.
   - `tools` → `sched`, for `sched_apply_task()` / `sched_remove_task()` after
     `task.set` or `tasks.delete`. `sched` never calls `tools`.
3. **L0 carries no business logic.** `store` does not know what a task is; `net` does
   not know what a provider is.

### 1.2 Two cycles the current module table has

`02-firmware.md` §1 gives each module its "Primary Dependencies". Read literally, that
table contains two cycles. Both are real. Each is resolved at its single illegal edge —
one by a port, one by ordinary downward direction — so the table in `02-firmware.md`
stays a summary of *intent* and this section is how the code actually composes.

**Cycle A — `tools` → `mqtt` → `sched` → `agent` → `tools`.**

- `tools` needs `mqtt` to publish.
- `mqtt` needs to deliver inbound messages to the scheduler.
- `sched` needs `agent` to run a task.
- `agent` needs `tools` to dispatch a call.

Resolution splits by direction: **outbound halves by direction, inbound halves by
ports.** The edge that points down needs nothing; the one that points up needs a port.

- `tools` → `mqtt` is L2 → L1 and therefore already legal. `mqtt.publish` includes
  `mqtt/mqtt.h` and calls `mqtt_publish()` directly. No port.
- `mqtt` → `sched` is the illegal half: L1 cannot call L2. `mqtt` owns a **sink port**
  and never learns who consumes messages.

```c
/* mqtt owns this; sched implements it and app registers it */
typedef struct {
    void (*on_message)(const char *server_id, const char *topic,
                       const uint8_t *payload, size_t len);
} mqtt_sink_t;

esp_err_t mqtt_set_sink(const mqtt_sink_t *sink);
```

`mqtt` calls `sink->on_message` and stops there. `sched` registers itself at boot.
Neither names the other in a header.

**Cycle B — `tools` → `sched` → `agent` → `tools`.**

Of the three edges, only one is illegal: `sched` → `agent` crosses L2 into L3. The
other two are fine — `tools` → `sched` is a permitted L2 edge (§1.1) and `agent` →
`tools` is L3 calling down into L2. So this cycle needs exactly one port, not three.

`event.emit` must fire matching tasks, which means starting a run, which is `agent`'s
job — but `tools` cannot call `agent` and `sched` cannot either, since both are L2 and
`agent` is L3.

Resolution: a **run port** that `agent` *implements* and `app` *hands down*.
Ownership follows the caller, so `sched` and `tools` define nothing; `agent` publishes
one function pointer and `app` passes it to the two L2 modules at boot.

```c
/* agent exports; app installs into sched and tools */
typedef void (*agent_run_done_cb)(esp_err_t err, void *user);
typedef esp_err_t (*agent_submit_fn)(const char *source, const char *prompt,
                                     agent_run_done_cb on_done, void *user);

esp_err_t agent_submit(const char *source, const char *prompt,
                       agent_run_done_cb on_done, void *user);
esp_err_t sched_set_submit(agent_submit_fn fn);
esp_err_t tools_set_submit(agent_submit_fn fn);
```

The dependency still points downward at call time — `agent` is not included by `sched`
or `tools`, only a function signature is — but the *edge* is installed by `app`, so no
module names a higher layer. This is the one place where the law needs mechanical help
rather than a plain call, and the mechanism is a function pointer, not a framework.

`agent_submit()` is the only way a run starts, whatever triggered it. This is DRY in
its most load-bearing form: `prompt` from `link`, a `schedule` fire, a `gpio` edge
(handler queues it, `sched` task drains — `02-firmware.md` §5.1), an `mqtt_message`, a
`boot` task and an `event.emit` all converge on one function with one queue, one
`E_BUSY` path and one close-out gate.
Six triggers, one implementation, and not one of them includes an `agent` header —
`sched` calls the installed pointer.

### 1.3 Ports: the complete set

A port is a function-pointer type owned by its **caller**, implemented by a **lower**
module or by `app`, and registered at boot. Nothing else. There are four.

| Port | Owner (defines) | Implementer | Because |
|---|---|---|---|
| `agent_submit_fn` | `sched`, `tools` (consumers) | `agent`, installed by `app` | breaks Cycle B |
| `mqtt_sink_t` | `mqtt` | `sched` → `agent` (via `agent_submit`) | breaks Cycle A |
| `tool_provider_t` | `tools` | one per domain (see §4) | a domain registers; `tools` does not include it |
| `link_sink_t` | `link` | `app` | frames route to `agent` or `sched` by `type`; `link` must not know which |

```c
/* link owns this; app implements the routing */
typedef struct {
    void (*on_frame)(const json_node_t *frame);
} link_sink_t;
```

Four ports for fifteen modules, and no port exists without a named cycle or a named
consumer that cannot include the module directly. Adding a fifth requires saying which
of those two it is.

A fifth, `clock_iface_t`, was designed and then deleted: an earlier draft put `clock`
at L1, which forced a port so that L1 peers could reach it. Moving `clock` to L0 (§1)
removed that port and its four call sites. Recorded here so the next person does not
reinvent it.

### 1.4 What is deliberately *not* an interface

No dependency-injection framework, no vtables for their own sake, no `void *`
contexts where a concrete type will do. ESP-IDF in C already gives direct calls and
link-time composition; a function pointer earns its indirection only at the four
points above. Everywhere else, call the function.

---

## 2. Module anatomy

Every module has the same four parts, in the same order. This is the DRY rule applied
to structure: a reader who has seen one module has seen them all. The declaration-level
view of every component, with fields and source links, is [14](14-class-diagrams.md).

```
components/<name>/
  include/<name>.h     public API only — what other modules may call
  <name>.c             implementation
  <name>_internal.h    private types, not includable from outside
  Kconfig.projbuild    only if the module has a build-time option
```

```c
/* include/mqtt.h — the whole public surface, one screenful */
#pragma once
#include "esp_err.h"

typedef struct { ... } mqtt_sink_t;          /* port, if this module owns one */

esp_err_t mqtt_start(void);                  /* idempotent, safe to call twice */
esp_err_t mqtt_stop(void);                   /* releases sockets, keeps config */
esp_err_t mqtt_publish(const char *server_id, const char *topic,
                       const char *payload);
esp_err_t mqtt_status(size_t max, mqtt_status_t out[max], size_t *count);
```

**Rules that make modules actually modular:**

1. **No module includes another module's `_internal.h`.** Enforced by the include
   path: `<name>_internal.h` lives outside `include/`.
2. **Allocating module initialises its own state.** `mqtt_start()` allocates; `app`
   does not pre-create structs for it.
3. **`esp_err_t` everywhere**, with the mapping to protocol codes done once (§3).
4. **No module calls `printf`, `ESP_LOGI` to the UART, or `abort()`.** Logs go through
   the log vprintf hook owned by `link` (`00-overview.md` D9); failure returns a code.
5. **A module's public header includes only `<esp_err.h>`, its own port structs, and
   primitive stdint/stddef types.** No `cJSON.h` in a public header — that would leak
   one JSON library across the whole codebase and is the most common DRY violation in
   ESP-IDF projects.

Rule 5 is the one that keeps the tree refactorable. The single place JSON is allowed
to cross a module boundary is the frame type in `link_sink_t`, and even there it is
opaque.

---

## 3. Cross-cutting mechanisms — implemented once

These are the places DRY actually saves code. Each has exactly one implementation and
every module routes through it.

| Mechanism | Sole owner | Not allowed elsewhere |
|---|---|---|
| JSON parse / serialise / canonical ordering | `store` (`json.c`) | No direct `cJSON_*` outside `store` |
| Error code mapping (`esp_err_t` → protocol code) | `app` (`errors.c`) | No module invents a wire code |
| Secret resolution (`nvs:<key>` → value, then redact) | `cfg` | No module reads the `secrets` namespace |
| Path resolution and confinement | `store` | No module builds a path from a model string |
| Time (epoch, monotonic, TZ) | `clock` | No direct `time()`, no `esp_timer` reinterpretation |
| Frame emit (build envelope, `id`, `ts`, send) | `link` | No module writes to CDC directly |
| Redaction of secrets in any outbound text | `cfg` | No module formats its own log lines around secrets |
| LLM request body assembly | `llm` | No module builds a `/chat/completions` payload |

**The JSON rule deserves its size.** One library (`cJSON`, already in ESP-IDF), one
wrapper, one place where canonical key ordering is defined. `03-config.md` requires
canonical JSON in examples; a single serializer makes that a property of the code
rather than a habit. It also makes the 16 KiB TX cap checkable in one function instead
of at every emit site.

```c
/* store/json.c — the only JSON entry points in the firmware */
esp_err_t json_parse(const char *buf, size_t len, json_doc_t **out);
esp_err_t json_emit(json_doc_t *doc, char *buf, size_t cap, size_t *written);
void      json_free(json_doc_t *doc);
```

---

## 4. Extension points

The two things a user extends are tools and skills. Both follow one recipe.

### 4.1 Adding a built-in tool

A tool domain is one file, not one tool. `docs/tools/` already organises them this way
(`gpio.md` holds `gpio.read` and `gpio.write`); the code mirrors it.

```c
/* components/tools/src/domains/gpio.c */
static const tool_provider_t gpio_provider = {
    .prefix   = "gpio",
    .gate     = TOOL_GATE_DANGEROUS,          /* or TOOL_GATE_NONE */
    .present  = gpio_present,                 /* NULL = always present */
    .dispatch = gpio_dispatch,
};
TOOL_DOMAIN_REGISTER(gpio_provider);
```

`TOOL_DOMAIN_REGISTER` is a linker-section macro: a domain that is not compiled in
simply is not present, with no registry to edit. Adding a tool means **one new file**
in `src/domains/`, which is the entire modularity claim made concrete.

The shared work — argument validation, the gate check, the result envelope, the
`agent.tool` / `agent.tool_result` frames, the `sys.tool_calls` counter, error
mapping — lives in `tools` and is written once for all 26 built-ins.

### 4.2 Adding a declarative tool

No code. A file under `/tools/*.json`, imported with `fs.put` (`03-config.md` §tools).
Declarative tools and built-ins produce the same `tool_result` shape, because both
converge on the same dispatcher.

### 4.3 Adding a capability module

Rare, and the bar is high: it must be a new *kind* of I/O, not a new feature. The
recipe is a checklist that enforces this document.

1. Name the layer from §1.
2. If it needs a lower module, confirm that module is strictly below — otherwise
   you need a port, and you must name the cycle it breaks.
3. Declare the API in `<name>.h`, `esp_err_t` returns.
4. Register it in `app`'s boot sequence and in `02-firmware.md` §1 and §2.

---

## 5. Energy model

The device is mains-powered in v1 and USB-attached, so energy is **not** a survival
constraint — it is a cost and a heat constraint. The rules below are what "energy
efficient" means concretely here, and each is testable.

### 5.1 The budget

| State | Target | Dominant cost |
|---|---|---|
| Idle, link open, WiFi associated | < 120 mA avg | Modem sleep + idle task stack |
| Idle, no link, WiFi associated | < 80 mA avg | Modem sleep |
| Active LLM round | ~350 mA peak | WiFi TX + mbedTLS |
| Deep sleep | Not reachable in v1 | — |

These are the same figures as `01-hardware.md` §5, which is the authority for them;
this section is how they are achieved and verified. The peak number is what causes
brownout resets, and it is set by the USB port, not by firmware. The idle numbers are
targets for M5, not measurements — they carry `[ASSUMPTION]` in the hardware doc until
a meter says otherwise.

### 5.2 The rule: idle means idle

**Energy efficiency here is achieved by never waking up for nothing.** The firmware
has no polling loops, no `vTaskDelay(10)` spinner, and no timer that fires to check
whether something changed.

| Anti-pattern | Consequence | Correct form |
|---|---|---|
| Polling the link for input | Wakes the CPU thousands of times a second at idle | Block on the CDC read with a long timeout |
| Polling a sensor or MQTT | Same, plus radio never sleeps | Event-driven; `mqtt` delivers through its sink port |
| A timer that checks `tasks.json` every second | Preventable wake + flash read | One `esp_timer` per next-due task, armed to its deadline |
| Busy-waiting on a socket | CPU pinned, no sleep | `select`-equivalent on the TLS socket, or IDF's blocking HTTP client |
| Holding a WiFi scan for `geo` at idle | Radio awake for ~2 s | Only on an explicit `geo.locate`, cached by TTL |

Every blocking call is a power feature. A `read(fd, buf, len)` that blocks on the CDC
endpoint for one second wakes once per second instead of 100 times, and does the same
work.

### 5.3 Dynamic frequency scaling: on

DFS is the one power mechanism fully compatible with the frozen no-sleep rule: the CPU
still runs, still answers `ping` on the next tick, still fires `schedule` on time — it
just does so at half clock when nothing is happening.

```c
const esp_pm_config_t pm = {
    .max_freq_mhz = 160,
    .min_freq_mhz = 80,
    .light_sleep_enable = false,   /* the whole point: DFS only */
};
esp_pm_configure(&pm);
```

Build with `CONFIG_PM_ENABLE=y` and `CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_160=y`.
`light_sleep_enable = false` is what keeps this inside the ban; setting it `true` is
exactly the light sleep §5.4 forbids. `agent` takes an `esp_pm_lock` for the duration
of a run so the clock only rises while there is work; `link` and `sched` never take
one, so an idle device sits at 80 MHz with the modem asleep.

DFS costs nothing architecturally: no task changes, no wake-latency budget, no USB
side effect. It is the default and is verified at M5 alongside the current figures.

**Tickless idle is deliberately *not* used in v1**, and this is the subtle one. On
ESP-IDF, FreeRTOS tickless idle is implemented **as automatic light sleep** — enabling
it alongside `CONFIG_PM_ENABLE` puts the core into light sleep between ticks. So it is
not an alternative to the light-sleep ban; it *is* the light-sleep ban, with the same
cost: `schedule` precision and possible USB CDC frame loss (§5.4). Anyone reaching for
tickless idle as a "free" win should read §5.4 first. `CONFIG_FREERTOS_USE_TICKLESS_IDLE`
stays off.

### 5.4 Sleep: what v1 does and does not do

`01-hardware.md` §5 states that `app` never enters light or deep sleep, because the
device must answer `ping` and fire `schedule` triggers while the link is open. This
document keeps that and adds the one sleep mode that does not conflict with it:

- **Modem sleep: on.** Automatic once WiFi is associated and no socket is actively
  transferring. It is the difference between ~80 mA idle and ~20 mA, and it is
  transparent to `ping` — the AP buffers briefly, and the modem wakes on the next
  beacon. No code is needed beyond not disabling it.
- **Light sleep: off in v1, deliberately.** It would gate `schedule` precision and
  drop USB CDC frames. The gain on a mains device is not worth an unreliable link.
- **Deep sleep: off in v1**, as already frozen.

**Conditional exception, gated on a milestone.** If M5 measures idle draw that misses
the §5.1 target, the sanctioned fix is *not* to enable light sleep globally — it is to
allow light sleep **only when `link` is detached and `sched` has no task due within
`light_sleep_horizon_s`**, waking on the timer. That path is `[ASSUMPTION]` and must
not be built before the measurement says it is needed. Measuring first is the KISS
answer: it is likely the target is met without it.

### 5.5 Cost-proportional work

Three rules that cut radio-on time, which dominates the energy profile.

1. **One TLS connection per active operation, reused within it.** `llm` keeps its
   session for the duration of a run and closes it at `agent.final`; it does not
   reconnect per round.
2. **No speculative network calls.** `geo` runs only on `geo.locate` and is TTL-cached
   (`03-config.md` §geo). `web_search` and `weather_now` are declarative tools the
   model must choose — they never fire on their own.
3. **Batch frame emission.** A tool round emits `agent.tool` then `agent.tool_result`;
   both go out under one mutex acquisition and one CDC write where the sizes allow,
   not two wake-and-write cycles. `link` owns this batching.

### 5.6 Measuring it

`sys.diagnostics` with `scope: "runtime"` already reports `mem.heap_free` and
`uptime_s`. Energy verification at M5 adds one measurement, not a subsystem: a USB
power meter on the 5 V rail, sampled in the three §5.1 states, compared against the
targets. If a state misses, §5.4's conditional exception is the sanctioned response
and nothing else is.

---

## 6. What "modular" specifically buys

Concrete, because "modular" alone is unfalsifiable. Four properties, each with a test.

| Property | Statement | Test |
|---|---|---|
| Independence | Compiling with `geo` removed still builds and boots | Delete the component; `idf.py build` succeeds and `geo.locate` is refused as `E_CONFIG` rather than crashing. `mqtt` is *not* removable — `tools` owns `mqtt.publish`, so removing it must fail the build at the domain's `present` hook, not at link time |
| Substitutability | `geo` can be replaced by a stub returning a fixed fix | Link the stub; `geo.locate` result shape unchanged |
| Locality of change | Adding a tool touched one domain file and no callers | Diff shows one new file plus its registry line |
| Testability | The agent loop's tool dispatch runs with a mock `llm` | Host test binary calls `agent_submit` with a canned response |

The `geo`/`mqtt` asymmetry is worth stating plainly: a module is removable only if no
frozen tool, command or config key depends on it. `geo` qualifies because `geo.locate`
is a single gated command that can refuse. `mqtt` does not, because `mqtt.publish`,
`mqtt.status` and the `mqtt_message` trigger are all frozen surface. Writing the test
this way is what stops "modular" from being claimed for a module that cannot be
removed.

The third is the one that pays repeatedly. It held for the five tools added most
recently only because `docs/tools/` forced the one-file-per-domain shape early; that
is why the code follows the documentation's file boundaries rather than inventing its
own.

---

## 7. Non-goals

Recorded so they are not reintroduced as "just one more layer".

- **No dynamic module loading.** No Lua, no scripting, no `.so`-style plugin ABI.
  Declarative tools cover user extension; anything more is a different product.
- **No event bus.** Modules call four ports. A publish/subscribe broker would let any
  module reach any other and would make the §2 dependency law unenforceable.
- **No dependency-injection container.** `app` is the composition root and calls
  `*_start()` directly.
- **No abstraction over ESP-IDF.** `net` calls `esp_wifi_*`; it does not wrap it in a
  "network provider interface". The portability it would buy is not needed — there is
  one target board.
- **No host-side emulation of the firmware.** The two testable seams are `llm` and
  `geo`, which get stubs. A simulator that reimplemented the firmware would be out of
  scope and the largest module in the tree. The one host-side double is
  `bridge/tools/test_agent.py` (`10-test-agent.md`), and it is not emulation: it speaks
  the wire format and models no config, NVS, LLM, network or scheduler, so no firmware
  behaviour is reimplemented where a bug could hide. QEMU is not this: it executes the
  real firmware binary, reimplements nothing, and cannot drift (`02-firmware.md` §8).
- **No subagents, no multirole, no wake word, no audio** — frozen in
  `00-overview.md` §1.2 and not revisited here.
