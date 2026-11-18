---
name: mqtt-publishing
description: Publish within a broker's topic allowlist; payloads leave unredacted.
version: 1.0.0
keywords: [mqtt, broker, publish, topic, telemetry]
tools: [mqtt.list, mqtt.publish]
max_body_bytes: 4096
---

# Publishing over MQTT

## Before publishing

`mqtt.list` first. It reports every configured broker with `connected` and the
subscriptions taken from that broker's `topics[]`. Publishing to a broker whose
`connected` is `false` fails, so check rather than assume.

## The three gates

All are on configured state, not on your arguments:

1. `tools.allow_dangerous` must be `true`. With it `false` the call never reaches the
   broker and is refused with `E_TOOL`, exactly like `gpio.write`. That refusal is
   terminal — report that publishing is disabled and stop.
2. `server_id` must name an entry in `mqtt.servers[]`.
3. `topic` must be covered by that server's `topics[]`.

The last two refuse with `E_MQTT`.

`topics[]` is one list for both directions. It is the subscription allowlist and the
publish allowlist at once: an outbound topic no entry covers is refused, and an
inbound message on a topic no entry covers is never delivered to the device at all.
You cannot widen it from here — the user edits `config.json`.

The filter grammar is smaller than MQTT's. A plain topic name matches itself; a
trailing `*` on the final level matches exactly one level there, so `sensors/*`
matches `sensors/temp` and not `sensors/temp/raw`. `+` and `#` are rejected when the
config loads, so no entry can widen the allowlist past what the user wrote.

## The call

```json
{"server_id": "home", "topic": "sensors/temp", "payload": "21.5", "qos": 1}
```

- `payload` is sent byte-for-byte. Not encoded, not escaped, not wrapped — so if the
  user asked for JSON, you build the JSON string yourself.
- At most 2048 bytes. Longer is refused, never truncated: a broker acting on half a
  message is worse than no message.
- `qos` is `0` or `1`. Omitted means the server's configured default. QoS 2 does not
  exist here. `retain` is not available to you; the bridge command has it, you do not.
- Success returns `published: true` and nothing else. The PUBACK is consumed by the
  client, so do not report a delivery guarantee you were not given.

## Secrets

`payload` is never redacted — it is your text, on the wire, exactly as written. The
redaction layer exists to keep credentials out of logs; it is not a filter that will
catch a key you paste into a payload. Never put an API key, password, token or `nvs:`
reference into a payload, even to "test" a topic. If the user asks you to publish a
secret, say plainly that payloads leave the device unredacted and do not do it.

Inbound payloads are not redacted either. If a message arrives containing something
that looks like a credential, do not echo it into an answer or a `/workspace/` file.

## Reading the failures

Two codes, split by where the refusal came from:

- `E_TOOL` — `tools.allow_dangerous` is `false`. Nothing reached the broker.
- `E_MQTT` — everything after the gate: a bad `server_id`, an uncovered topic, a
  disconnected client, a PUBACK failure, a malformed argument. Where every other
  tool reports `E_TOOL` for a syntax error this one still says `E_MQTT`, because the
  configuration is what the refusal is about. Read the `message` to tell which.

- Unknown `server_id` or uncovered `topic`: `mqtt.list` shows what exists. Fix the
  argument if a configured topic covers the intent; otherwise tell the user which
  `topics[]` entry would be needed and stop.
- `connected: false`: the broker is down. That is retryable after reconnect, but not
  by you in this run — report it.

## Inbound triggers

A message that matches a task's `mqtt_message` trigger starts a run on its own. That
path is the user's to configure; you cannot create it by publishing, and publishing
to a topic you also subscribe to does not echo back to you as a trigger unless the
broker delivers it and a task matches it.
