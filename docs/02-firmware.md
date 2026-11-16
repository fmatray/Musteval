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
| `tools` | L2 | Tool registry (built-in, declarative, remote) | `store`, `cfg`, `net`, `geo`, `mqtt`, `mcp`, `sched`* |
| `llm` | L1 | HTTPS client, provider failover chain, prompt assembly | `net`, `cfg` |
| `skills` | L1 | Prompt template loading from `/skills/*.md` | `store` |
| `mcp` | L1 | Remote MCP server communication (HTTP/JSON-RPC) | `net`, `cfg`, `store` |
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
- `cfg_secrets_reload()` / `cfg_secrets_clear()` / `cfg_redact_line(line, cap)`:
  the RAM snapshot that lets a log line be scrubbed without an NVS read on the
  logging task's stack. `cfg_load` and `cfg_store_secret` call `cfg_secrets_reload`,
  so a `/secret set` is redacted from the next line on rather than the next boot.
  The snapshot holds values only (never key names) and never leaves RAM. `cfg` owns
  it because it is the only module allowed to open the `secrets` namespace
  (`08-code-architecture.md` §3); `link` asks here rather than resolving a secret
  itself. The storage half is `cfg_doc.c`, pure and host-tested; only the NVS
  enumeration is in `cfg.c`.
- `cfg_clear_secret(key, &deleted)`: erase one key from the `secrets` namespace and
  refresh the redaction snapshot, so a value that is gone stops being scrubbed from
  log lines. `deleted` is false when the key was already unset — an idempotent call,
  not a failure (`04-protocol.md` §4.31). Absence arrives as
  `ESP_ERR_NVS_NOT_FOUND`, not `ESP_ERR_NOT_FOUND`: `nvs_erase_key` reports its own
  code, and a handler that tests the generic one answers `E_STORAGE` to a second
  delete of the same key. `store.h` states which of the two each entry point uses.
- `cfg_ref_names_key(doc, key, where, cap)`: the walk behind `secret.delete`'s
  refusal. True when any `*_ref` member of `doc` is `nvs:<key>`, with `where` naming
  the member that holds it. It reads the same `*_ref` suffix rule `cfg_validate_refs`
  applies, rather than a second key table, so a new reference anywhere in the schema is
  covered without an edit here. Pure and host-tested (`cfg_doc.c`), because the rule it
  protects — a document never names a secret that is not set — is `03-config.md` rule 9
  and is worth proving without a board.

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
- `store_read_text(agent_path, offset, buf, cap, len, total, truncated)`: read
  a window of text under `/workspace/` starting at byte `offset`; UTF-8 validated,
  invalid sequences return `E_STORAGE`. `offset` past the end of the file is
  `E_STORAGE`, and a window that begins inside a codepoint skips its continuation
  bytes, so a caller paging by `bytes` never splits one and never has to align
  anything itself. `total` is the file's whole length, which is what `04` §4.18
  reports as `bytes` even when the cap stopped the read short; `truncated` is
  `offset + len < total`. Paging exists so `fs.read` can reach past its 2048-byte
  window in a `/workspace/` that admits 64 KiB files.
- `store_resolve_sandbox_path(in, out, out_len)`: the listing admission. Same
  canonicalisation as `store_resolve_write_path`, except that `/workspace` itself
  is admitted, because `04` §4.17 defaults `sandbox.list` to it. It exists so a
  caller cannot reach the root by passing the file-target resolver, whose refusal
  of `/workspace/` is correct there and wrong here.
- `store_list_dir(agent_path, out, cap, count)`: list one directory under
  `/workspace/`, sorting directories first then names. A listing exceeding the
  cap returns `E_STORAGE`. The root `/workspace/` is a legal listing target,
  though not a legal file target; `store_resolve_sandbox_path` is the admission
  for it, and the resolver is applied to entries within.
  The staging file of a live transfer is never listed, because `04` §4.16
  forbids exposing partial state: an entry whose path equals the current
  staging path is skipped, and nothing else is.
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
- `store_delete_file(agent_path, recursive, entries)`: unlink a sandbox file, one empty
  directory, or — with `recursive` — a whole subtree (§4.19).
- `store_mkdir(agent_path, created)`: create one directory (§4.21).
- `store_move(from, to)`: rename or relocate a sandbox entry (§4.22).
- `store_is_dir(agent_path, out)`: report if a sandbox path is a directory.

Pure helpers in `store_sandbox.c` implement internal logic: `store_entry_cmp`
for sorting, `store_entry_name_ok` for character validation,
`store_decode_chunk` for base64/UTF-8 normalization, `store_is_staging_entry`
for the listing skip above, and `store_utf8_prefix` for the UTF-8 scan
`store_read_text` runs. `store_utf8_prefix` returns the longest
valid prefix and refuses a lone continuation byte, an overlong form (`C0`/`C1`,
`E0 80..9F`, `F0 80..8F`), a surrogate (`ED A0..BF`) and anything above
`U+10FFFF` (`F4 90..`, `F5`..`FF`). A prefix shorter than the buffer is a
truncation only if the remaining bytes begin a sequence the cap cut in half:
those shorten the text and set `truncated`, they do not refuse the file. Being
pure, it is host-tested rather than reached through a mounted filesystem.
`store_is_staging_entry(vfs_dir, entry_name, stage_path)` is exact-match for the
same reason: a prefix compare would hide `a.txt.new.new` and a suffix append on
an entry already carrying `.new` hides nothing at all, which is the shape of the
bug it exists to prevent.
- `store_delete_action(is_dir, entries, recursive)`: decide if a delete is a
  file, empty dir, recursive tree, or a refusal (§4.19).
- `store_count_entry(count, name)`: fold one `readdir` name into a child count.
- `store_move_verdict(source_exists, target_exists, from, to)`: decide if a
  rename is allowed, refuse root, or report target taken (§4.22).
- `store_nvs_set_int(key, val)` / `store_nvs_get_int(key, &val)`: Access
  NVS namespace `runtime`.
valid prefix and refuses a lone continuation byte, an overlong form (`C0`/`C1`,
`E0 80..9F`, `F0 80..8F`), a surrogate (`ED A0..BF`) and anything above
`U+10FFFF` (`F4 90..`, `F5`..`FF`). A prefix shorter than the buffer is a
truncation only when the remaining bytes begin a sequence the cap cut in half:
those shorten the text and set `truncated`, they do not refuse the file. Being
pure, it is host-tested rather than reached through a mounted filesystem.
`store_is_staging_entry(vfs_dir, entry_name, stage_path)` is exact-match for the
same reason: a prefix compare would hide `a.txt.new.new` and a suffix append on
an entry already carrying `.new` hides nothing at all, which is the shape of the
bug it exists to prevent.
- `store_nvs_set_int(key, val)` / `store_nvs_get_int(key, &val)`: Access
  NVS namespace `runtime`.

`net.ping` also creates a short-lived task, one per command: four ICMP probes take up
to 2.8 s, and `answer_command` runs on `link_task`'s 12 KiB stack, which must answer
`pong` inside 1 s even while the ping is in flight (`04-protocol.md` §3.7). The task
copies its host and `reply_to`, emits one `command.result`, and deletes itself. A
second `net.ping` while one is in flight is `E_BUSY` (`04-protocol.md` §4.25).

- Exactly three APIs write `userfs`: `config.set`, which targets `/fs/config.json`
  and is not reachable through any tool path; `store_write_image_chunk()`, which
  targets `/fs/tasks.json` through the staged-write protocol (§4.20) and is reachable
  from the `task.set` tool and the `tasks.set`/`tasks.delete` commands (§7.2); and
  the host-only `fs.put` command,
  which targets any allowlisted path not shipped in the image (`04-protocol.md`
  §4.16). Every other write target is `wsfs`, and only `task.set` is model-reachable.

### `net` — WiFi and transport
- `net_init()`: Read `wifi.*`, rank `wifi.networks[]` by `priority` and resolve each
  `password_ref`. Does not touch the radio: a config error is reported before the
  station costs power. Returns `E_CONFIG` when no network has both an SSID and a
  resolvable password.
- `net_connect()`: Bring the station up and join the best visible AP, highest
  `priority` first, `retry_backoff_ms` between attempts and `connect_timeout_ms` per
  attempt. Blocks for at most the sum of those bounds. Returns `E_WIFI` when every
  configured AP refused; the device stays reachable over the link, which is the point
  of continuing. Not every `WIFI_EVENT_STA_DISCONNECTED` is a refused attempt:
  - The `esp_wifi_disconnect` it issues before configuring the next AP raises one with
    reason `WIFI_REASON_ASSOC_LEAVE` (8) or `WIFI_REASON_STA_LEAVING` (36), depending
    on how far the association got.
  - The driver also raises one with reason `WIFI_REASON_UNSPECIFIED` (1) for that same
    leave, dispatched after the flag that marks it has been cleared.

  Neither may raise the failure the wait is watching for, or every attempt is scored
  refused before the radio can associate — observed on target as `E_WIFI` logged with
  "timeout 15000 ms" ten milliseconds into the wait, while the association completed
  a second later. A refusal that carries its own reason (`AUTH_FAIL` 202,
  `NO_AP_FOUND` 201, ...) is scored. The predicate is `net_disconnect_ignored`,
  host-tested against the driver's real code values.
- `net_connected()`: Whether an IP is assigned right now. A `WIFI_EVENT_STA_DISCONNECTED`
  clears the state and the reported `ssid`/`ip` before it retries, so a dropped link
  reads as `connecting` with no identity rather than as a live network with a stale
  address.
- `net` reconnects from the disconnect event only once a link has been *held*
  (`WiFi Lost`, §4.2). A boot in which every configured network refused stays
  `failed`: retrying there would spin `connecting` against a config that cannot
  associate, and the operator's next step is the link, not a wait.
- `net_status(&st)`: The `wifi` facts `status` reports (`04-protocol.md` §3.9):
  `state`, `ssid`, `rssi`, `ip`, `failovers`. Nothing is fabricated before an
  association: `ssid`, `rssi` and `ip` are NULL, which encodes as JSON null.
- `net_post(url, content_type, authorization, body, body_len, timeout_ms, &resp)`:
  `POST <url>` over TLS. Verifies the chain against the bundled roots and never falls
  back to plaintext (`06-security.md` T11); a URL that fails verification returns
  `ESP_ERR_ESP_TLS_CERTIFICATE_VERIFY_FAILED`, which `llm` maps to
  `E_LLM_UNAVAILABLE`. Follows no redirect. A body over 16 KiB is refused with
  `ESP_ERR_INVALID_SIZE` rather than truncated, so `llm` never parses half a reply.
- `resp.body` / `resp.body_len` carry the reply for **every** status, 4xx and 5xx
  included. The body is collected by the HTTP event handler while the request is
  performed, not by a read afterwards: `esp_http_client_perform` consumes the body
  through its own parser and resets the buffer as it returns, so a read after it
  yields 0 for a 200 as readily as for a 400. `net_exchange` read the body this way
  once, so every reply it returned was empty; that is why an `E_LLM_UNAVAILABLE`
  arrived as a bare `http 400` with no reason in it.

`net` knows nothing about providers: an HTTP POST is transport, and what a body means
belongs to `llm`. `net_rank()` and `net_backoff_ms()` are pure and host-tested; the
radio and TLS paths are board-only, because a test against a stubbed `esp_wifi` proves
nothing about the code that ships.

### `clock` — Wall clock and SNTP
- `clock_init()`: Read `ntp.servers[]` and the sync interval, and put the wall clock
  in UTC. Called at boot step 6. A missing `ntp` block is not an error: the device
  boots with an unsynced clock rather than refusing to start.
- `clock_sync()`: Wait for the first SNTP reply, up to `ntp.timeout_ms` or the
  default 3000 ms. Returns `E_TIMEOUT` when no server answered. The caller logs it
  and boots on; `ntp.synced` fires only on success (§5.1).
- `clock_synced()`: Whether the clock has been stepped by SNTP at least once since
  boot. `hello_ack.time_synced` and `time.synced` both report exactly this.
- `clock_now()`: Unix seconds, UTC. Before a sync this is the boot-relative clock,
  which is why every consumer that cares checks `clock_synced()` first.
- `clock_uptime_ms()`: Monotonic milliseconds since boot, from `esp_timer`, **not**
  `gettimeofday`: the SNTP step moves the wall clock, so a derivative of it would
  jump and `duration_ms` would be a lie.
- `clock_tz()` / `clock_utc_offset_s()`: The rendering zone for `time.now` and the
  tool's `tz` argument. Storage is always UTC; only presentation shifts.
- `clock_next_interval_ms(interval_s)`: The clamped poll interval, 60 s to 86400 s, so
  a config typo cannot turn the device into an SNTP flood or silence it for a year.

`clock` is L0 rather than a capability: `llm`, `sched`, `geo` and `agent` all need the
time, and as a platform service it needs no port.

### `llm` — Language Model
- `llm_query(&request, response_buf, len, &provider)`: Send one prompt through the
  provider chain. Returns `E_LLM_UNAVAILABLE` on a transport or provider failure,
  which triggers provider failover; returns `E_LLM_RATE` when the budget check
  refuses first, which does **not** trigger failover — a different provider would
  spend the same budget; returns `ESP_ERR_NOT_FINISHED` when `llm_abort()` cancelled
  the call, which also stops the chain. `provider` receives the index that answered,
  or -1.
- `llm_budget_charge()`: Called once per request *attempt that opens a socket*,
  before the exchange. A provider skipped for a cheap pre-check — no resolved
  `api_key`, a `base_url` too long for the endpoint buffer, a body that does not fit
  — is not an attempt and is not counted, so misconfiguration cannot consume the
  day's calls. Refuses with `E_LLM_RATE` when either fixed UTC window
  (`llm.max_calls_per_hour`, `llm.max_calls_per_day`) is exhausted, so a fallback
  chain cannot multiply the spend. A refused call is not counted. Each window is
  stored as a counter plus its bucket index (`unix / 3600`, `unix / 86400`); a
  bucket mismatch resets the counter instead of trusting a stale value
  (`03-config.md` §`llm`).
- `llm_retry_after(err, status, retry_after_s, attempt, &wait_ms)`: Whether a failed
  attempt is repeated against the same provider, and the wait before it. Only a
  transient failure retries: a transport error, or 408/429/5xx. A 400, 401, 403 or
  404 is per-entry configuration and repeating it spends budget to learn nothing.
  The provider's `Retry-After` is honoured in integer-seconds form and clamped to
  `LLM_RETRY_MAX_MS`; the HTTP-date form is ignored in favour of
  `LLM_RETRY_BASE_MS`. At most `LLM_RETRY_ATTEMPTS` (one) extra attempt per
  provider, so a chain of N costs at most 2N requests. Each retry is another socket,
  so it is charged. Pure and host-tested in `llm_body.c`.
- `llm_abort()`: Cancels the request `llm_query` is blocked in, if any, so a `cancel`
  frame reaches a call already on the wire instead of waiting out `timeout_ms`.
  `agent_cancel()` calls it. An aborted call makes `llm_query` return
  `ESP_ERR_NOT_FINISHED`, which the loop reports as `E_CANCELLED` — not as a
  provider failure, and never as a reason to try the next provider.
- `llm_budget_counters(hour, day)`: Reads the counters for `status`
  (`04-protocol.md` §3.9). Counters live in NVS and survive reboot.
- `llm_assemble_prompt(&request, extra_body_json, out, cap)`: Merge system prompt,
  history and the tool array into the request body described below. Returns the
  length written, or 0 when the body does not fit — a truncated body is a 400 the
  caller cannot tell from a config mistake, so it is refused instead.
- `llm_flatten_members(extra, out, cap)`: Emits one `extra_body` object's members as
  a single flat object into `out`, and returns the length written or 0 when it does
  not fit. `llm_init` stores the result in `llm_provider_t.extra_body`, so the
  assembler merges members that are already at the request root. Emitting
  `{"extra_body":{...}}` and then storing that text was the defect: the assembler
  strips one level of braces, so the wrapper's own key survived and every member
  under it - the thinking level included - was inert on the wire.
- `llm_endpoint(base_url, out, cap)`: `base_url` with `/chat/completions` appended
  verbatim. No normalisation: a base URL that legitimately ends in `/` keeps it.
- `llm_next_provider(current, count)`: The next index to try, or -1 when the chain is
  exhausted. The chain is walked once per request and never wraps, so a flapping
  provider cannot spend the budget in a loop.
- `llm_counters(&hour, &day)` / `llm_limits(&hour, &day)` /
  `llm_configured(&id, &model, &thinking)` / `llm_last_answer(&id, &model)`: The
  facts `status` reports (`04-protocol.md` §3.9). `llm_configured` answers "what
  will the next call use" — the chain's first entry, named from boot — while
  `llm_last_answer` answers "did the last call succeed", and `llm.available` is
  that answer rather than the other one.
- `llm_last_error()`: Why the chain last failed, for `E_LLM_UNAVAILABLE`'s
  `error.message`. When the last attempt got an HTTP status and a body, the message
  carries the provider's own words, clipped to `LLM_ERROR_CLIP`:
  `ollama-cloud: http 400: {"error":"model \"gemma4:31b\" not found"}`. The status
  alone says only that the request was wrong, never which member was wrong, and the
  provider is the only party that knows — a 400 naming `model` points at the model
  dropdown, one naming `tools` at the tool array, one naming `reasoning_effort` at
  the thinking level the wizard wrote. The body is read before it is freed, which is
  the whole reason this module keeps a copy at all.

- `llm_init()` **is re-runnable and is the only way a `llm.providers` edit goes
  live.** `config.set` on the `llm` block calls it before replying, because the
  service start-up is otherwise the sole caller and a written document would sit
  unread until the next `reload`. It holds no state an in-flight request reads (the
  provider entry is copied per attempt), so re-running it cannot disturb a run that
  is already posting. It writes no last-call fact: `s_available`, the answered id and
  model, and `s_last_error` belong to `llm_query`, because they describe a call and
  `llm_init` makes none. Resetting them here made a `config.set` on any `llm.*` path
  — including one that changed nothing — flip `llm.available` from true to false, and
  the bar announced `llm down` for a provider that was answering.
- A **cancelled** call is likewise not a provider failure, so `ESP_ERR_NOT_FINISHED`
  leaves the last-answer facts untouched. `agent` reports it as `E_CANCELLED` from its
  own `s_cancel`; writing `cancelled` into `s_last_error` also left that string as the
  `E_LLM_UNAVAILABLE` message the next genuine failure would have shown.

The budget, the body and the chain order are pure and host-tested (`llm_budget.c`,
`llm_body.c`); `llm.c` resolves secrets, reads NVS and posts, and is board-only.

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
request's structure - and it holds at most four members, each a flat scalar. Which
member a provider wants, and which values it accepts, is `03-config.md` §Provider
compatibility's fact and is not restated here. `extra_body` is per provider and per
model, which is exactly the granularity that divergence has.

Tool calls are read from `choices[0].message.tool_calls[]` and results returned as
`role: "tool"` messages carrying `tool_call_id`. A provider that returns tool calls
in any other shape is out of scope.

Tool definitions are assembled at run start; the assembly rules and the
per-tool schemas are in §7.1 and `docs/tools/`.

**Capacity.** The assembled body — system prompt, tool array and history — must fit
one fixed buffer. The tool array alone is about 5 KiB for the frozen 26-tool
registry and the system prompt may be up to 8 KiB, so the body buffer is 16 KiB
(`NET_BODY_MAX`, the largest reply the transport holds). The history has its own
smaller cap, because a history allowed to fill the body would leave no room for the
tools and fail every later request. When the assembler still refuses, the run is
`E_LLM_UNAVAILABLE` and the loop drops history until the body fits, rather than
retrying a body that can never be built.

The tool array is the budget that fails first, not the history. `TOOLS_MAX_TOTAL` is
64, so the M5 surface — MCP servers plus declarative tools — can push the array past
the 8 KiB the system prompt already leaves, at which point no amount of history
dropping reclaims a body that cannot be assembled. Whatever adds tools at M5 must
also add the rule that sends a subset (`07-roadmap.md` M5).

### `agent` — Core Loop
- `agent_submit(prompt, len, source, reply_to, run_id_out, run_id_cap)`: Queue one
  prompt for the loop. The text is **copied** into the module's own buffer; the
  caller's pointer is not retained (the link task's stack is 12 KiB, so an 8 KiB
  prompt on it is a stack overflow). `run_id_out` receives the id **minted
  before** the enqueue, because the replies that start a run name it — `tasks.run`
  (`04-protocol.md` §4.5) and `fired` (§3.16) — and a refusal writes `""`; it may be
  NULL when the caller does not need the id. Returns `E_BUSY` when the queue
  (`AGENT_MAX_QUEUE`, two deep) is full — `04-protocol.md` §3.15. `source` records
  which trigger started the run, for `status`.
- `agent_cancel()`: Set the run's cancel flag and abort any request already on the
  wire (`llm_abort()`). A `cancel` frame therefore ends a run that is inside
  `esp_http_client_perform` instead of waiting out the provider's `timeout_ms`; when
  no request is in flight it takes effect at the next round boundary.
- `agent_status()`, `agent_start()`, `agent_todo_phases()`, `agent_todo_reset()`,
  `agent_history_reset(dropped)`.
  `agent_todo_phases()` returns the live todo phase/task tree. The list is state
  owned by this module, not by the `tools` module, so it outlives a tool call and
  survives across runs until `reload` or reboot. `docs/tools/todo.md` is the canonical
  description of the shape. The loop reads it for the close-out check described in
  §4.1 step 5.
- `agent_history_reset(dropped)`: forget the conversation (`04-protocol.md` §4.28) and
  report how many messages went through `dropped`. Refused with
  `ESP_ERR_INVALID_STATE` while a run is in flight, which the dispatcher reports as
  `E_BUSY`: the run appends to and trims this same buffer between rounds, so a clear
  landing mid-run would drop a `tool` message whose `tool_call_id` the assistant turn
  still names, and the provider refuses the next request. `agent_history_clear` is the
  pure half — `[]`, `*used = 2` — and is host-tested as the pair clear-then-append,
  because a clear that a following append cannot build on is a buffer the next prompt
  cannot use. The todo tree, the task list and the counters `status` reports are other
  owners' state and are untouched.
- `agent_todo_apply(args, out, why, why_cap)`: the `todo.apply` command's entry
  (`04-protocol.md` §4.27), and the `todo` tool's apply path — one function behind
  both, so an op cannot come to mean two things. `out` receives `tools/todo.md`
  §Result; `view` writes nothing. `todo_apply` commits with an 8 KiB non-atomic
  struct copy, so this module holds one mutex that both callers take: without it a
  `/todo` arriving mid-run would tear the list the model is reading.

The loop is entered only through `agent_submit`; there is no synchronous entry and
no per-call tool entry point. Dispatch to built-in, declarative or MCP tools is
`tools_call`, called from inside the loop's tool step.

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
- `link_log_frame(tag, msg, msg_cap, level, ts, t_ms, out, out_cap, written)`
  redacts `msg` in place before framing it (`06-security.md` §7 rule T2, `04` §3.10).
  The builder is the one path a log line takes off the device, so the scrub sits
  there rather than at each call site: a caller cannot forget it, and the host suite
  proves it through the real builder and the real parser. `msg_cap` is the buffer's
  whole size, because the mask is longer than a short secret. The scrub reads the
  snapshot `cfg_secrets_reload` fills at `cfg_load` and after each `secret.set`, so
  the log hook does no NVS work on the logging task's stack. `tag` is not scrubbed:
  it is a module name the firmware chose, never a provider-supplied value.

### `sched` — Scheduler
- `sched_init()`: allocate the state, the lock, the GPIO queue and the drain task.
  Reads nothing and arms nothing, so safe mode still answers `tasks.list` and
  `tasks.delete` (§3.2).
- `sched_start()`: load `/tasks.json`, arm the tick and the GPIO pins, then fire the
  `boot` trigger (§3.1 step 9). Never called in safe mode.
- `sched_reload()`: re-read `/tasks.json` and re-arm the pins. A file the build
  refuses leaves the armed schedule in place, so a bad edit never unschedules the
  device.
- `sched_command(name, args, out, why, why_cap)`: one `tasks.*` command
  (`04-protocol.md` §4.4). Every write is the whole-file rewrite, so a refused
  `tasks.set` leaves the previous document armed.
- `sched_trigger_bare(type)`: a transition the firmware observed — `boot`,
  `wifi_connected`, `ntp_synced`, `serial_connected`. Returns how many tasks fired.
- `sched_trigger_event(name, data)`: fire every `event` task named `name`, then emit
  `agent.event` with the hit count — even at 0 (§5.5). Returns the hits.
- `sched_trigger_mqtt(server_id, topic, payload)`: match inbound messages against
  `mqtt_message` tasks (§5.4).
- `sched_trigger_gpio_isr(pin)`: the registered handler's body. Queues the pin and
  returns; matching and firing happen on the drain task (§5.1).
- `sched_set_submit(fn)`: install the `agent_submit_fn` port (§2.1). Any trigger
  that starts a run — `boot`, `schedule`, `gpio`, `mqtt_message`, `event` — goes
  through `*fn`. `sched` never includes an `agent` header.
- `sched_stored_json(out)`: `/tasks.json` as stored, for `task.list`
  (`docs/tools/tasks.md`).
- `sched_trigger_json(parent, task)`: add a task's `trigger` object in the frozen
  shape. Public because `task.fired` (`04` §3.16) reports the same object, and one
  owner for the shape keeps the file, the list and the frame from drifting.

`sched` emits its own `task.fired` and `gpio.edge` frames, which is why `sched` →
`link` is a permitted L2 edge (`08-code-architecture.md` §1.1).

### `mqtt` — Brokers
Lives in `components/broker/`, not `components/mqtt/`: ESP-IDF already ships a
component named `mqtt` (the `esp-mqtt` submodule's directory), and a local
component of that name shadows it, which drops `mqtt_client.h` from the include
path. The public header is still `mqtt.h` and the module is still `mqtt`
everywhere else, so only the directory differs from the `components/<name>/`
layout in `08-code-architecture.md` §2.

- `mqtt_init()`: create one `esp_mqtt_client_handle_t` per `mqtt.servers[]`
  entry, up to `mqtt.max_servers`; subscribe to that server's `topics[]`.
- `mqtt_publish(server_id, topic, payload, len, qos)`: refuse `E_MQTT` unless
  `server_id` exists and `topic` is covered by that server's `topics[]`.
  Returns `E_MQTT` when the client is not connected. `E_MQTT` is the whole
  failure domain here; neither gate maps to `E_TOOL`.
- `mqtt_status(out, len)`: serialise `[{id,connected,subscriptions}]`, where
  `subscriptions` is that server's configured `topics[].filter` list — the set it
  subscribed to, which is what `04` §4.15 shows.
- `mqtt_set_sink(sink)`: install the `mqtt_sink_t` callback (§2.1). `mqtt` calls
  `sink->on_message(server_id, topic, payload, len)` for every inbound message and
  does not know who consumes it. `app` registers `sched`'s handler at boot, which
  in turn raises the `mqtt.message` frame through `link` and matches `mqtt_message`
  triggers.

Reconnect uses `wifi.retry_backoff_ms[]`. `tls: true` sets
`broker.verification`; there is no insecure fallback. Client contexts and the
per-server inbound buffer are internal SRAM, never PSRAM (DMA;
`01-hardware.md` §2). The inbound buffer is `MQTT_PAYLOAD_MAX + 1` bytes taken
from the heap when that server starts, not an array reserved for
`mqtt.max_servers` entries: a board with one broker pays for one. An inbound
payload larger than one RX read arrives as several `MQTT_EVENT_DATA` events,
which `mqtt` assembles before raising a single `mqtt.message`; a payload past
`MQTT_PAYLOAD_MAX` is emitted with `truncated: true` rather than dropped.

### `geo` — WiFi positioning
- `geo_init()`: validate `geo.providers[]`, resolve any `api_key_ref`.
- `geo_locate(geo_fix_t *out)`: blocking. Returns `E_CONFIG` when
  `geo.enabled` is false, `E_GEO` when every provider fails, `E_TIMEOUT` on
  `geo.timeout_ms`.
- `geo_last_error()`: the last failed provider and why, as
  `"<id>: <esp_err_to_name> (http <status>)"`, or empty after a success. The return
  code cannot distinguish a DNS miss from a refused handshake from a 500, and those
  are three different repairs; this is the string the tool reports in its place of
  "every provider failed".

Flow:

1. Return the cached fix when its age is under `geo.cache_ttl_s`.
2. `esp_wifi_scan_start` (blocking) — **only when some `geo.providers[]` entry is a
   `wifi_scan`** (`geo_chain_needs_scan`) — sort by RSSI, keep the `geo.max_bssids`
   strongest. An `ip`-only chain skips the scan entirely: scanning in station mode
   disturbs the link the request that follows needs, and the scan's 1.4 KiB of stack
   buys a BSSID list nothing in the chain can send.
3. Walk `geo.providers[]` in the order it was written, whichever `kind` each entry
   is. A `wifi_scan` entry is a `POST` of `[{bssid, rssi}]`; an `ip` entry is a `GET`
   with no key and no body, because such a provider reads the caller's address off
   the connection and refuses a `POST` (`geo_kind_posts`, `docs/tools/geo.md`
   §Provider chain). If a provider exceeds its attempt budget or fails to respond
   within `geo.timeout_ms`, it is considered failed and the chain continues to the
   next entry. Order is the user's privacy control, so the firmware does not reorder
   the chain by kind.
4. Cache the whole `geo_fix_t` and return it: the coordinates, `accuracy_m`,
   `provider`, `source_ts`, and every place and locale member the provider sent.
   `geo_add_place()` is the one emitter for those members (`04` §4.13 owns their
   names), because the command surface and the tool both report them and two copies
   of a seventeen-name list drift.

The place members are optional because no provider is obliged to send them: `ipwho.is`
answers a city, a region, a country, a postal code and a timezone but no currency and
no languages, while `ipapi.co` answers all of them under different key names. `geo_parse`
normalizes both dialects into one `geo_fix_t` — the country name and the code swap
places between them, `is_eu` is `ipwho.is`'s spelling of `in_eu`, and a calling code
arrives with or without its `+`. A member whose value cannot be normalized is left
empty rather than guessed at, and `geo_add_place` then omits it from the frame.

The cache is RAM-only and bounded by `geo.cache_ttl_s` alone: a change in RSSI
does not invalidate it. A reboot costs one scan. Geolocation reveals the access
point's position, not necessarily the device's, and `accuracy_m` is reported
as-is rather than implied precise (`06-security.md` §6).

### `sys` — Hardware abstraction
- `sys_board_init()`: Install the GPIO ISR service (`gpio_install_isr_service()`) and
  start the task watchdog. Does not install a brownout handler: the brownout threshold
  is a board constant (`01-hardware.md` §5), not a runtime decision. Named
  `sys_board_init` rather than `sys_init` because lwIP exports a global `sys_init` in
  `liblwip.a`, and the two collide at link time once anything pulls in the network
  stack — which M3's `net` does. The prefix also says what the function is: board
  bring-up, not system policy.
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
10. **Idle**: `app` enters main loop, waiting for `prompt`, `sched` or `mqtt.message` events. Reaching it clears `boot_failures` back to `0` (§3.2), because the boot that reached the idle point is the proof the previous one completed. A normal boot clears it; safe mode deliberately does not, so the operator exits that state with `reload` rather than with a reset that repaired nothing.

Steps 5-7 log and continue: a radio that will not associate or an NTP server that
does not answer must not cost the operator the link, because the link is how they
repair the config. In safe mode (§3.2) they are skipped entirely — the station stays
down and `time_synced` stays false, so nothing reaches the network before the config
has been repaired over USB.

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
| `link_task` | High | 12 KiB | Read/write USB CDC; fast framing to avoid buffer overflow, `hello`'s tool context, and every `command` frame — including one that blocks on a TLS handshake, which needs more headroom than the 8 KiB an earlier build carried |
| `agent_task` | Medium | 8 KiB | Serialized execution of LLM runs and tool calls |
| `sched_gpio` | Medium | 4 KiB | ISR-to-work handoff for an armed GPIO trigger |
| `wifi_connect` | Medium | 4 KiB | One association attempt, spawned per `wifi.connect` |
| `net_ping` | Medium | 3 KiB | One `net.ping`, so a blocking ping never holds `link_task` |

`mqtt` has no task of its own: esp-mqtt runs a per-client event loop task
internally (one per broker). Handlers must not block, so an inbound message is
matched against the armed tasks and the frame is emitted from the handler.

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

   The history that a round sends is the ordered message list, and the order is part
   of the contract rather than an implementation detail: the `assistant` message
   carrying `tool_calls` comes before the `role:"tool"` messages that answer it, and
   each tool message carries the `tool_call_id` of its call. A provider rejects a
   tool message whose id has no matching call, so the two travel together or not at
   all. The run's own prompt is the *last* message of the first request only; from
   the second round on it is already in the history, and `llm_request_t.user_prompt`
   is NULL. Sending it again would ask the question twice and place the user turn
   after the tool replies.

   A run that ends without an answer — cancelled, or failed before a final round —
   drops the turns it added and restores the depth it started from. The rule is the
   same one: an unanswered question, or a `tool` result whose call was never sent,
   must not reach the next run. A completed run keeps its question and its answer,
   which is what makes the buffer a conversation rather than a transcript of one
   request.

   **The run's question is pinned.** `agent_history_append` trims oldest-first at
   `llm.max_history_messages`, and one tool round can add five messages, so from the
   second round on the question would be evicted by the rounds it is paying for —
   leaving every later request a conversation whose premise it cannot see. While a
   run is active the append pins message 0, so the bound applies to the messages
   after it; the append is refused rather than allowed to drop the question.

   **A reply cut by the output cap is handled, not assumed complete.** The provider's
   `choices[0].finish_reason` is read on every reply. `length` means `content` is a
   prefix of what the model was writing, and it reaches `agent.final` as
   `truncated: true` with no `full_bytes` (`04-protocol.md` §3.14) — a sentence that
   stops mid-word is never presented as a finished answer. `length` also explains a
   tool call whose `arguments` is not valid JSON: the call is kept with
   `args_truncated` set instead of failing the parse, and step 4 answers it with an
   `E_TOOL` result naming the cause, so the run retries the call smaller rather than
   dying. A malformed call under any other finish reason is still a provider fault.

   **A thinking model's working is carried beside its answer, never instead of it.**
   The trace arrives in `message.thinking` (Ollama), `reasoning_content` or
   `reasoning`; none is in the OpenAI spec, and the providers in `03-config.md`
   §Provider compatibility add them. `agent_parse_reply` reads all three and keeps the
   trace on `agent_reply_t.thinking`, which reaches the bridge as its own
   `agent.thinking` frame (`04-protocol.md` §3.21). It never enters
   `agent.final.text`: presenting the private notes as the reply is a different
   answer, not a longer one, and `AGENT_TEXT_MAX` is already the whole frame budget.

   The one case where the trace does become the answer is a reply with **no** visible
   `content` and **no** tool call — some models reason and stop. Read as-is that
   reached `agent.final` as `text: ""`, which the bridge renders as a blank turn, an
   answer the reader cannot tell from a silent failure. The fallback is kept for that
   case alone. The tool-call condition is what stops it corrupting a normal tool
   round, where an empty `content` is expected and the reasoning would otherwise be
   replayed into history as the assistant turn. Visible `content` always wins.

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
| WiFi Lost | `net` attempts reconnect from the disconnect event; `agent` returns error | `E_WIFI` |
| LLM Down | Try the next fallback provider; a transient failure against the current one is retried once first (`llm_retry_after`). If all fail → error | `E_LLM_UNAVAILABLE` |
| LLM call cap reached | No provider is tried; the call is never sent, so the daily budget cannot be exceeded by a fallback chain | `E_LLM_RATE` |
| Provider 400 (bad model, wrong `token_param`, rejected `extra_body`) | Not retried in place: the next provider is tried, since a 400 is per-entry config, not a transient fault | `E_LLM_UNAVAILABLE` after the chain is exhausted |
| Provider 408/429/5xx | Retried once against the same provider after `Retry-After` (clamped to `LLM_RETRY_MAX_MS`), then the chain advances | `E_LLM_UNAVAILABLE` after the chain is exhausted |
| Run cancelled by `cancel` | `llm_abort()` cancels the in-flight request; the turns the run added are dropped and the run ends | `E_CANCELLED` |
| Tool call arguments truncated by the output cap | The call is answered with an `E_TOOL` result naming the cause and the run continues; the assistant turn never references an unanswered `tool_call_id` | `E_TOOL` result |
| Tool result too large to render into its 4096-byte slot | Answered with an `E_TOOL` result naming the cause; a `role:"tool"` message always carries `content` | `E_TOOL` result |
| MCP Timeout | Terminate specific tool call; return error to LLM | `E_MCP` / `E_TIMEOUT` |
| Tool Crash | Isolate tool; return `E_TOOL` to LLM | `E_TOOL` |
| Buffer Full | Truncate oldest history entry in PSRAM | — |
| Close-out failed at budget end (open tasks, or untracked multi-round work) | Finalize anyway; `agent.final` reports `todo_open: true` | — |

### 4.3 Concurrency, frames and reload

Three rules here are load-bearing and each had a defect found by running the loop
on a board rather than in the host suite.

**A frame with no `id` is not emitted.** `link_emit_body` rejects a NULL id, and the
rejection is silent, so a caller that forgets one produces a run that starts and
then says nothing — indistinguishable from a hang. Every emitter allocates through
`link_next_id`, which takes a caller-owned buffer: `04-protocol.md` §2 gives the
device one counter for the whole link, so a second one in the agent task would let
`d-1` and `a-1` coexist and make "never reused" false. The buffer is per call
because the next allocation would otherwise overwrite an id that has not yet been
copied into its frame.

**Frames are assembled and written under one lock.** `link_emit_body` is called from
the link task (`command.result`, `hello_ack`, `event`) and from the agent task
(`agent.start`, `agent.tool`, `agent.final`), and the log hook can fire from either.
`s_tx` and `s_emit` in `link.c` are shared, so assembling without the lock lets two
tasks interleave halves of two lines and desynchronise the stream — the one failure
the NDJSON reader cannot recover from (`04-protocol.md` §1.1). The lock is a
recursive mutex created by `link_set_writer`, taken by every path that writes either
buffer.

**The provider chain is walked once, and `-1` ends it.** `llm_next_provider` returns
`-1` when the chain is exhausted. A walk guarded by `index < count` is *true* for
`-1`, so it reads `providers[-1]` and then loops forever: the run never returns,
`agent.busy` stays true, every later prompt is queued and then refused, and the
budget counter climbs without bound (14,483 calls were charged this way in one
observed run). The guard is `llm_provider_loop_ok`, host-tested, and every `continue`
in the walk advances the index.

**`reload` restarts the services, and safe mode is named.** `reload` clears
`boot_failures` and then calls the same `start_services` the boot path calls, so
`net`, `clock`, `llm` and `agent_start` run again. Before that it cleared the flag
and nothing else, leaving `sys.safe_mode: false` while every prompt was still refused
for a reason no frame carried. A prompt that arrives while the device is in safe mode
now answers `E_SAFE_MODE` (`04-protocol.md` §4.11) instead of a generic error.

## 5. Scheduler & Triggers

The scheduler implements the frozen triggers from `docs/00-overview.md` §5.

### 5.1 Trigger Semantics
 - `boot`: Fires once during boot sequence, at the end of `sched_start()`.
 - `wifi_connected`: Fires when `net` confirms association.
 - `ntp_synced`: Fires when `clock` receives first valid SNTP update.
 - `serial_connected`: Fires when `link` detects the host has opened the port.
 - `gpio`: Fires on edge detection (rising/falling) of a configured pin.
   **Path:** `sched` installs the handler itself with `gpio_isr_handler_add()` and uses
   `sys` only to configure the pin (`sys_gpio_edge_config(pin, edge)`, an L0 call).
   `gpio_isr_handler_add()` registers a handler *function*; the handler it registers
   does one thing — `xQueueSendFromISR()` into a `sched`-owned queue — and returns. The
   `sched` task drains that queue, reads the level, emits `gpio.edge` and matches
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
overlapping. A payload is untrusted input, but it is never prompt text: it is
matched against `payload_contains` and reported as `mqtt.message`; the run's prompt
is the task's own `action.prompt` or the rendered template (`06-security.md` §6.4).

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

`mcp` is L1 (`08-code-architecture.md` §1): it depends on `cfg`, `net` and `store`,
calls no host, and is called by `tools` (L2) and by `agent` through `tools`.

### 6.1 Transport

MCP over Streamable HTTP: every request is one `POST` of a single JSON-RPC object to
`mcp.servers[].url`, and the reply is the response body. No SSE stream is opened, so a
server that answers only over `text/event-stream` is not supported. `net` performs the
request; `mcp` builds the body and parses the reply.

`auth_header_ref`, when set, resolves through `cfg_resolve_secret` into the whole
`Authorization` header value, which `mcp` zeroes after the request. The secret is never
logged and never reaches a frame.

### 6.2 Session and discovery

Discovery runs once at start, after the network is up and before the model is offered
its tool list, so `hello_ack.capabilities.tools`, `hello_ack.capabilities.mcp` and the
`tools` array sent to the model cannot disagree:

1. For each enabled server, in config order: `initialize`, then
   `notifications/initialized`, then `tools/list`.
2. Cache the returned `tools[].name`, `description` and `inputSchema`, namespaced as
   `mcp.<server_id>.<tool_name>`. A name over `MCP_TOOL_NAME_MAX` or a set over
   `MCP_MAX_TOOLS` is dropped rather than truncated.
3. A server that fails contributes no tools, raises `mcp.error`, and does not stop the
   others. A board where every server is down still boots with its built-in tools.

`mcp_discovered()` returns that flattened set; `tools_context_from_config` copies it
onto `tools_context_t.mcp_tools`, which is what `tools_json.c` offers and what
`tools_offered` recognises. Nothing re-derives it per model round.

`tools_offered` is a real gate: a name is offered because the registry defines it, the
loaded declarative set carries it, or an MCP server listed it this run. A name none of
the three supplies — the model invented it, or the file behind it was deleted — is not
offered, and `tools_call` answers `E_TOOL` rather than reaching a dispatch arm that has
nothing to dispatch to.

### 6.3 Call

A `mcp.` name is split at the first two dots into server id and tool name. A name whose
server is not configured, or whose tool was not discovered, is refused with `E_TOOL`
before any request is made. Otherwise `tools/call` is posted and the reply's
`result.content[].text` items are joined with a space into the tool result string; a
non-text item contributes `[<type>]` so a refusal is visible rather than empty. The join
is cut at the caller's cap with a trailing `…`, the same rule `tools` applies to a long
built-in result. `result.structuredContent` is not read.

A remote tool is **untrusted**: it is gated on `tools.allow_dangerous`, because the
server chooses what it does. A JSON-RPC `error` object, a non-2xx status, a timeout or
an unparseable body is `E_MCP` with the server's own message when it gave one. A reply
with `result.isError` true is also `E_MCP`, with the joined content as the message.

### 6.4 Memory

A discovered set is `MCP_MAX_SERVERS × MCP_MAX_TOOLS` entries of fixed-size strings,
about 88 KiB. Both the per-server state and the flattened set are heap allocations,
PSRAM first and internal as fallback, for the same reason `sched` and `agent` do it:
the model's task stack comes from the same DIRAM pool.

## 7. Tools

The tool surface is documented per domain in `docs/tools/`. This section keeps only
what belongs to the firmware contract: how definitions are assembled and sent, and
the one write carve-out that touches storage internals.

### 7.1 Tool definitions sent to the model

`tools_json` is built at run start from three sources, in this order: built-ins,
declarative `/tools/*.json`, and MCP tools discovered at `tools/list`. All three are
live: `tools_declared_loaded()` lists the image's `/tools/` once, compiles every
`<stem>.json` through `tools_declared_compile` and keeps the rest as
`tools_context_t.declared`, and `mcp_discovered()` supplies the third. A file the
loader drops is absent from `tools` and from `capabilities.tools` rather than offered
and then failing per call; a file costs one tool, never boot. The load is lazy and
whoever asks first pays for it, which makes the compile path stack-frugal by
requirement: `agent_start` primes the prompt and the tool array at boot (§3.1), so
the first caller is the main task's 3584-byte stack, not `link_task`'s 12 KiB — a
2048-byte local buffer in the compile frame overflowed exactly that stack and
panic-looped the device at boot. The emitted schema is therefore built straight into
`tools_declared_t.schema`, never into a local of the same size. Each entry is
one `{"type":"function","function":{...}}`. The value `llm` receives is the bare
array — `[{"type":"function",...}]` — with no enclosing object, because `llm` splices
it into a member of its own request. A slice that carries the wrapper's closing brace
closes the request early and every provider refuses the body. Every built-in carries a
description and a parameter schema of the same shape declarative tools require
(`03-config.md`
§Tool schema): `type: "object"`, `properties`, `required`, plus `enum` for a closed
value set; nothing else.

The stack rule binds everything `agent_start` reaches, not only the compile path,
because it runs last in `start_services` with the least headroom: a probe on the
target measured 1080 bytes free on the main task at `before agent_start`. The
1312-byte `skills_scan` frame — its `store_entry_t entries[SKILLS_MAX]` local,
reached from `agent_start` through `build_system_prompt` → `skills_current` —
overflowed that stack on every normal boot and drove the device into the guard's
safe mode, which in turn booted cleanly because it skips `start_services`; the
crash therefore looked like a safe-mode latch rather than an overflow. The listing
now lives in a file-scope buffer beside `s_meta`, for the same reason `s_file` is
static. A fixed-size aggregate in any function `agent_start` can reach is a boot
crash, not a saving.

Two built-in schemas nest: `task.set` and `todo`. The `task.set` `task` object
mirrors `/tasks.json` verbatim (`03-config.md` §Task schema) so the model authors a
task in the shape the file stores, and `todo`'s `list` is an array of `{phase, items}`
objects next to an `op` enum (`tools/todo.md`). `sys.diagnostics` takes one optional
`scope` enum (`tools/sys.md`); the other twenty-three built-ins take a flat object of
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
| Reachability | `tools/net.md` | `net.ping` |

A tool call whose arguments fail schema validation is rejected as `E_TOOL` without
executing, and no partial effect is applied.

### 7.2 `task.set` and the `tasks.json` write

This is the only **tool** allowed to write to `userfs`. It validates the input
against the task schema in `docs/03-config.md` §tasks and refuses if the resulting
count would exceed `tools.task_max`. The cap is checked against a *new* id only: an
update replaces in place and consumes no slot, so a schedule already at the cap can
still be edited. Result returns the task id. This is a deliberate carve-out to
`06-security.md` §6.1 barrier 3.

The tool is **add/update only**. There is no delete op and no field that reads as
one: an unrecognised field fails schema validation, and `enabled: false` retains the
entry. Deletion is host-only through the `tasks.delete` command (§4.7 of
`04-protocol.md`), so a compromised model cannot remove a user's scheduled task.
Authority split and argument semantics: `tools/tasks.md`.

`store_write_image_chunk("/tasks.json", ...)` writes the whole `/fs/tasks.json`
atomically: the same staged-write protocol `fs.put` uses, which renames over the
target only on the `final` chunk, so a refused or abandoned transfer leaves the
previous file intact. `sched` is its only caller. There is no separate
`store_write_image_chunk()` — the generic image writer already has the semantics the task
file needs, and a second entry point would be a second place to keep the atomicity
right.
 A write cannot be refused by a concurrent `reload`: both take the same mutex, so the
 write waits for the reload to finish and then lands. There is therefore no `E_BUSY`
 for this path, and the only failures are the ones named in `04-protocol.md` §4.6 and
 `docs/tools/tasks.md`. Its callers are exactly three: the `task.set` tool, and the
 `tasks.set` / `tasks.delete` commands.
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

The seeded `userfs` QEMU runs on is a throwaway: three directories and a
placeholder `config.json`. The real image is built by
`bridge/tools/build_userfs.py` from `firmware/userfs/` (`../05-bridge.md` §11),
which is flashed at the partition offset rather than formatted by the firmware; a
freshly flashed board halts on a `userfs` that is blank and cannot be formatted
(§3.1), and the two declarative tools `00-overview.md` §7 ships — `weather_now`
and `web_search` — are absent unless that image was flashed.
