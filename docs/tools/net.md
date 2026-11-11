# Network tools

One tool, `net.ping`, answers "can this device reach the host that matters right
now?" — the router it is attached to, the LLM provider it is configured to call, or
the NTP server that disciplines its clock. It is a different question from two
neighbouring ones: `ping`/`pong` (`../04-protocol.md` §3.7) measures the bridge↔device
link, and `sys.diagnostics`' `wifi.rssi` (`sys.md`) measures radio strength. Neither
shows whether the path beyond the radio actually carries packets, which is what a
model needs before it decides a request is worth attempting.

The tool is ungated. It is read-only: it writes nothing, opens no listening socket,
makes no LLM call, reads no file and reads no key. Its target set is closed below, so
it cannot be aimed at an arbitrary host and no allowlist applies to it. It does send
ICMP, which is why `06` §6 names it in the always-available row explicitly rather
than leaving it to be inferred from "read-only".

| Tool | Gate | Errors |
|---|---|---|
| `net.ping` | none | `E_TOOL` on an unknown target or a missing reference; `E_WIFI` when the link is down; `E_INTERNAL` |

Gate matrix: `../06-security.md` §6. The matrix is the sole authority; this tool falls
into the always-available class (`time.now`, `fs.list`), because it is read-only, has
no actuation path, and its target is an enum rather than a caller-supplied address.

It never returns secret material. The tool reads no API key, no PSK and no `nvs:`
value: `llm` resolves to a host name, never to the key that authenticates against it.

## `net.ping`

Resolves the selected reference to one host and probes it with four ICMP echoes. The
resolution is the point of the tool: `router` becomes whatever gateway the DHCP lease
actually carried, so a model sees what was measured instead of assuming its own idea
of "the router" was the one on the wire.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `target` | string | yes | `router`, `llm`, `ntp` |

`router` is the default gateway the station received over DHCP. `llm` is the host of
the active provider, resolved from the fallback chain without reading its key. `ntp`
is the first entry of `ntp.servers[]`. A value outside the three is refused as
`E_TOOL` by schema validation, before anything is resolved or sent
(`../02-firmware.md` §7.1).

**Result**

| Member | Type | Meaning |
|---|---|---|
| `target` | string | Echo of the argument, so the result names the question it answers |
| `host` | string | The name or address actually probed; what `router` resolved to |
| `sent` | integer | Echoes sent; always 4 on a successful result (`NET_PING_COUNT`) |
| `received` | integer | Echo replies that came back; `0` is a valid answer |
| `loss_pct` | integer | `100 * (sent - received) / sent` |
| `rtt_min_ms` | integer | Shortest round trip of the replies, milliseconds; `0` when none came back |
| `rtt_avg_ms` | integer | Mean round trip of the replies; `0` when none came back |
| `rtt_max_ms` | integer | Longest round trip of the replies; `0` when none came back |

A reachable host:

```json
{"target":"router","host":"192.168.1.1","sent":4,"received":4,"loss_pct":0,"rtt_min_ms":1,"rtt_avg_ms":2,"rtt_max_ms":3}
```

**A 100 % loss is a result, not an error.** `received: 0` and `loss_pct: 100` says the
host did not answer, which is exactly what the model asked, and the rtt fields are `0`
because nothing came back. The tool fails only when the probe could not be attempted
at all: an unknown target or a reference that does not exist (`E_TOOL`), a link that
is down (`E_WIFI`), or a probe session that could not start.

In place, as the model's tool result:

```json
{"v":1,"type":"agent.tool_result","id":"d-31","ts":1730572531,"run_id":"r-7","round":1,"call_id":"call_b4","tool":"net.ping","ok":true,"result":{"target":"router","host":"192.168.1.1","sent":4,"received":4,"loss_pct":0,"rtt_min_ms":1,"rtt_avg_ms":2,"rtt_max_ms":3},"duration_ms":812}
```

The same shape when nothing answers, still `ok: true`:

```json
{"v":1,"type":"agent.tool_result","id":"d-32","ts":1730572540,"run_id":"r-7","round":1,"call_id":"call_b5","tool":"net.ping","ok":true,"result":{"target":"llm","host":"openrouter.ai","sent":4,"received":0,"loss_pct":100,"rtt_min_ms":0,"rtt_avg_ms":0,"rtt_max_ms":0},"duration_ms":2810}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"target":{"type":"string","enum":["router","llm","ntp"]}},"required":["target"]}
```

The schema uses only `type`, `properties`, `required` and `enum`, the subset every
provider in the compatibility table accepts; there is no `oneOf`, `anyOf` or `$ref`,
and the result shape is not declared to the model at all (`../02-firmware.md` §7.1).

**Bounds.** Four echoes are sent `NET_PING_INTERVAL_MS` (200 ms) apart with a
`NET_PING_TIMEOUT_MS` (500 ms) wait each, so the call returns inside about 3 s even
when every echo is lost. It blocks the run for that window, which the agent loop
already serialises: tool calls are dispatched one at a time. The host command
`net.ping` (`../04-protocol.md` §4.25) requires an `E_BUSY` gate because it runs on its
own task and owns a session flag; the tool does not, because the loop never has
two calls in flight.

## Errors

- `E_TOOL` — `target` is absent or is not one of `router`, `llm`, `ntp`; schema
  validation rejects the call and nothing is probed. Also the answer when the selected
  reference does not exist: no gateway, no provider configured, or an empty
  `ntp.servers[]`. The model fixes this by choosing another target, not by retrying.
- `E_WIFI` — the station is not connected, so no echo can leave the device. This is
  the network's failure, not the call's.
- `E_INTERNAL` — the probe task or session could not start; the echo never left, so
  this is not a statement about reachability. Retrying is reasonable once; a second
  `E_INTERNAL` means the device is short of memory or the ping stack is wedged.
- A reachable device whose host does not answer is not an error; see the loss
  paragraph above.

## See also

- `../04-protocol.md` §4.25 — the host command that runs the same probe, and the
  canonical definition of the result members.
- `../04-protocol.md` §3.13 — `agent.tool_result`, the frame that carries the result.
- `../02-firmware.md` §7.1 — built-in tool table and the schema subset.
- `../06-security.md` §6 — the gate matrix; sole authority on which tools are
  always available.
- `sys.md` — `sys.diagnostics`, whose `wifi.rssi` is radio strength rather than
  reachability.
