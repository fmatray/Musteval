# 02 — Firmware

This document details the internal architecture of the Musteval firmware.
It implements the decisions in `docs/00-overview.md` §2 using ESP-IDF v5.x (C).
The firmware is structured as a collection of loosely coupled modules with a
strictly serialized agent loop.

## 1. Module Map

The firmware consists of 15 modules in four layers. Dependency flow is strictly
downward — `app` (L4) → `agent` (L3) → hosts and capabilities → platform — with `cfg`,
`store`, `net` and `sys` at the bottom. The normative layering law, the port set and
the energy rules are in `docs/08-code-architecture.md`; this table is the summary.

| Module | Layer | Responsibility | Primary Dependencies |
|---|---|---|---|
| `app` | L4 | Entry point, boot sequence, global error handler | everything (composition root) |
| `agent` | L3 | Agent loop, tool dispatch, conversation history | `llm`, `tools`, `skills`, `mcp` |
| `link` | L2 | NDJSON framing over USB-Serial/JTAG (CDC-ACM) | `cfg` |
| `sched` | L2 | Task trigger logic, `tasks.json` tracking, timers | `store`, `cfg`, `clock`, `link`* |
| `tools` | L2 | Tool registry (built-in + declarative) | `store`, `cfg`, `net`, `geo`, `mqtt`, `sched`* |
| `llm` | L1 | HTTPS client, provider failover chain, prompt assembly | `net`, `cfg` |
| `skills` | L1 | Prompt template loading from `/skills/*.md` | `store` |
| `mcp` | L1 | Remote MCP server communication (HTTP/JSON-RPC) | `net`, `cfg` |
| `mqtt` | L1 | One client per configured broker, subscriptions, publish | `net`, `cfg` |
| `geo` | L1 | WiFi scan positioning with provider fallback | `net`, `cfg` |
| `clock` | L0 | SNTP client, time sync, timezone offset | `net`, `cfg`, `store` |
| `cfg` | L0 | `config.json` parsing, NVS `secrets` resolution | `store` |
| `store` | L0 | LittleFS wrapper, NVS `runtime` namespace | — |
| `net` | L0 | WiFi (STA), DHCP, DNS, mbedTLS setup | `cfg` |
| `sys` | L0 | Hardware abstraction (GPIO, Watchdog, Brownout) | — |

`*` marks the two permitted same-layer edges (`08-code-architecture.md` §1.1). Every other
entry is strictly downward.

Dependencies are one-directional by layer: a module calls only strictly lower layers,
a module in the same layer on a permitted edge (`08-code-architecture.md` §1.1), or an
interface it owns. Earlier versions of this table listed `tools` → `mqtt`, `sched` →
`agent` and `agent` → `mqtt`/`geo`, which formed two cycles; each is resolved at its
single illegal edge — outbound halves by direction, inbound halves by ports:
- Cycle A (`tools` → `mqtt` → `sched` → `agent` → `tools`) — the **inbound** half goes
  through `mqtt_sink_t`; the **outbound** half (`mqtt.publish`) is a plain downward
  L2 → L1 call, which is legal and needs no port.
- Cycle B (`tools` → `sched` → `agent` → `tools`) — `agent` is L3 and cannot be called
  by L2, so runs start through the `agent_submit_fn` port installed by `app`.

`docs/08-code-architecture.md` §1 is the normative statement of the law and §1.3 lists
all four ports.

## 2. Public APIs (Internal Contract)

The following function signatures are the primary internal contracts. All
functions return `esp_err_t`.

### `cfg` — Configuration
- `cfg_init()`: Load `config.json` from flash. Returns `E_CONFIG` if `secret_ref`
  resolution fails.
- `cfg_get_str(key, buf, len)`: Get value from `config.json`.
- `cfg_resolve_secret(ref, buf, len)`: Resolve `nvs:<key>` to plaintext via
  NVS namespace `secrets`.

### `store` — Storage
Two LittleFS partitions, two VFS mounts (`docs/01-hardware.md` §4). The agent's
namespace is rooted at `/`; the resolver maps it onto those mounts.

`store_map_path(agent_path, out, out_len)`: translate an agent-visible path to a VFS
path. Pure prefix substitution, no filesystem access:

| Agent path prefix | VFS path |
|---|---|
| `/workspace/...` | `/workspace/...` (`wsfs` mount, unchanged) |
| anything else | `/fs/...` (`userfs` mount) |

- `store_mount()`: register `userfs` at `/fs` and `wsfs` at `/workspace`.
  `userfs` mount failure is `E_STORAGE` and halts boot; `wsfs` mount failure
  formats `wsfs` once and retries. Never formats `userfs`. The mount points carry
  no trailing slash: `esp_vfs_register` rejects one (`is_path_prefix_valid`), while
  the agent-path prefixes need it, so `store.h` derives `STORE_ROOT_*` from
  `STORE_VFS_*`.
- `store_read_text(agent_path, buf, cap, len, total, truncated)`: read partial
  text under `/workspace/`; UTF-8 validated, invalid sequences return `E_STORAGE`.
  `total` is the file's whole length, which is what `04` §4.18 reports as `bytes`
  even when the cap stopped the read short.
- `store_list_dir(agent_path, out, cap, count)`: list one directory under
  `/workspace/`, sorting directories first then names. A listing exceeding the
  cap returns `E_STORAGE`. The root `/workspace/` is a legal listing target,
  though not a legal file target; the resolver is applied to entries within.
- `store_resolve_write_path(in, out, out_len)`: accept `in` only if it canonicalises
  to strictly under `/workspace/` (with at least one further segment), then return
  the VFS path. Rejects `..` that escapes, relative paths, NUL bytes and over-long
  paths with `E_TOOL`. Because `/workspace` is a separate mount root, `..` above it
  has nowhere to go; the resolver is defense in depth, not the only barrier
  (`06-security.md` §6.1).
- `store_write_chunk(agent_path, data, len, offset, final, bytes, created)`:
  staged chunked write after `store_resolve_write_path`. Enforces the workspace
  quota (`06-security.md` §6.2); the write commits by renaming a temporary
  file over the target only on `final:true`.
- `store_fs_usage(fs, used, total)`: report one mounted filesystem's usage through
  `esp_littlefs_info`, with `STORE_FS_USERFS` / `STORE_FS_WSFS` naming the mount. It
  exists so `app` can fill `status.storage` without naming a partition label itself;
  the label is `store`'s knowledge (`04` §3.9).
- `store_delete_file(agent_path)`: unlinks a sandbox file or an empty
  directory after `store_resolve_write_path` gating. Quota accounting is
  recomputed from the directory state rather than decremented, so a failed
  unlink leaves the count untouched. Non-empty directories return `E_CONFIG`.
- `store_is_dir(agent_path, out)`: report if a sandbox path is a directory.

Pure helpers in `store_sandbox.c` implement internal logic: `store_entry_cmp`
for sorting, `store_entry_name_ok` for character validation,
`store_decode_chunk` for base64/UTF-8 normalization, and `store_utf8_prefix` for
the UTF-8 scan `store_read_text` runs. `store_utf8_prefix` returns the longest
valid prefix and refuses a lone continuation byte, an overlong form (`C0`/`C1`,
`E0 80..9F`, `F0 80..8F`), a surrogate (`ED A0..BF`) and anything above
`U+10FFFF` (`F4 90..`, `F5`..`FF`). A prefix shorter than the buffer is a
truncation only when the remaining bytes begin a sequence the cap cut in half:
those shorten the text and set `truncated`, they do not refuse the file. Being
pure, it is host-tested rather than reached through a mounted filesystem.
- `store_nvs_set_int(key, val)` / `store_nvs_get_int(key, &val)`: Access
  NVS namespace `runtime`.
- Exactly three APIs write `userfs`: `config.set`, which targets `/fs/config.json`
  and is not reachable through any tool path; `store_task_write()`, which targets
  `/fs/tasks.json` and is reachable from the `task.set` tool and the
  `tasks.set`/`tasks.delete` commands (§7.2); and the host-only `fs.put` command,
  which targets any allowlisted path not shipped in the image (`04-protocol.md`
  §4.16). Every other write target is `wsfs`, and only `task.set` is model-reachable.

### `llm` — Language Model
- `llm_query(prompt, history, tools_json, response_buf, len)`: Send prompt to
  current provider. Returns `E_LLM_UNAVAILABLE` on a transport or provider failure,
  which triggers provider failover; returns `E_LLM_RATE` when the budget check
  refuses first, which does **not** trigger failover — a different provider would
  spend the same budget.
- `llm_budget_charge()`: Called once per request *attempt*, before the socket is
  opened. Refuses with `E_LLM_RATE` when either fixed UTC window
  (`llm.max_calls_per_hour`, `llm.max_calls_per_day`) is exhausted, so a fallback
  chain cannot multiply the spend. A refused call is not counted. Each window is
  stored as a counter plus its bucket index (`unix / 3600`, `unix / 86400`); a
  bucket mismatch resets the counter instead of trusting a stale value
  (`03-config.md` §`llm`).
- `llm_budget_counters(hour, day)`: Reads the counters for `status`
  (`04-protocol.md` §3.9). Counters live in NVS and survive reboot.
- `llm_assemble_prompt(system_prompt, history, tools_json, target_buf)`: Merge
  system prompt, history and the tool array into the request body.

#### Request body

The request is the same for every provider in the `03-config.md` provider table: one
`POST {base_url}/chat/completions` with `Content-Type: application/json` and
`Authorization: Bearer <resolved api_key_ref>`. These are the fields the firmware
writes; `"<token_param>"` stands for `max_tokens` or `max_completion_tokens` per
`llm.providers[].token_param`:

```json
{
  "model": "<llm.providers[].model>",
  "messages": [
    {"role": "system", "content": "<system prompt>"},
    {"role": "user", "content": "<prompt>"}
  ],
  "tools": [{"type": "function", "function": {"name": "...", "description": "...", "parameters": {}}}],
  "<token_param>": 1024,
  "stream": false
}
```

The token cap is the one field whose **name** differs per provider, so it is not
hard-coded: `llm.providers[].max_output_tokens` is sent under
`llm.providers[].token_param`, either `max_tokens` (default) or
`max_completion_tokens`. OpenAI reasoning models reject `max_tokens` with a 400, and
Ollama's compatibility table lists `max_tokens` as supported and does not list
`max_completion_tokens` at all; one fixed name cannot serve both.
`stream` is always `false` (overview D8).

Everything else is deliberately absent: omitting a key is ignored by every provider,
while sending one that a provider rejects fails the whole request. So no
`tool_choice`, `temperature`, `top_p`, `response_format` or `strict` is sent by
default - Ollama Cloud does not support `tool_choice`, and Anthropic's compatibility
layer ignores `strict` and rejects `response_format: json_object`.

Where a provider needs a field anyway, `llm.providers[].extra_body` supplies it
verbatim. It is merged last, but it may not carry `model`, `messages`, `tools` or
`stream` - those are rejected as `E_CONFIG` rather than silently overriding the
request's structure. That is what keeps current OpenAI models usable:
`gpt-6-luna` and its generation support function calling over `/chat/completions`
**only** with `reasoning_effort: "none"`, so an OpenAI entry carries
`{"reasoning_effort": "none"}` there. `extra_body` is per provider and per model,
which is exactly the granularity the divergence has.

Tool calls are read from `choices[0].message.tool_calls[]` and results returned as
`role: "tool"` messages carrying `tool_call_id`. A provider that returns tool calls
in any other shape is out of scope.

Tool definitions are assembled at run start; the assembly rules and the
per-tool schemas are in §7.1 and `docs/tools/`.

### `agent` — Core Loop
- `agent_run_sync(user_input)`: The core serialized loop. Handles tool rounds
  and history updates. Returns `E_BUSY` if a run is already in flight.
- `agent_handle_tool_call(tool_name, args_json)`: Dispatch to built-in,
  declarative, or MCP tools.
- `agent_todo_phases()`: Returns the live todo phase/task tree. The list is heap
  state owned by this module, not by the `tools` module, so it outlives a tool call
  and survives across runs until `reload` or reboot. `tools/todo.md` is its only
  consumer and the canonical description of the shape. The loop reads it for the
  close-out check described in §4.1 step 5.

### `skills`
- `skills_build_index()`: Assembles the skill index at boot and on `reload` from
  every `/skills/*.md` frontmatter. One line per skill of the form
  `- <name>: <description>` is appended to the system prompt before history. The
  index counts against `agent.max_input_bytes`; if the total prompt exceeds the cap,
  skills are dropped from the end of the index in name order until it fits, and the
  omission is logged. The body remains load-on-demand via `skill.read`.

### `link` — Framing
The only module that builds an envelope (`08-code-architecture.md` §3); every other
module hands it a JSON body built with the `store` helpers.

- `link_start()`: install the USB-Serial/JTAG driver, register the CDC writer and
  start `link_task` (§3.3). Idempotent, and a driver failure is reported without
  aborting boot. The driver's ring buffers are sized to `LINK_TX_MAX + 2` and
  `LINK_RX_MAX + 2`, not IDF's 256-byte default: `link_emit` makes exactly one
  writer call per frame so two tasks cannot interleave halves of a line, and a ring
  smaller than the frame refuses that single call and drops the frame. Measured on
  the board, a 277-byte `hello_ack` against the default 256-byte ring wrote 0 bytes.
- `link_emit_body(type, id, ts, reply_to, body)`: build the envelope around `body`
  and write one frame. This is the only way `app` sends a frame — `08` §3 forbids
  any other module from building an envelope.
- `link_build_hello_ack(obj, hello)` / `link_build_status(obj, st)`: pure body
  builders, host-tested. They fill a node the caller already created
  (`json_root(doc)`) and return `esp_err_t`, because no `store` entry point adopts a
  detached subtree. The `link_hello_t` / `link_status_t` facts they serialise are
  filled by `app`.
- `ping` is answered inside `link_task` and never reaches the sink, so the
  keepalive costs no handler round-trip.

### `sched` — Scheduler
- `sched_init()`: Load `/tasks.json` and start timers.
- `sched_trigger_event(type, pin_id)`: Mark a trigger (e.g., `gpio`, `boot`)
  as fired.
- `sched_on_mqtt_message(server_id, topic, payload)`: match inbound messages
  against `mqtt_message` tasks (`02-firmware.md` §5.4).
- `sched_apply_task(id)`: re-arm one task after `tasks.set` added or updated it, so
  a new schedule takes effect without a `reload`.
- `sched_remove_task(id)`: cancel one task's timer after `tasks.delete`. Returns
  `E_CONFIG` for an unknown id; the caller has already validated, so this only
  fires on a race with `reload`.
- `sched_set_submit(fn)`: install the `agent_submit_fn` port (§2.1). Any trigger
  that starts a run — `boot`, `schedule`, `gpio`, `mqtt_message`, `event` — goes
  through `*fn`. `sched` never includes an `agent` header.

### `mqtt` — Brokers
- `mqtt_init()`: create one `esp_mqtt_client_handle_t` per `mqtt.servers[]`
  entry, up to `mqtt.max_servers`; subscribe to that server's `topics[]`.
- `mqtt_publish(server_id, topic, payload, len, qos)`: refuse `E_MQTT` unless
  `server_id` exists and `topic` is covered by that server's `topics[]`.
  Returns `E_MQTT` when the client is not connected. `E_MQTT` is the whole
  failure domain here; neither gate maps to `E_TOOL`.
- `mqtt_status(out, len)`: serialise `[{id,connected,subscriptions}]`.
- `mqtt_set_sink(sink)`: install the `mqtt_sink_t` callback (§2.1). `mqtt` calls
  `sink->on_message(server_id, topic, payload, len)` for every inbound message and
  does not know who consumes it. `app` registers `sched`'s handler at boot, which
  in turn raises the `mqtt.message` frame through `link` and matches `mqtt_message`
  triggers.

Reconnect uses `wifi.retry_backoff_ms[]`. `tls: true` sets
`broker.verification`; there is no insecure fallback. Client contexts and RX
buffers are internal SRAM, never PSRAM (DMA; `01-hardware.md` §2).

### `geo` — WiFi positioning
- `geo_init()`: validate `geo.providers[]`, resolve any `api_key_ref`.
- `geo_locate(out, &accuracy_m)`: blocking. Returns `E_CONFIG` when
  `geo.enabled` is false, `E_GEO` when every provider fails, `E_TIMEOUT` on
  `geo.timeout_ms`.

Flow:

1. Return the cached fix when its age is under `geo.cache_ttl_s`.
2. `esp_wifi_scan_start` (blocking), sort by RSSI, keep the
   `geo.max_bssids` strongest.
 3. Try each `kind: "wifi_scan"` provider in order with
    `[{bssid, rssi}]`. If a provider exceeds its attempt budget or fails to
    respond within `geo.timeout_ms`, it is considered failed and the chain
    continues to the next entry.
 4. Fall back to the first `kind: "ip"` provider, which needs no key.
 5. Cache `{lat, lon, accuracy_m, provider, source_ts}` and return it.

 The cache is RAM-only and bounded by `geo.cache_ttl_s` alone: a change in RSSI
 does not invalidate it. A reboot costs one scan. Geolocation reveals the access
 point's position, not necessarily the device's, and `accuracy_m` is reported
 as-is rather than implied precise (`06-security.md` §6).

### `sys` — Hardware abstraction
- `sys_init()`: Install the GPIO ISR service (`gpio_install_isr_service()`) and start
  the task watchdog. Does not install a brownout handler: the brownout threshold is a
  board constant (`01-hardware.md` §5), not a runtime decision.
  - `sys` is the only owner of the task watchdog: `CONFIG_ESP_TASK_WDT_INIT` and both
    `CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU*` are off in `sdkconfig.defaults`. Left
    on, IDF initializes the watchdog first, `esp_task_wdt_init()` then returns
    `ESP_ERR_INVALID_STATE` and logs an error on every boot. Taking ownership means
    also taking the subscription IDF was doing: `idle_core_mask` covers every core
    (`(1 << CONFIG_FREERTOS_NUMBER_OF_CORES) - 1`), so the idle tasks are watched
    until the scheduler exists and can subscribe its own.
- `sys_gpio_edge_config(pin, edge)`: Configure a pin as input with the given edge
  (`SYS_EDGE_RISING` / `SYS_EDGE_FALLING` / `SYS_EDGE_ANY`). Does not install a
  handler.
- `sys_gpio_read(pin, *level)` / `sys_gpio_write(pin, level)`.
- `sys_led_set(on)`: the onboard user LED on GPIO21 (`01-hardware.md` §6), which is
  active-low and not on the header. The inversion lives here, so no caller writes a
  logic level that means the opposite of what it says. `gpio.read` on GPIO21 still
  reports the raw level, per the tool contract in `03-config.md`.
- `sys_adc_read(channel, *mv)`: One-shot ADC1 read.
- `sys_wdt_reset()` / `sys_wdt_add(task)`.
- `sys_brownout_pending()`: Returns true if the reset cause was a brownout
  (`esp_reset_reason()`), so `app` can log it. Does not touch `boot_failures`;
  that counter is incremented once in §3.1 step 1 regardless of cause (§3.2).

`sched` calls `gpio_isr_handler_add()` itself after `sys_gpio_edge_config()`, so the
ISR belongs to the trigger engine and `sys` stays a pin/adc/watchdog wrapper with no
knowledge of triggers (§5.1).

### 2.1 Cross-module ports

Four function-pointer types carry every dependency that the layer law
(`08-code-architecture.md` §1) forbids as a direct call. Each is owned by its caller,
implemented by a lower module or by `app`, and installed at boot. Nothing else crosses
a layer boundary.

```c
typedef struct {
    void (*on_message)(const char *server_id, const char *topic,
                       const uint8_t *payload, size_t len);
} mqtt_sink_t;                        /* mqtt owns, sched implements */

typedef void (*agent_run_done_cb)(esp_err_t err, void *user);
typedef esp_err_t (*agent_submit_fn)(const char *source, const char *prompt,
                                     agent_run_done_cb on_done, void *user);
                                      /* sched/tools own, agent implements */

typedef struct {
    const char *prefix;
    bool       (*present)(void);
    esp_err_t  (*dispatch)(const char *name, const json_node_t *args,
                           json_doc_t **result);
} tool_provider_t;                    /* tools owns, each domain implements */

typedef struct {
    void (*on_frame)(const json_node_t *frame);
} link_sink_t;                        /* link owns, app implements */
```

| Installer | Installed | Call site |
|---|---|---|
| `mqtt_set_sink()` | `sched` via `app` | every inbound MQTT message |
| `sched_set_submit()` | `agent_submit` via `app` | every trigger that starts a run |
| `tools_set_submit()` | `agent_submit` via `app` | `event.emit` starting a run |
| `link_set_sink()` | `app` | every inbound frame, routed by `type` |

A module never includes a header from a higher layer to reach one of these. `app` is
the only place that names both sides.

## 3. Task Model & Boot Sequence

### 3.1 Boot Sequence
The device follows a strict linear boot to ensure dependencies are met.

1. **ROM Stage**: USB-Serial/JTAG enumerates; logs sent to UART0 (D6/D7). Early ROM and bootloader text also reaches the CDC endpoint raw, where the bridge drops it: `04-protocol.md` §1.2 makes an undecodable line non-fatal.
2. **Hardware Init**: `sys` initializes GPIOs and Watchdog.
3. **Storage**: `store` registers `userfs` at `/fs`, then `wsfs` at `/workspace`.
   Failure to mount `userfs` is fatal (`E_STORAGE`); `wsfs` may be reformatted.
4. **Config**: `cfg` loads `config.json`, then `geo_init()` validates
   `geo.providers[]`.
   - → If `secret_ref` missing or the file will not parse → `E_CONFIG`.
     The boot guard (§3.2) decides what follows: below the threshold the device
     halts after reporting on `status`; at or above it the device enters safe
     mode, starts the link anyway and lets the bridge repair the file over USB.
     Without that escape a broken `config.json` would be unrecoverable, since
     `userfs` is read-only to tools and OTA is not planned.
5. **Networking**: `net` connects to best WiFi AP from `wifi.networks[]`.
6. **Clock**: `clock` syncs via SNTP → `ntp_synced` trigger fires.
7. **Link**: the driver claims the CDC endpoint — the secondary console writes through the same buffer, so there is no second owner — and `link_task` blocks on a 100 ms read. `hello` is routed to `app`'s sink, which answers `hello_ack`.
8. **MQTT**: `mqtt_init()` starts each enabled broker client. Connecting is
   asynchronous and does not block boot; a broker that never connects does not
   delay the agent.
9. **Scheduler**: `sched` loads `tasks.json` → `boot` trigger fires.
10. **Idle**: `app` enters main loop, waiting for `prompt`, `sched` or `mqtt.message` events. This is the step that clears `boot_failures` (§3.2). At M1 this is not implemented: `app_main` returns after starting the link, so `link_task` is what keeps the device reachable, and the boot counter legitimately stays at its incremented value because the idle step that clears it does not run yet.

### 3.2 Boot guard and safe mode

A config that parses but hangs, or a scheduled task that panics on every boot,
would otherwise loop forever with no way in: `userfs` is read-only to tools, OTA is
not planned, and the bridge cannot talk to a device that never reaches the link.
The boot guard is the recovery path, and it costs one NVS counter plus one flag.

**Counter.** `boot_failures` lives in NVS namespace `runtime` — the same counter
`01-hardware.md` §5 already names for telling a brownout reset from a commanded
one, so there is one boot counter, not two. It is incremented early in boot, before
`cfg` loads anything, via `store_nvs_set_int()`, and set back to `0` only when the
device reaches step 10 of §3.1 (idle). Reaching the link is *not* enough to clear
it: a task that panics once the scheduler starts would otherwise loop undetected.
A brownout therefore counts as a failed boot, which is correct — in both cases the
previous boot did not complete.

**Threshold.** `sys.safe_mode_after` (default `3`, range 2..10) consecutive failed
boots. It is a `config.json` key, not an NVS value, so when `config.json` will not
parse the device falls back to the default rather than locking itself out — see step
4 of §3.1. The counter already includes the boot in progress, so the current boot
enters safe mode once the *previously failed* boots reach the threshold: with the
default `3`, three failed boots put the fourth into safe mode (`07-roadmap.md` M0),
which is one increment past it. Entering a boot earlier would make the threshold
mean one fewer failure than its name says.

**Safe mode** changes the boot sequence in exactly four places:

| Step | Normal | Safe mode |
|---|---|---|
| 5. Networking | connect to best AP | **skipped** — radio stays off |
| 6. Clock | SNTP sync | skipped; clock stays unsynced |
| 8. MQTT | start brokers | skipped |
| 9. Scheduler | load `tasks.json`, fire `boot` | **`tasks.json` is not loaded or fired** |

Storage (step 3), config parsing (step 4) and the link (step 7) still run, so the
bridge can connect and diagnose. `status` reports `sys.safe_mode: true` and the
counter, and the device accepts the full command set — `config.get`, `config.set`,
`tasks.list`, `tasks.delete` and `fs.put` all work, which is what makes recovery
possible from the bridge alone.

While safe mode is active, anything that needs the network, the scheduler or the
agent loop is refused with `E_SAFE_MODE`: `prompt` and `tasks.run`. `reload` is
deliberately **not** refused — it is the way out. After repairing the cause with
`config.set` (or `fs.put`), `reload` re-reads config, resets `boot_failures` to `0`
and leaves safe mode without a reboot.

Boot counter arithmetic is `boot_guard_next_count(current)` then
`boot_guard_should_enter_safe_mode(count, sys.safe_mode_after)`; both are pure
functions, so the policy is unit-testable without flashing.


### 3.3 FreeRTOS Task Model

To maintain responsiveness, work is split across priorities:

| Task | Priority | Stack | Role |
|---|---|---|---|
| `link_task` | High | 4 KiB | Read/write USB CDC; fast framing to avoid buffer overflow |
| `agent_task` | Medium | 16 KiB | Serialized execution of LLM runs and tool calls; runs `geo_locate`, which blocks on a scan |
| `sched_task` | Medium | 4 KiB | Timer management and trigger dispatch |
| `net_task` | Low | 8 KiB | Background WiFi reconnection and SNTP sync |

`mqtt` has no task of its own: esp-mqtt runs a per-client event loop task
internally (one per broker). Handlers must not block, so inbound messages are
copied and queued to `sched_task`.

## 4. The Agent Loop

The agent process is **serialized**. Only one run is active at a time.

### 4.1 Execution Flow
1. **Input**: `prompt` arrives via `link` or `sched` triggers a task.
2. **Prompt Assembly**: `agent` loads `system_prompt` and merges it with
   history.
3. **LLM Round**:
   - `llm` sends payload to provider.
   - If `E_LLM_UNAVAILABLE` → try next provider in `llm.providers[]`.
4. **Tool Dispatch**:
   - If response contains `tool_calls`:
     - Check `tools.allow_dangerous` for `gpio.write`/`fs.write`.
     - Check `tools.http_allow_hosts` for declarative HTTP tools.
     - Dispatch: `Built-in` → `Declarative` → `MCP (mcp.<server>.<tool>)`.
     - Collect result → Append to history → Repeat step 3.
5. **Close-out check**: Before finalizing, inspect the todo tree via
   `agent_todo_phases()` and the rounds used. The check fails in two cases:

   - **Open tasks.** The tree holds a `pending`, `in_progress` or `blocked` task.
     Append `Close out your todo list before answering. Every task must end completed, or abandoned via drop if you are deliberately not doing it. No task may be left pending, in_progress or blocked.`
   - **Untracked work.** The tree is empty and the run has already used two or
     more tool rounds. Append `This run took several steps but opened no todo list. Open one with init and record what you did and what remains, then close it out.`

   While either case holds and rounds remain, do not finalize: append the message,
   take one more round, then re-run the check. The check consumes
   `llm.max_tool_rounds` like any other round, so it cannot spin forever. Once the
   budget is spent the run finalizes regardless and `agent.final` reports
   `todo_open: true`; it is `false` only when neither case held at the final check.

   A single-round run that never calls `todo` is compliant, and so is an empty tree
   when the run did no more than one tool round: a question answered in one step
   needs no list. The untracked-work case is what makes "use `todo` for any
   multi-step task" a loop control rather than a request the model may ignore.
6. **Finalization**:
   - Send `agent.final` via `link`. Its `todo_open` field (boolean, always
     present) is `true` only when open tasks forced the close-out round and the
     budget was exhausted, `false` otherwise.
   - Store conversation in PSRAM history buffer.

### 4.2 Fallback & Error Handling
| Failure | Response | Code |
|---|---|---|
| WiFi Lost | `net` attempts reconnect; `agent` returns error | `E_WIFI` |
| LLM Down | Try fallback providers; if all fail → error | `E_LLM_UNAVAILABLE` |
| LLM call cap reached | No provider is tried; the call is never sent, so the daily budget cannot be exceeded by a fallback chain | `E_LLM_RATE` |
| Provider 400 (bad model, wrong `token_param`, rejected `extra_body`) | Not retried in place: the next provider is tried, since a 400 is per-entry config, not a transient fault | `E_LLM_UNAVAILABLE` after the chain is exhausted |
| MCP Timeout | Terminate specific tool call; return error to LLM | `E_MCP` / `E_TIMEOUT` |
| Tool Crash | Isolate tool; return `E_TOOL` to LLM | `E_TOOL` |
| Buffer Full | Truncate oldest history entry in PSRAM | — |
| Close-out failed at budget end (open tasks, or untracked multi-round work) | Finalize anyway; `agent.final` reports `todo_open: true` | — |

## 5. Scheduler & Triggers

The scheduler implements the frozen triggers from `docs/00-overview.md` §5.

### 5.1 Trigger Semantics
 - `boot`: Fires once during boot sequence after `sched_init()`.
 - `wifi_connected`: Fires when `net` confirms association.
 - `ntp_synced`: Fires when `clock` receives first valid SNTP update.
 - `serial_connected`: Fires when `link` detects the host has opened the port.
 - `gpio`: Fires on edge detection (rising/falling) of a configured pin.
   **Path:** `sched` installs the handler itself with `gpio_isr_handler_add()` and uses
   `sys` only to configure the pin (`sys_gpio_edge_config(pin, edge)`, an L0 call).
   `gpio_isr_handler_add()` registers a handler *function*; the handler it registers
   does one thing — `xQueueSendFromISR()` into a `sched`-owned queue — and returns. The
   `sched` task drains that queue and calls `sched_trigger_event("gpio", pin_id)`
   there, not in ISR context. No ISR work, no upward call.
   This keeps the edge L2 → L0; `sys` never learns what a trigger is.
 - `mqtt_message`: Fires when an inbound message matches §5.4. Inert until that
   broker is connected.
 - `event`: Fires when `event.emit` names it (§5.5).
 - `schedule`: Fires based on the `kind` (`at` or `every`) in `tasks.json`.
 - `manual`: Fires only when `tasks.run` command is received via `link`.

### 5.2 Schedule semantics and missed fires
Stored in `/tasks.json`.

Two `kind` modes:
1. `at`: fires at a specific ISO-8601 instant, or daily at a bare `HH:MM:SS`
   local wall-clock time. It fires at the next occurrence; a synced NTP clock is
   required, and before the first sync the task is inert (not counted as missed).
2. `every`: fires at a fixed interval after `first_at` (Unix epoch seconds) or,
   omitting `first_at`, after boot. Requires a synced NTP clock.

Missed-fire policy:
- `skip` (Default): If the device was off/offline during the scheduled time,
  the event is ignored.
- `run_once`: The task fires immediately upon the next successful boot/sync.

### 5.3 Schedule Example
A task scheduled to fire every 1 hour, starting at a specific time, and
repeating forever:

```json
{
  "name": "hourly-report",
  "trigger": {
    "type": "schedule",
    "kind": "every",
    "interval_s": 3600,
    "first_at": 1767225600,
    "repeat": { "mode": "forever" },
    "missed": "skip"
  },
  "prompt": "Summarize the last hour of GPIO events."
}
```

### 5.4 MQTT message trigger matching

A `mqtt_message` task matches an inbound message on four fields:

| Field | Rule |
|---|---|
| `server_id` | exact match; must exist in `mqtt.servers[]` |
| `topic` | exact match, or a trailing `*` as the final level, which matches exactly one level (`home/sensor/*` matches `home/sensor/a`, not `home/sensor/a/b`). MQTT `+` and `#` filters are not accepted here. |
| `payload_contains` | optional substring, case-sensitive, max 64 chars |
| `cooldown_s` | after firing, the task will not fire again within this window (default 5) |

Matching is evaluated most-specific-first (exact `topic` before wildcard), and a
message may match several tasks. Matched tasks run serialized through
`agent_task`, never reentrantly, so a burst of messages queues rather than
overlapping. Payloads are untrusted input: they reach the model as a task
prompt, so the prompt wrapper delimits them (`06-security.md` §6.4).

```json
{
  "name": "garage-door-alert",
  "trigger": {
    "type": "mqtt_message",
    "server_id": "home",
    "topic": "garage/door/state",
    "payload_contains": "open",
    "cooldown_s": 60
  },
  "prompt": "The garage door reported open. Check the last known state and summarize."
}
```

An `mqtt_message` task is inert until `mqtt` reports that broker connected; a
message arriving while the agent is busy is queued, not dropped, up to the
scheduler queue depth.

### 5.5 `event` trigger

An `event` trigger matches an emission from `event.emit` on the `name` field.
Matching is exact and case-sensitive on `[a-z0-9._-]{1,32}`. Every enabled task with
a matching `name` is considered and fired through the same serialized path as any
other trigger.

An `event.emit` call also emits an `agent.event` frame with `task_hits` (the integer
count of matching tasks fired). An emission that matches no tasks is not an error
and still produces the frame with `task_hits: 0`.

## 6. MCP Dispatch

The Model Context Protocol (MCP) is implemented as a direct-to-server
HTTP bridge.

1. **Discovery**: `agent` identifies tools starting with `mcp.`.
2. **Connection**: `mcp` module opens a TLS connection to the server URL in
   `mcp.servers[]`.
3. **Request**: Sends a JSON-RPC request to the remote server.
4. **Result**: Returns the tool output to the LLM.
5. **Isolation**: An `E_MCP` error in one server does not affect built-in
   tools or other MCP servers.

Cross-references: Protocol envelope in `docs/04-protocol.md`; config keys in
`docs/03-config.md`; hardware pins in `docs/01-hardware.md`.

## 7. Tools

The tool surface is documented per domain in `docs/tools/`. This section keeps only
what belongs to the firmware contract: how definitions are assembled and sent, and
the one write carve-out that touches storage internals.

### 7.1 Tool definitions sent to the model

`tools_json` is built at run start from three sources, in this order: built-ins,
declarative `/tools/*.json`, and MCP tools discovered at `tools/list`. Each entry is
one `{"type":"function","function":{...}}`. Every built-in carries a description and
a parameter schema of the same shape declarative tools require (`03-config.md`
§Tool schema): `type: "object"`, `properties`, `required`, plus `enum` for a closed
value set; nothing else.

Two built-in schemas nest: `task.set` and `todo`. The `task.set` `task` object
mirrors `/tasks.json` verbatim (`03-config.md` §Task schema) so the model authors a
task in the shape the file stores, and `todo`'s `list` is an array of `{phase, items}`
objects next to an `op` enum (`tools/todo.md`). `sys.diagnostics` takes one optional
`scope` enum (`tools/sys.md`); the other twenty-two built-ins take a flat object of
scalars, and no schema uses `oneOf`, `anyOf`, or `$ref`. That subset is
all any provider in the compatibility table is guaranteed to accept.

The per-tool schemas are canonical in each domain file, not here:

| Domain | File | Tools |
|---|---|---|
| GPIO | `tools/gpio.md` | `gpio.read`, `gpio.write` |
| ADC | `tools/adc.md` | `adc.read` |
| I2C | `tools/i2c.md` | `i2c.scan`, `i2c.read`, `i2c.write` |
| Time | `tools/time.md` | `time.now` |
| Filesystem | `tools/filesystem.md` | `fs.list`, `fs.read`, `fs.write`, `fs.delete` |
| Web | `tools/web.md` | `http.get`, declarative `/tools/*.json`, `weather_now`, `web_search` |
| Skills | `tools/skills.md` | `skill.read` |
| Tasks | `tools/tasks.md` | `task.list`, `task.set` |
| Todo | `tools/todo.md` | `todo` |
| Events | `tools/events.md` | `event.emit` |
| Geo | `tools/geo.md` | `geo.locate` |
| MQTT | `tools/mqtt.md` | `mqtt.list`, `mqtt.publish` |
| Memory | `tools/memory.md` | `memory.get`, `memory.set`, `memory.list`, `memory.delete` |
| System | `tools/sys.md` | `sys.diagnostics` |

A tool call whose arguments fail schema validation is rejected as `E_TOOL` without
executing, and no partial effect is applied.

### 7.2 `task.set` and `store_task_write()`

This is the only **tool** allowed to write to `userfs`. It validates the input
against the task schema in `docs/03-config.md` §tasks and refuses if the resulting
count would exceed `tools.task_max`. Result returns the task id. This is a deliberate
carve-out to `06-security.md` §6.1 barrier 3.

The tool is **add/update only**. There is no delete op and no field that reads as
one: an unrecognised field fails schema validation, and `enabled: false` retains the
entry. Deletion is host-only through the `tasks.delete` command (§4.7 of
`04-protocol.md`), so a compromised model cannot remove a user's scheduled task.
Authority split and argument semantics: `tools/tasks.md`.

 `store_task_write()` writes the whole `/fs/tasks.json` atomically by writing to a
 temp file then renaming; this is held under a mutex and refused during `reload`.
 A write refused during `reload` returns `E_BUSY`; this is the only failure of
 `task.set` that is neither `E_TOOL` nor `E_STORAGE`. Its callers are exactly
 three: the `task.set` tool, and the `tasks.set` / `tasks.delete` commands.
 `tasks.delete` additionally purges that task's keys from NVS namespace `runtime`
 before the rename commits, so a partially applied delete cannot leave orphaned
 `fired_count` state.

## 8. Simulator

`firmware/tools/run_qemu.sh` boots the firmware under `qemu-system-xtensa` with no
board attached. It builds a QEMU-only image into `build/qemu/`, merges it, seeds
`userfs`, and runs the machine. The board `sdkconfig` is never read or written; the
script layers `sdkconfig.qemu.defaults` over `sdkconfig.defaults`.

```bash
firmware/tools/run_qemu.sh                 # build, then boot
firmware/tools/run_qemu.sh --no-build      # boot what is already in build/qemu/
firmware/tools/run_qemu.sh --keep          # keep flash state between runs
firmware/tools/run_qemu.sh --pty           # UART0 on build/qemu/uart
QEMU_BIN=/path/to/qemu-system-xtensa firmware/tools/run_qemu.sh
```

The default mode binds UART0 to the invoking terminal's stdin and stdout, so there is
no pts and `--no-build` creates none. `--pty` allocates one instead and links it at
`build/qemu/uart`, a stable path rather than a `/dev/pts/N` number that changes each
run; that is what `bridge/link/serial_transport.py` opens. There is no argument for
choosing the number, and a stray argument is forwarded to QEMU.

A pty is not a log. A reader that opens the slave before the guest writes receives
the whole 4,560-byte boot log; one that attaches later receives none, and this is the
reader's doing, not the pty's: pyserial calls `tcflush(TCIFLUSH)` on open
(`serialposix.py`, `_reset_input_buffer`), so a buffer the kernel did hold is
discarded at the moment the bridge attaches. Holding the slave open in the script does
not help for the same reason. QEMU's chardev `logfile` cannot recover it either, since
it only copies bytes the backend accepted. Reading the log means the default mode. The
guest also writes no reply until `link` exists (§3.1), which makes `--pty` a proof of
the transport plumbing, not of the protocol.

The script prints the link path before QEMU's own redirect line, so the mode does not
look inert once the guest goes quiet.

Each run normally re-merges the image, which resets NVS. `--keep` reuses the
existing one, and that is how the boot guard is exercised from here: three
`--keep` runs count failures 1→2→3 and the fourth reports
`boot: safe mode, failures 4` (§3.2).

Exit the default mode with `Ctrl-A` then `x`. `--pty` runs with no monitor, so the
terminal carries nothing but QEMU's two startup lines and `Ctrl-C` stops the guest.

Four settings are not optional, and each one has a failure that names something
else:

| Setting | Without it |
|---|---|
| `-m 8M`, not 32M | PSRAM mapping shares the MMU vaddr window; 32M leaves none for flash, and the partition table cannot be read |
| `ssi_psram is_octal=true` | `PSRAM chip is not connected, or wrong PSRAM line mode`, then boot loops |
| `CONFIG_ESPTOOLPY_FLASHMODE_DIO` | QEMU's `gd25q64` model has no Gigadevice branch in `WRSR`, so the QE bit never latches; a QIO app asserts in `esp_flash_init_default_chip` |
| 8 MiB padded image | `esp32s3_init_spi_flash` selects the flash model by exact drive length, 2/4/8/16 MiB only |

What the simulator proves and what it does not:

| Provable | Board-only |
|---|---|
| partition table parse, NVS namespaces, boot counter and safe mode across reboots | GPIO21 LED polarity: QEMU's GPIO write handler is a stub |
| `userfs` and `wsfs` mounts, including the once-only `wsfs` reformat | LittleFS wear behaviour, brownout, WiFi failover |
| `app_main()` reaching `store_mount()` and the fit assertions in §3.1 | `ping` latency, and any code path that needs a real radio |

The seeded `userfs` is a throwaway: three directories and a placeholder
`config.json`. The real image builder is the M4 `bridge/tools/build_userfs.py`
(`03-config.md` open question 8); until it lands, a freshly flashed board halts on
a `userfs` that is blank and cannot be formatted (§3.1).
