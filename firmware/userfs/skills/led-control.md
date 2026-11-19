---
name: led-control
description: Report device state from the onboard LED; the LED itself is firmware-owned and cannot be driven by a tool.
version: 1.1.0
keywords: [led, gpio, signal, diagnostics]
tools: [sys.diagnostics, gpio.read]
max_body_bytes: 2048
---

# LED Control

The onboard LED is on GPIO21 and is active-low. Firmware owns it: the boot and run
state it shows is not something a tool can change.

## What does not work

`gpio.read` and `gpio.write` both refuse GPIO21 with `E_TOOL`, whatever
`tools.allow_dangerous` says. GPIO21 is reserved in `docs/01-hardware.md` §6.1 because
it is not a header pin, and the pin allowlist in the firmware rejects it before the
gate is consulted. Do not retry with another pin: GPIO13 is the Sense camera pixel
clock and is equally refused.

`gpio.write` on a header pin still needs `tools.allow_dangerous: true`. If that tool is
refused, say the config disables dangerous operations and stop.

## What to do instead

Report state, do not signal it. Call `sys.diagnostics` and read the device counters it
returns, then describe them. For a physical indicator the person can see, ask them to
watch the onboard LED while you run the task, and tell them what it currently means.
