/* Boot-guard arithmetic only: no NVS, no ESP-IDF calls, so the policy links on the
 * host target as well as on the board (`02-firmware.md` §3.2). Keeping it in its own
 * file is what lets `test_host` cover it without mocking NVS. */

#include "sys.h"

/* Saturating, never wrapping: a counter at INT32_MAX stays there rather than
 * turning negative and silently resetting the guard. */
int32_t boot_guard_next_count(int32_t current)
{
    if (current < 0) {
        /* A corrupt read is treated as a fresh device, not as a near-threshold
         * one: NVS returning garbage must not trip safe mode on its own. */
        return 1;
    }
    if (current == INT32_MAX) {
        return current;
    }
    return current + 1;
}

bool boot_guard_should_enter_safe_mode(int32_t count, int32_t safe_mode_after)
{
    /* `count` already includes this boot, because §3.1 step 1 increments before
     * config is readable; safe mode therefore trips once the *prior* consecutive
     * failures reach the threshold. Three failed boots put the fourth into safe
     * mode (`07-roadmap.md` M0), which is `4 > 3`, not `3 >= 3`. */
    return count > boot_guard_clamp_threshold(safe_mode_after);
}

int32_t boot_guard_clamp_threshold(int64_t configured)
{
    /* Out of range falls back to the default rather than clamping to the nearest
     * bound: `02-firmware.md` §3.2 requires the fallback, because a config typo
     * must not make the device either unprotectable or permanently locked out. */
    if (configured < SYS_SAFE_MODE_AFTER_MIN ||
        configured > SYS_SAFE_MODE_AFTER_MAX) {
        return SYS_SAFE_MODE_AFTER_DEFAULT;
    }
    return (int32_t)configured;
}

/* --- The pin allowlist (`01-hardware.md` §6, §6.1) -------------------------
 * Pure: a pin a tool may touch is a rule, and a rule is proven on the host rather
 * than by a board that might or might not have lit the right LED. */

bool sys_pin_readable(int pin)
{
    switch (pin) {
    case 1: case 2: case 3: case 4: case 5: case 6:
    case 7: case 8: case 9: case 43: case 44:
        return true;
    default:
        /* GPIO0 (download strapping) and GPIO21 (onboard LED, §6.1) are not header
         * pins; neither is any pin the module does not bring out. */
        return false;
    }
}

bool sys_pin_writable(int pin)
{
    /* §6.1: GPIO3 is writable after boot — every call here is after boot — and is
     * only forbidden to be held low across a reset, which firmware does not do. */
    return sys_pin_readable(pin);
}

bool sys_pin_adc(int pin)
{
    /* ADC1 only: ADC2 is unavailable while the radio is up (`tools/adc.md`). */
    return pin == 1 || pin == 2 || pin == 4;
}
