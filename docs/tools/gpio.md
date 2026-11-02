# GPIO tools

Digital level access to the header pins: `gpio.read` for input, `gpio.write` for output.
`gpio.read` is always available; `gpio.write` is gated on `tools.allow_dangerous: true` (`../06-security.md` §6).

| Tool | Gate | Errors |
|---|---|---|
| `gpio.read` | none | `E_TOOL` on a reserved pin |
| `gpio.write` | `tools.allow_dangerous: true` | `E_TOOL` on a reserved pin or when the gate is off |

## `gpio.read`

Samples the digital level of `pin` and returns `0` or `1`. The pin is read as a plain
input; no pull mode is applied by the tool. Reading an ADC-capable pin returns the
**digital** level, not a voltage -- voltage is `adc.read` (`../01-hardware.md` §6.2).

Any pin that is not reserved may be read, including pins the agent does not
otherwise own. Reserved pins are refused rather than silently misread, because a
read on the flash or PSRAM lines is a bus fault and not a bad value.

Reserved, always `E_TOOL` (`../01-hardware.md` §6.1):

| GPIO | Reason |
|---|---|
| GPIO0 | Boot mode strapping; low at reset selects download mode |
| GPIO3 | Strapping (JTAG source); writable after boot, never held low across reset |
| GPIO19, GPIO20 | USB D- / D+; driving them kills the link |
| GPIO21 | Onboard LED, active-low, firmware-owned |
| GPIO26-GPIO32 | Quad SPI flash and PSRAM |
| GPIO33-GPIO37 | Octal PSRAM extra data lines |
| GPIO38-GPIO48 | Not exposed on this board |
| GPIO45, GPIO46 | Strapping (VDD_SPI voltage, ROM messages) |

Usable: GPIO1, GPIO2, GPIO4, GPIO5, GPIO6, GPIO7, GPIO8, GPIO9, and GPIO43/GPIO44
with the UART0-console caveat noted below.

Pin numbers are **GPIO numbers**, not the silkscreen D-numbers: D0 is GPIO1, D6 is
GPIO43, and so on. The full mapping is the table in `../01-hardware.md` §6.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `pin` | integer | yes | 0-48; reserved pins are refused at call time, not schema time |

**Result**

```json
{"level":0}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"pin":{"type":"integer"}},"required":["pin"]}
```

## `gpio.write`

Drives `pin` to `level`. The tool sets the pin high or low once and returns; it does
not hold a level across a reset and it does not configure a latch. `level` is
restricted to `0` or `1` by the schema, so there is no toggle or pulse form.

This is a write tool: with `tools.allow_dangerous` false the call is refused before
any pin is touched (`../06-security.md` §6). The same reserved list as `gpio.read`
applies, plus the strap rule for GPIO3 -- firmware refuses it outright rather than
allowing a post-boot write.

GPIO43 (D6) and GPIO44 (D7) are usable but carry the UART0 ROM console: driving them
loses ROM-stage logging, and GPIO44 is the ROM RX line. Leave them alone while
diagnosing boot problems. `../01-hardware.md` §6.3 governs the electrical side: 3.3 V
logic only, a few mA for signalling, never a relay or motor directly.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `pin` | integer | yes | 0-48; reserved pins are refused |
| `level` | integer | yes | `0` or `1` |

**Result**

```json
{"ok":true}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"pin":{"type":"integer"},"level":{"type":"integer","enum":[0,1]}},"required":["pin","level"]}
```

## Errors

- `E_TOOL` -- `gpio.read` or `gpio.write` was called with a reserved pin (the table above).
- `E_TOOL` -- `gpio.write` was called while `tools.allow_dangerous` is `false`.
- `E_TOOL` -- the arguments failed schema validation, for example `level` outside `0`/`1` or a missing `pin`; nothing is executed (`../02-firmware.md` §7.1).
- `E_INTERNAL` -- the GPIO driver call itself failed. [ASSUMPTION] This is not reachable for a valid, non-reserved pin on the fixed board.

## See also

- `../01-hardware.md` §6 -- GPIO-to-D-number map and default functions.
- `../01-hardware.md` §6.1 -- why each reserved pin is reserved.
- `../01-hardware.md` §6.3 -- electrical limits per pin.
- `../02-firmware.md` §7.1 -- the built-in tool table with gates.
- `../06-security.md` §6 -- authorization model for write tools.
- `adc.md` -- the analog path for the ADC-capable pins.
