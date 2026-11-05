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
