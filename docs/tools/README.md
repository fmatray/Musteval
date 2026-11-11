# Tool Documentation

This directory documents the tool surface available to the Musteval LLM.
Tools are called via the `agent.tool` frame and return values via the `agent.tool_result`
frame (`../04-protocol.md` §3.12 and §3.13).

Declarative HTTP tool results are capped at 4 KiB before returning to the model; the
rest is dropped and a `truncated: true` note is appended (`../03-config.md` §tools,
step 4, where the cap carries an `[ASSUMPTION]` marker). Other tools are bounded per
domain, documented in each file; no single cap applies to all of them.

The authoritative capability matrix for tool gating is found in `../06-security.md` §6,
with workspace confinement detailed in §6.1.

| File | Tools | Domain |
|---|---|---|
| `README.md` | — | Tool index |
| `gpio.md` | `gpio.read`, `gpio.write` | Digital I/O |
| `adc.md` | `adc.read` | Analog input |
| `i2c.md` | `i2c.scan`, `i2c.read`, `i2c.write` | I2C bus |
| `time.md` | `time.now` | System time |
| `filesystem.md` | `fs.list`, `fs.read`, `fs.write`, `fs.delete` | LittleFS storage |
| `memory.md` | `memory.get`, `memory.set`, `memory.list`, `memory.delete` | NVS key/value store |
| `web.md` | `http.get`, `weather_now`, `web_search` | HTTP and declarative tools |
| `skills.md` | `skill.read` | Skill definition access |
| `tasks.md` | `task.list`, `task.set` | Task management |
| `todo.md` | `todo` | RAM-only checklist |
| `events.md` | `event.emit` | System events |
| `geo.md` | `geo.locate` | Geolocation |
| `mqtt.md` | `mqtt.list`, `mqtt.publish` | MQTT broker communication |
| `sys.md` | `sys.diagnostics` | Device health snapshot |
| `net.md` | `net.ping` | Reachability probing |

## Governing documents

Tool behavior is defined by several non-tool documents:

- **Physical Pins:** `../01-hardware.md` §6 (GPIO maps and reserves).
- **Config Keys:** `../03-config.md` §tools (bus pins, allow-hosts, dangerous flag).
- **Gates:** `../06-security.md` §6 (authorization and confinement).
- **Frames:** `../04-protocol.md` §3.12 / §3.13 (wire protocol and framing).
