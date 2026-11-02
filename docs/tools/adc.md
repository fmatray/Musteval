# ADC tools

Voltage sampling on the header pins: `adc.read` samples the analog voltage on a subset of the header's GPIOs.
There is no configuration gate; access is limited by a hard-coded pin allowlist (`../01-hardware.md` §6.2).

| Tool | Gate | Errors |
|---|---|---|
| `adc.read` | pin allowlist | `E_TOOL` on an invalid pin |

## `adc.read`

Samples the voltage on `pin` and returns the raw 12-bit code and a millivolt value. The
sampling is performed via the ESP-IDF ADC calibration driver.

Sampling is limited to **ADC1** only. ADC2 is claimed by the WiFi driver and is not
offered; any attempt to read from ADC2 pins returns `E_TOOL`.

Allowlist (pins that are ADC1-capable and not assigned to a bus role):
- GPIO1 (D0)
- GPIO2 (D1)
- GPIO4 (D3)

The exclusions are not all for the same reason (`../01-hardware.md` §6.2):

| Pin | Why excluded |
|---|---|
| GPIO3 (D2) | ADC-incompatible while strapping: a valid analog read needs the pin free of the reset-time strap, which it is not |
| GPIO5, GPIO6 (D4/D5) | ADC1 CH4/CH5, but carry the recommended I2C roles; enabling analog would conflict with the bus |
| GPIO7, GPIO8, GPIO9 (D8-D10) | ADC1 CH6-CH8, but carry the recommended SPI roles |

All of them are refused with `E_TOOL`. Only GPIO1, GPIO2 and GPIO4 are sampled.

Sampling happens at 12-bit resolution with `ADC_ATTEN_DB_12` attenuation, which
provides a nominal range of 0 to 3.1 V. Because ADC1 attenuation and calibration
are affected by WiFi activity on this SoC, all analog sampling must happen with
the radio idle [ASSUMPTION]; quantified error is unmeasured.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `pin` | integer | yes | GPIO1, GPIO2, or GPIO4; others are refused |

**Result**

```json
{"raw":2048,"mv":1550}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"pin":{"type":"integer"}},"required":["pin"]}
```

## Errors

- `E_TOOL` -- `pin` is not in the allowlist (GPIO1, GPIO2, GPIO4).
- `E_TOOL` -- the arguments failed schema validation, for example `pin` is not an integer; nothing is executed (`../02-firmware.md` §7.1).
- `E_TOOL` -- runtime failure during sampling, such as an internal driver timeout (`../06-security.md` §6).

## See also

- `../01-hardware.md` §6.2 -- ADC1 pin layout and attenuation facts.
- `../02-firmware.md` §7.1 -- the built-in tool table.
- `gpio.md` -- digital level reads of ADC pins.
