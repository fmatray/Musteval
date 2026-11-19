---
name: i2c-bus
description: Probe, read and write a 7-bit I2C device, including the register-pointer ritual.
metadata:
  version: "1.0.0"
  keywords: "i2c sensor bus peripheral register"
allowed-tools: i2c.scan i2c.read i2c.write
---

# Working with the I2C bus

The bus exists only when `tools.i2c_sda` and `tools.i2c_scl` are set. When they are
`null` the three tools are not in your tool list at all — that is not a refusal and
cannot be fixed from here, so say the bus is unconfigured and stop.

## The three-step ritual

1. `i2c.scan` — get the addresses that actually answer. Never write to an address
   you assumed; the whole point of the scan is that the board tells you.
2. `i2c.read` — with the address the scan returned.
3. `i2c.write` — only when the user asked for a change. This one is gated.

## Addresses

- 7-bit only, in 0x08–0x77. 0x00–0x07 and 0x78–0x7F are reserved and are refused.
- Datasheets usually print the 8-bit form (the 7-bit address shifted left). `0x76`
  is written `0xEC` in that form. Pass the 7-bit number: `{"addr": 118}` for `0x76`.
  Passing an 8-bit value is the single most common way to write to the wrong device,
  and for any address where `addr & 0x80` is clear the shifted form silently names a
  different chip.
- An empty scan result is a success, not an error. It means nothing is wired, the
  pull-ups are missing, or SDA and SCL are swapped. Report which, and do not retry
  in a loop.

## Reading a register

There is no register parameter. `i2c.read` reads from the device's current internal
pointer, so the first byte is usually the pointer itself.

To read register `0x0a` of a device at 0x3c:

1. `i2c.write` `{"addr": 60, "hex": "0a"}` — moves the pointer to `0x0a`.
2. `i2c.read` `{"addr": 60, "len": 2}` — returns the bytes from that pointer.

The pair is not atomic: the bus is installed for one call and dropped before it
returns, so anything else touching the bus between the two calls moves the pointer.
Treat it as read-modify-write on a shared resource, not a transaction.

`len` is at most 64. `hex` is even-length lowercase, no `0x`, no separators, at
most 64 characters (32 bytes). Writing a register means putting the register byte
first: `{"addr": 60, "hex": "0a1f"}` writes `0x0a` then `0x1f`.

## The write gate

`i2c.write` requires `tools.allow_dangerous: true`. With it `false` the call is
refused before the bus is even installed, with `E_TOOL`. That refusal is terminal:
report that actuation is disabled and do not retry or look for a workaround.

A NACK on write is also `E_TOOL`. It means the device did not acknowledge — wrong
address, wrong speed, or a device that cannot take that byte sequence. Re-scan and
re-read before writing again; do not assume the bus is fine.

## Electrical facts that cause most failures

- Standard mode only, 100 kHz. There is no fast-mode option.
- External 4.7 kΩ pull-ups to 3.3 V are required. The SoC's internal pull-ups are
  too weak; a bus without them may scan empty or NACK intermittently.
- Allowed pins only: GPIO1, 2, 4, 5, 6, 7, 8, 9. SDA and SCL must differ.
  Recommended wiring is D4 = SDA (GPIO5), D5 = SCL (GPIO6).
- No clock is held between calls, so two back-to-back calls pay the install cost
  twice. That is a reason to batch, not a reason to loop.

## Reporting

Say which address answered, what the bytes were in hex, and — if you wrote — which
register and value. Never report a read as a measurement without the address it
came from: the same bytes from a different device mean something else.
