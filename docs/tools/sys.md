# System tools

One tool, `sys.diagnostics`, returns device health to the model. The same numbers
already leave the device in the `status` object (`../04-protocol.md` §3.9), but that
object is bridge-only: it is emitted on the link and never enters the model's context.
A model that cannot see heap free tries a large HTTP fetch while the device is low on
internal SRAM and loses the round instead of choosing a smaller request. This tool
closes that gap with a read-only projection of the status object, not a second source
of truth.

The tool is ungated. It reads counters and returns them; it writes nothing, opens no
socket, makes no LLM call and reads no file. Its result is bounded by construction
(below), so there is no reason to put it behind `tools.allow_dangerous`.

| Tool | Gate | Errors |
|---|---|---|
| `sys.diagnostics` | none | `E_TOOL` on an unknown `scope` or failed validation |

Gate matrix: `../06-security.md` §6. The matrix is the sole authority; this tool falls
into the always-available class (`time.now`, `fs.list`), as it is read-only and
bounded.

It never returns secret material. No API key, PSK, MCP auth header, `secret.set`
value, resolved `nvs:` value, `nvs:` key name or path into the `secrets` namespace
can appear in a result, because no member of the projection is sourced from NVS
secrets at all. This is the same rule the redaction table applies to
`agent.tool_result` (`../06-security.md` §7, T18): a resolved secret value is never
emitted anywhere, and the only thing that may cross the link is the reference string.

## `sys.diagnostics`

Reads the requested health subset and returns it as one flat object. Every member is
named with its `status` path verbatim — `mem.heap_free`, `sys.boot_failures`,
`uptime_s` — so a bridge can plot a tool result and a `status` frame under the same
key. The single derived member is `mqtt.connected`, a count over the `mqtt[]` array;
it is marked as such below.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `scope` | string | no | `quick`, `runtime`, `memory`, `net`, `all`; default `quick` |

An omitted `scope` is `quick`, which is the cheapest useful answer. A value outside
the five is refused as `E_TOOL` by schema validation, before anything is read
(`../02-firmware.md` §7.1).

**Result**

The members depend on `scope`:

| Scope | Members |
|---|---|
| `quick` (default) | `uptime_s`, `mem.heap_free`, `sys.boot_failures` |
| `memory` | `mem.heap_free`, `mem.psram_free`, `storage.wsfs.used_bytes`, `storage.wsfs.total_bytes`, `storage.workspace_quota_bytes` |
| `net` | `wifi.state`, `wifi.rssi`, `wifi.ip`, `llm.provider`, `llm.available`, `mqtt.connected` |
| `runtime` | `sys.tool_calls`, `sys.tool_failures`, `llm.calls_hour`, `llm.calls_day`, `llm.limit_hour`, `llm.limit_day` |
| `all` | union of the four rows above, 19 members |

`all` is the union, not a superset with extra detail: the whole result is under 512
bytes serialized with every integer at its maximum, so it stays three orders of
magnitude below the 4 KiB declarative-tool result cap (`../03-config.md` §tools,
step 4) and cannot itself trigger truncation. A result is produced by reading
counters and cached state; no socket is opened, no file is opened or enumerated, and
no MCP or LLM call is issued.

```json
{"uptime_s":812340,"mem.heap_free":182344,"sys.boot_failures":0}
```

`quick` above. The `net` scope while WiFi is still associating shows the null
members the status object defines for that state:

```json
{"wifi.state":"off","wifi.rssi":null,"wifi.ip":null,"llm.provider":"openrouter","llm.available":false,"mqtt.connected":0}
```

In place, as the model's tool result:

```json
{"v":1,"type":"agent.tool_result","id":"d-31","ts":1730570002,"run_id":"r-7","round":2,"call_id":"call_c3","tool":"sys.diagnostics","ok":true,"result":{"uptime_s":812340,"mem.heap_free":182344,"sys.boot_failures":0},"duration_ms":1}
```

Member semantics, all inherited from the status object:

| Member | Type | Meaning |
|---|---|---|
| `uptime_s` | integer | Seconds since boot |
| `mem.heap_free` | integer | Internal heap bytes; the figure a large TLS handshake competes for (`../01-hardware.md` §2) |
| `mem.psram_free` | integer | PSRAM bytes; separate from `mem.heap_free` and not a substitute for it |
| `sys.boot_failures` | integer | Consecutive failed boots recorded by the boot guard |
| `sys.tool_calls` | integer | Built-in and declarative tool calls since boot |
| `sys.tool_failures` | integer | Of those, the ones returning `ok:false` |
| `wifi.state` | string | `connected`, `connecting`, `failed`, `off` |
| `wifi.rssi` | integer or null | dBm; `null` when there is no association |
| `wifi.ip` | string or null | IPv4; `null` when there is no lease |
| `llm.provider` | string or null | Active provider id — the config identifier, never the key behind it |
| `llm.available` | boolean | Last provider call succeeded |
| `mqtt.connected` | integer | Count of configured brokers whose `mqtt[].connected` is `true`; the one derived member |
| `storage.wsfs.used_bytes` | integer | Bytes used in `wsfs` |
| `storage.wsfs.total_bytes` | integer | `wsfs` partition size |
| `storage.workspace_quota_bytes` | integer | Constant quota enforced on `/workspace/` |
| `llm.calls_hour` | integer | LLM calls charged in the current UTC hour |
| `llm.calls_day` | integer | LLM calls charged in the current day |
| `llm.limit_hour` | integer or null | `llm.max_calls_per_hour`; `null` when the limit is disabled |
| `llm.limit_day` | integer or null | `llm.max_calls_per_day`; `null` when the limit is disabled |

Two readings that are easy to get wrong:

- **`storage.wsfs.used_bytes` is not quota usage.** The `wsfs` partition is 1 MiB
  (`../01-hardware.md` §4); the 64 KiB limit in `../06-security.md` §6.2 applies to
  the `/workspace/` tree inside it. `used_bytes` above the quota is therefore normal
  and does not mean the quota is broken; the quota figure itself is the constant
  `storage.workspace_quota_bytes`.
- **`sys.tool_calls` and `sys.tool_failures` are since boot; the LLM counters are
  per UTC bucket.** The first two reset on reboot and count this device's calls
  only; `llm.calls_hour` and `llm.calls_day` are charged per request attempt across
  the whole fallback chain and reset at the hour and day boundary respectively, not
  on a sliding window, which is what `E_LLM_RATE` reports once a limit is reached
  (`../03-config.md` §llm).

Nothing else from the status object is returned: no SSID, no `boot_id`, no `geo`
fix, no per-broker array, no config. A model that needs those asks the bridge, which
has the full `status` frame.

**Schema sent to the model**

```json
{"type":"object","properties":{"scope":{"type":"string","enum":["quick","runtime","memory","net","all"]}}}
```

`required` is absent because `scope` is optional. The schema uses only `type`,
`properties`, `required` and `enum`, the subset every provider in the compatibility
table accepts; there is no `oneOf`, `anyOf` or `$ref`, and the result shape is not
declared to the model at all (`../02-firmware.md` §7.1).

## Errors

- `E_TOOL` — `scope` is present but not one of `quick`, `runtime`, `memory`, `net`,
  `all`. Schema validation rejects the call and nothing is read.
- No other code is reachable from this tool. It needs no network, no scheduler and
  no agent loop, so it never itself returns `E_SAFE_MODE`. It cannot be *called*
  while safe mode is active, because every entry point that starts a run refuses
  with that code (`../02-firmware.md` §3.2); the bridge reads the same numbers from
  `status` instead. It has no `E_STORAGE` path, because it reads the
  already-computed `wsfs` usage figure instead of opening files, and no
  `E_LLM_UNAVAILABLE` path, because it reports `llm.available` rather than calling a
  provider.

## See also

- `../04-protocol.md` §3.9 — the canonical status object this tool projects; every
  field above is defined there and only there.
- `../04-protocol.md` §3.13 — `agent.tool_result`, the frame that carries the result.
- `../02-firmware.md` §3.2 — boot guard, `boot_failures` and safe mode.
- `../02-firmware.md` §7.1 — built-in tool table and the schema subset.
- `../06-security.md` §6 — the gate matrix; §6.2 — workspace quota; §7 — redaction
  rules the tool result obeys.
- `../03-config.md` §llm — `max_calls_per_hour`, `max_calls_per_day`.
- `../01-hardware.md` §2 — internal SRAM and PSRAM budgets; §4 — partition sizes.
