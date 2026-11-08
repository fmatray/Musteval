# I2C tools

Probe, read and write 7-bit I2C peripherals on the optional external bus. The bus is
not hardwired: it exists only when `tools.i2c_sda` and `tools.i2c_scl` are set to two
different usable pins. When both keys are `null` (the default), `i2c.scan`,
`i2c.read` and `i2c.write` are **absent from the tool list sent to the model** — this
is not a call-time error, the tools are simply never offered. All three appear
together or not at all: a bus that is configured offers the write tool too, whatever
`tools.allow_dangerous` says, because that key gates the call and not the listing.

| Tool | Gate | Errors |
|---|---|---|
| `i2c.scan` | bus pinned by `tools.i2c_sda` / `tools.i2c_scl`; absent when both are `null` | `E_TOOL` on transfer failure |
| `i2c.read` | same, plus `addr` in 0x08–0x77 and `len` ≤ 64 | `E_TOOL` on bad `addr`, bad `len`, transfer failure |
| `i2c.write` | same, plus `addr` in 0x08–0x77, `hex` ≤ 32 bytes, and `tools.allow_dangerous: true` | `E_TOOL` on a closed gate, bad `addr`, bad `hex`, or NACK |

`i2c.scan` and `i2c.read` need no `tools.allow_dangerous`: both are read-only on the
bus. `i2c.write` changes device state, so it is actuation and requires
`tools.allow_dangerous: true`, exactly like `gpio.write`; with the key `false` the
call is refused before the bus is installed. Gate matrix: `../06-security.md` §6.

The electrical and pin contract lives in `../01-hardware.md` §7.1; this file only
restates what a caller needs to predict behaviour.

- Allowed pins: GPIO1, GPIO2, GPIO4, GPIO5, GPIO6, GPIO7, GPIO8, GPIO9. GPIO3
  (strapping) and GPIO43/GPIO44 (ROM console) are refused even though
  `../01-hardware.md` §6 marks them usable for general I/O.
- SDA and SCL must differ. Recommended wiring is D4 = SDA (GPIO5), D5 = SCL
  (GPIO6).
- Standard mode only, 100 kHz. Fast mode (400 kHz) is deliberately not offered:
  the header carries no pull-ups and 400 kHz over jumper wires is not reliable.
- External 4.7 kΩ pull-ups to 3.3 V are required. The SoC's internal pull-ups are
  too weak and must not be relied on.
- The bus is installed on the first use and deleted before the call returns, so no
  clock is held on the pins between tool calls. Two back-to-back calls pay the
  install cost twice.

## `i2c.scan`

Probes every 7-bit address in 0x08–0x77 (decimal 8–119) and reports the ones that
acknowledged. Addresses 0x00–0x07 and 0x78–0x7F are reserved and are never probed.
Ordering of the returned list is not specified.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| — | — | — | The schema declares no properties. |

**Result**

```json
{"addrs": [8, 60, 87]}
```

An empty array means nothing answered; that is a successful scan, not an error.

**Schema sent to the model**

```json
{"type": "object", "properties": {}}
```

## `i2c.read`

Reads `len` bytes from the 7-bit device at `addr` in one transaction and returns
them hex-encoded. `bytes` is the number of bytes actually returned and equals `len`
on success. There is no sub-address/register parameter, so this reads from the
device's current internal pointer — for register-addressed parts the first returned
byte is typically the register pointer itself. To target a register, issue
`i2c.write` with the register byte first, then `i2c.read`; the bus stays owned
between the two calls only if nothing else touches it, so treat the pair as a
read-modify-write sequence rather than an atomic transaction.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `addr` | integer | yes | 7-bit, 0x08–0x77 (decimal 8–119) |
| `len` | integer | yes | 1–64 |

**Result**

```json
{"hex": "0a1f00ff", "bytes": 4}
```

`hex` is lowercase, two characters per byte, no separators, no `0x` prefix.

**Schema sent to the model**

```json
{"type": "object", "properties": {"addr": {"type": "integer"}, "len": {"type": "integer"}}, "required": ["addr", "len"]}
```

## `i2c.write`

Writes the bytes encoded in `hex` to the 7-bit device at `addr` in one transaction.
The string is even-length lowercase hex, two characters per byte, no separators and
no `0x` prefix, up to 64 characters (32 bytes). There is no sub-address parameter,
so writing a register means putting the register byte first in `hex`:
`{"addr": 60, "hex": "0a1f"}` writes `0x0a` then `0x1f`.

The tool mutates a bus, so it is `DANGEROUS` and refused unless
`tools.allow_dangerous` is `true` (`../06-security.md` §6).

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `addr` | integer | yes | 7-bit, 0x08–0x77 (decimal 8–119) |
| `hex` | string | yes | even length, 2–64 characters, lowercase hex |

**Result**

```json
{"addr": 60, "written": 2}
```

`written` is the number of bytes sent and equals `len(hex) / 2` on success.

**Schema sent to the model**

```json
{"type": "object", "properties": {"addr": {"type": "integer"}, "hex": {"type": "string"}}, "required": ["addr", "hex"]}
```

## Errors

- `E_TOOL` — `addr` outside 0x08–0x77, including the reserved 0x00–0x07 and
  0x78–0x7F ranges (`i2c.read`, `i2c.write`).
- `E_TOOL` — `len` greater than 64, or otherwise failing schema validation. A call
  that fails schema validation is rejected without touching the bus (`i2c.read`).
- `E_TOOL` — transfer failure: NAK, bus arbitration loss, or timeout on the wire.
  For a NACK the device address is named in `detail`, e.g. `{"addr": 60}` (`i2c.write`).
- `E_TOOL` — `hex` odd-length, non-lowercase, non-hex, or longer than 64 characters
  (`i2c.write`).
- `E_TOOL` — `i2c.write` called while `tools.allow_dangerous` is `false`.
- Config-time pin refusal (a reserved pin, `GPIO3`, `GPIO43`, `GPIO44`, or
  SDA equal to SCL) is a `../03-config.md` §tools validation issue rather than a
  tool-call error; the documented code for it is not stated. [ASSUMPTION] It is
  surfaced as `E_CONFIG` at load, the same way other invalid config values are.

## See also

- `../01-hardware.md` §7.1 — pins, pull-ups, why 100 kHz
- `../03-config.md` — `tools.i2c_sda` / `tools.i2c_scl`
- `../02-firmware.md` §7.1 — built-in tool table
- `../06-security.md` §6 — authorization model
