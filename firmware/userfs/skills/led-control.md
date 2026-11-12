---
name: led-control
description: Control and read the onboard LED to signal device state or debug output.
version: 1.0.0
keywords: [led, gpio, signal]
tools: [gpio.read, gpio.write]
max_body_bytes: 2048
---

# LED Control

The onboard LED is connected to GPIO21 (active-low). Use this skill to signal status to the user.

## Usage
- To turn the LED ON: Use `gpio.write` with pin 21 and value 0.
- To turn the LED OFF: Use `gpio.write` with pin 21 and value 1.
- To check the current state: Use `gpio.read` on pin 21.

Note: `gpio.write` requires `allow_dangerous` to be true in the config. If the tool is refused, inform the user that dangerous operations are disabled.
