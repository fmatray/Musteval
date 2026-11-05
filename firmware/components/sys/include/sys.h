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

esp_err_t sys_init(void);

/* Onboard user LED, GPIO21, active-low (`01-hardware.md` §6). The inversion is the
 * point of this call: callers ask for lit or dark, never for a logic level. */
esp_err_t sys_led_set(bool on);
