# Musteval

AI Agent running on an ESP32-S3.

## Overview

Musteval is a lightweight AI agent implemented on the Seeed Studio XIAO ESP32-S3. It provides a bridge between a local terminal and a secured AI provider via WiFi, enabling the agent to execute scheduled tasks, respond to hardware triggers, and call tools on the device or remote MCP servers.

### Architecture

- **Hardware**: Seeed Studio XIAO ESP32-S3 (8MB Flash, 8MB PSRAM).
- **Connectivity**: WiFi (best-hotspot selection), NTP (time sync), USB (native CDC).
- **Intelligence**: any OpenAI-compatible LLM endpoint (OpenAI, OpenRouter, Groq,
  Ollama Cloud or local Ollama, vLLM, Anthropic and Gemini compatibility endpoints),
  chained into a fallback list.
- **Interface**: Python-based Terminal UI (TUI) on a computer.
- **Storage**: two LittleFS partitions — `userfs` (config, prompts, skills, tools; read-only to the agent) and `wsfs` (the agent's `/workspace`, a separate filesystem); secrets live in NVS.

### Key Features

- **Self-Sustained**: Runs tasks via timers, GPIO edges or MQTT messages without requiring a computer connection.
- **Extensible**: Add skills and tools as Markdown and JSON files — imported over
  the link with `fs.put`, or baked into the `userfs` image. A built-in file cannot be
  overwritten by an import.
- **Peripherals**: Built-in tools for GPIO, ADC, I2C and outbound HTTP — no extra libraries, no extra firmware per sensor.
- **MQTT**: Multiple brokers at once, each with its own TLS settings, credentials and topic allowlist.
- **Location without GPS**: Position from a WiFi BSSID scan, with an IP-based fallback and a provider chain you control.
- **MCP Integration**: Directly connects to remote Model Context Protocol (MCP) servers over HTTPS.
- **Secured**: API keys, MQTT passwords and WiFi passwords are stored in NVS (not in plaintext config files), may be referenced from HTTP headers without ever reaching the model, and are redacted from all output; flash is unencrypted by default (see docs/06-security.md).

## Documentation

The full specification is located in the `docs/` directory:

- [00 Overview](docs/00-overview.md) - Authoritative decisions, architecture, and contracts.
- [01 Hardware](docs/01-hardware.md) - Pinout, safe GPIOs, and power.
- [02 Firmware](docs/02-firmware.md) - Module APIs, task model, and boot sequence.
- [03 Config](docs/03-config.md) - JSON schemas for settings and tasks.
- [04 Protocol](docs/04-protocol.md) - USB serial NDJSON message catalog.
- [05 Bridge](docs/05-bridge.md) - TUI screens, commands, and stack.
- [06 Security](docs/06-security.md) - Threat model and secret handling.
- [07 Roadmap](docs/07-roadmap.md) - Milestones and acceptance criteria.
- [08 Code Architecture](docs/08-code-architecture.md) - Firmware layering law, ports, extension points, and energy model.
- [09 Bridge Architecture](docs/09-bridge-architecture.md) - Bridge layering, the protocol module, ports, and reuse boundaries.
- [10 Test Agent](docs/10-test-agent.md) - Fake device over a pty: validating peer for hardware-free bridge testing.
- [11 Acceptance M1 M2](docs/11-acceptance-M1-M2.md) - Hand-run procedure for the M1 and M2 criteria.
- [12 Acceptance M3](docs/12-acceptance-M3.md) - Hand-run procedure for the M3 criteria, and the two that have no route yet.
- [13 Acceptance M4](docs/13-acceptance-M4.md) - Hand-run procedure for the M4 criteria, and why the tool surface needs a live provider.
- [Tools](docs/tools/README.md) - Per-domain tool contracts: arguments, results, schemas.

## Quickstart (intended)

1. **Firmware**: Flash the ESP-IDF project to the XIAO ESP32-S3 using `idf.py flash`.
2. **Config**: Flash a `userfs` LittleFS image. It must ship `config.json` and
   `tasks.json`, plus the built-in `/prompts/`, `/skills/` and the two declarative
   tools. In-session, `config.set` edits `/config.json`, `tasks.set`/`tasks.delete`
   own `/tasks.json`, `fs.put` imports any additional file under `/fs`, and
   `secret.set` writes keys to NVS.
3. **Secrets**: Set `web_search`'s key before first use - it has no fallback:
   ```text
   /secret set brave_key
   ```
   `weather_now` needs no key.
4. **Bridge**: Run the bridge on your PC:
   ```bash
   bin/bridge            # autodetects the board's USB CDC port
   ```
   `bin/bridge --port /dev/ttyACM0` pins the port, and every other flag
   (`--target test`, `--send`, `--json`, ...) passes through to
   `python -m bridge`. Use `uv run python -m bridge` if you would rather not
   depend on the checkout's `.venv`.
5. **Interact**: Use the TUI to chat with the agent or trigger tasks.
