/* sys — hardware abstraction and the boot-guard policy.
 * Layering: L0 (`08-code-architecture.md` §1). The policy half is pure, which is
 * why it is testable on the host (`02-firmware.md` §3.2). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/* `sys.safe_mode_after` bounds, `02-firmware.md` §3.2: 2..10, default 3. A value
 * outside the range falls back to the default rather than locking the device out. */
#define SYS_SAFE_MODE_AFTER_MIN     2
#define SYS_SAFE_MODE_AFTER_MAX     10
#define SYS_SAFE_MODE_AFTER_DEFAULT 3

/* Pure policy, no NVS access, so the arithmetic is unit-testable without flashing
 * (`02-firmware.md` §3.2). Both saturate rather than wrap: a counter that cannot
 * be read back must not reach the threshold by accident. */
int32_t boot_guard_next_count(int32_t current);
bool    boot_guard_should_enter_safe_mode(int32_t count, int32_t safe_mode_after);

/* Clamp a configured threshold into 2..10, falling back to the default outside it.
 * Called with the value read from `config.json`, or with the default when the file
 * does not parse. */
int32_t boot_guard_clamp_threshold(int64_t configured);

esp_err_t sys_board_init(void);

/* Onboard user LED, GPIO21, active-low (`01-hardware.md` §6). The inversion is the
 * point of this call: callers ask for lit or dark, never for a logic level. */
esp_err_t sys_led_set(bool on);

/* --- GPIO, ADC and I2C for the tool layer (`02-firmware.md` §7, `01` §6) ---- */

/* True when `pin` may be read: an exposed header pin, excluding the two reserved
 * strapping pins (`01-hardware.md` §6.1). Pure, so the allowlist is a host test. */
bool sys_pin_readable(int pin);

/* True when `pin` may be driven. The same set as readable, plus the post-boot
 * allowance for GPIO3, which §6.1 permits writing after boot and forbids holding low
 * across a reset. GPIO0 and the LED pin are never in it. Pure. */
bool sys_pin_writable(int pin);

/* True when `pin` is an ADC1 channel the tool may sample: GPIO1, GPIO2, GPIO4
 * (`tools/adc.md`). Pure. */
bool sys_pin_adc(int pin);

esp_err_t sys_gpio_read(int pin, int *level);
esp_err_t sys_gpio_write(int pin, int level);

/* 12-bit raw sample (`tools/adc.md`). */
esp_err_t sys_adc_read(int pin, int *raw);

/* One combined read transaction; `len` is 1..64. `sda`/`scl` come from
 * `tools.i2c_sda` / `tools.i2c_scl` and the bus is opened on first use. */
esp_err_t sys_i2c_read(int sda, int scl, int addr, uint8_t *out, size_t len);
esp_err_t sys_i2c_write(int sda, int scl, int addr, const uint8_t *data, size_t len);

/* Zero-length probe: ESP_OK when a device acknowledged `addr`. */
esp_err_t sys_i2c_probe(int sda, int scl, int addr);
