/* clock_pure.c — the timing arithmetic, with no radio and no system clock.
 * Split from `clock.c` for the same reason `cfg_doc.c` is split from `cfg.c`: the
 * clamps are rules that must hold, and a rule that needs a board to check is a rule
 * nobody checks (`AGENTS.md` coverage gates). */

#include <stdint.h>

#include "clock.h"

/* Below a minute a re-sync costs more traffic than it buys; above a day the clock
 * drifts past what TLS tolerates. Both ends clamp rather than refuse, because a
 * wrong interval must not stop the device from keeping time at all. */
#define CLOCK_MIN_INTERVAL_S 60
#define CLOCK_MAX_INTERVAL_S 86400

int64_t clock_next_interval_ms(int32_t configured_s)
{
    if (configured_s < CLOCK_MIN_INTERVAL_S) configured_s = CLOCK_MIN_INTERVAL_S;
    if (configured_s > CLOCK_MAX_INTERVAL_S) configured_s = CLOCK_MAX_INTERVAL_S;
    return (int64_t)configured_s * 1000;
}
