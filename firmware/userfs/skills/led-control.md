---
name: led-control
description: The onboard LED is firmware-owned and permanently unlit; no tool can read or drive it. Report state from sys.diagnostics.
version: 1.2.0
keywords: [led, gpio, signal, diagnostics]
tools: [sys.diagnostics, gpio.read]
max_body_bytes: 2048
---

# LED Control

The onboard LED is on GPIO21 and is active-low. The only write to it in the firmware is
the one that clears it at boot, so it stays unlit for the whole session and carries no
information about device state.

## What does not work

`gpio.read` and `gpio.write` both refuse GPIO21 with `E_TOOL`, whatever
`tools.allow_dangerous` says. GPIO21 is not a header pin, so the pin allowlist in the
firmware rejects it before the gate is consulted (`docs/01-hardware.md` §6.1). Do not
retry with another pin: GPIO13 is the Sense camera pixel clock and is equally refused.

`gpio.write` on a header pin still needs `tools.allow_dangerous: true`. If that tool is
refused, say the config disables dangerous operations and stop.

## What to do instead

Report state, do not signal it. Call `sys.diagnostics` and describe the counters it
returns. If the person asks about the LED, tell them it is not driven: a dark LED means
the board is powered off, not that something failed.
