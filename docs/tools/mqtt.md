# MQTT tools

Read the configured brokers and publish to one of them. `mqtt.list` is ungated;
`mqtt.publish` requires both its `server_id` and its `topic` to pre-exist in
`mqtt.servers[]`. Gate matrix: `../06-security.md` §6.

| Tool | Gate | Errors |
|---|---|---|
| `mqtt.list` | none | `E_MQTT` `[ASSUMPTION]` |
| `mqtt.publish` | `server_id` exists in `mqtt.servers[]` AND `topic` is covered by that server's `topics[]`; client connected | `E_MQTT` |

Each `mqtt.servers[]` entry is one `esp_mqtt_client_handle_t`, created up to
`mqtt.max_servers` (4) at init, with its own TLS, credentials and `topics[]`
(`../02-firmware.md` §2, `mqtt`; keys in `../03-config.md` §mqtt). Servers are
isolated from each other: a disconnected broker loses its tools and the agent run
continues (`../03-config.md` §mqtt).

## `mqtt.list`

Reports every configured broker and its live state. `id` is the
`mqtt.servers[].id` string, `connected` is the client state at call time, and
`subscriptions` is the list of topic filters the client subscribed to, taken from
that server's `topics[]`. A disconnected broker is still listed, with
`connected: false`; it is not an error.

`mqtt.list` is the tool-side view. The host command `mqtt.status` reports the same
entries plus `last_error` (`../04-protocol.md` §4.15); `mqtt.list` does not carry
that field. No password material is emitted: `[ASSUMPTION]` ids and topic strings
are the only server-side values in the result (`../06-security.md` §11, S11).

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| - | - | - | no arguments; the object is empty |

**Result**

```json
{"servers":[{"id":"home","connected":true,"subscriptions":["sensors/temp","sensors/rh"]},{"id":"lab","connected":false,"subscriptions":["lab/telemetry"]}]}
```

**Schema sent to the model**

```json
{"type":"object","properties":{}}
```

## `mqtt.publish`

Publishes one message on one configured broker. Two gates apply, and both are on
configured state, not on argument syntax: `server_id` must name an entry in
`mqtt.servers[]`, and `topic` must be covered by that entry's `topics[]`. A miss on
either is refused with `E_MQTT` before any socket write. The call is refused with
`E_MQTT` again if the client exists but is not connected, and a PUBACK failure on
QoS 1 is the same code (`../04-protocol.md` §4.14).

`E_MQTT` is the entire failure domain of this tool. A malformed call — a missing
 `E_MQTT` is the entire failure domain of this tool. A malformed call — a missing
 required field, a `qos` other than `0` or `1`, an unknown property — is reported as
 `E_MQTT` as well, where every other built-in reports `E_TOOL`.
`E_MQTT` as well, where every other built-in reports `E_TOOL`. The reason is that
this tool's authorisation input is the configuration, and the configuration is
what the refusal describes; the wrapper does not split syntax errors from state
errors here (`../02-firmware.md` §2, `mqtt`). Refusals arrive in `agent.tool_result`
with `ok: false` and never abort the run (`../04-protocol.md` §3.13).

`payload` is sent as the message body exactly as given; it is not encoded, escaped
or wrapped. The docs state no size bound for it `[ASSUMPTION]`. `qos` omitted means
 `payload` is sent as the message body exactly as given; it is not encoded, escaped
 or wrapped. The docs state no size bound for it `[ASSUMPTION]`. `qos` omitted means
 the server's `mqtt.servers[].qos` default. `0` and `1` are the only accepted
 values; QoS 2 is permanently unsupported (`../03-config.md` §mqtt).

The host-side command of the same name additionally carries `retain?`
(`../04-protocol.md` §4.14); the model-facing tool does not expose it.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `server_id` | string | yes | must equal an `mqtt.servers[].id` |
| `topic` | string | yes | must be covered by that server's `topics[]`; see below |
| `payload` | string | yes | sent verbatim; no size bound stated `[ASSUMPTION]` |
| `qos` | integer | no | `0` or `1`; defaults to `mqtt.servers[].qos` |

**Result**

```json
{"published":true}
```

A successful publish reports `published: true` only. The broker's PUBACK is
consumed by the client; no packet id or timing is surfaced to the model.

**Schema sent to the model**

```json
{"type":"object","properties":{"server_id":{"type":"string"},"topic":{"type":"string"},"payload":{"type":"string"},"qos":{"type":"integer","enum":[0,1]}},"required":["server_id","topic","payload"]}
```

## Topic allowlist

One list per server serves both directions: `topics[]` is the complete
publish/subscribe allowlist. An outbound topic that no entry covers is refused with
`E_MQTT`, and an inbound message on a topic that no entry covers is never delivered
and never fires a trigger (`../03-config.md` §mqtt).

The filter grammar is deliberately smaller than MQTT's: plain topic names, optionally
with a trailing `*` on the final level. That `*` matches exactly one level, so it is
not `#`: `sensors/*` matches `sensors/temp` and not `sensors/temp/raw`. MQTT `+` and
`#` are refused at validation, so a config entry can never widen the allowlist past
what the user wrote.

| Pattern | Matches | Does not match |
|---|---|---|
| `sensors/temp` | `sensors/temp` | `sensors/temp/raw`, `sensors/temperature` |
| `sensors/*` | `sensors/temp`, `sensors/rh` | `sensors`, `sensors/temp/raw` |
| `home/office/*` | `home/office/temp` | `home/office`, `home/office/a/b` |
| `home/office/temp` | `home/office/temp` | `home/office/*`, `home/*/temp` |

A `*` anywhere other than the final level, a bare `*`, and `+`/`#` are rejected when
the config loads, not at call time.

## Errors

- `E_MQTT`: `server_id` is not an entry in `mqtt.servers[]`.
- `E_MQTT`: `topic` is not covered by that server's `topics[]`.
- `E_MQTT`: the server's client is not connected, or a QoS 1 PUBACK fails
  (`../04-protocol.md` §4.14).
- `E_MQTT`: any other rejected `mqtt.publish` call, including a bad argument shape.
  This tool never reports `E_TOOL`; the gate it applies is configuration state, so
  configuration state is what its refusals describe.

Failures never abort the run: a refusal is an `agent.tool_result` with `ok: false`
(`../04-protocol.md` §3.13), and the call itself appears as an `agent.tool` frame
- [MQTT server config keys](../03-config.md#mqtt)
- [Frozen MQTT decisions](../00-overview.md#42-mqtt)
- [`mqtt` module API](../02-firmware.md#mqtt--brokers)
- [`mqtt.message` frame](../04-protocol.md#318-mqttmessage-device--bridge)
- [`mqtt.status` command](../04-protocol.md#415-mqttstatus)
- [MQTT message trigger matching](../02-firmware.md#54-mqtt-message-trigger-matching)
- [Broker payloads as untrusted input](../06-security.md#64-mqtt-and-geolocation-inputs)
- [Authorization matrix](../06-security.md#6-authorization-model)
- [Tool surface index](README.md)
