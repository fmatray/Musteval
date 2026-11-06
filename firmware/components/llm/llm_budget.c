/* llm_budget.c — the two fixed UTC windows of `03-config.md` §llm.
 * Pure: the counters are the caller's, the clock is the caller's. A window rule that
 * needs a board to check is a window rule that only fails in production, and the
 * failure mode here is silent overspend. */

#include <stddef.h>

#include "llm.h"

void llm_budget_reset(llm_budget_t *budget)
{
    if (budget == NULL) return;
    budget->hour = (llm_window_t){0, 0};
    budget->day = (llm_window_t){0, 0};
}

/* One window's decision: does `now` still belong to the stored bucket, and how much
 * of `cap` is left. Split out so both windows run the identical rule rather than two
 * copies of it that can drift. */
static bool window_charge(llm_window_t *window, int32_t cap, int64_t bucket)
{
    if (window->bucket != bucket) {
        /* A different bucket is a fresh window. The stale count is discarded, not
         * trusted: a counter from a previous boot must not spend today's budget. */
        window->bucket = bucket;
        window->count = 0;
    }
    /* `cap == 0` disables the window (`03-config.md` §llm): it never refuses, but it
     * still counts, because `status` reports `llm.calls_hour` and a reported zero
     * would read as "nothing was ever sent". */
    if (cap > 0 && window->count >= cap) return false;
    window->count++;
    return true;
}

bool llm_budget_charge(llm_budget_t *budget, int32_t max_hour, int32_t max_day,
                       int64_t unix_now)
{
    if (budget == NULL) return false;
    /* Floor division, not truncation: a negative clock (an unsynced device before
     * 1970) must not collapse two different hours into bucket 0. */
    int64_t hour_bucket = unix_now / 3600;
    int64_t day_bucket = unix_now / 86400;
    if (unix_now < 0 && unix_now % 3600 != 0) hour_bucket--;
    if (unix_now < 0 && unix_now % 86400 != 0) day_bucket--;

    /* The hour is checked before the day so a call refused by the hour window does not
     * consume the day's allowance: a refused call is not counted (`02-firmware.md`
     * §llm). Testing the day after incrementing the hour would spend two windows on
     * one refusal. */
    llm_window_t hour_before = budget->hour;
    llm_window_t day_before = budget->day;

    if (!window_charge(&budget->hour, max_hour, hour_bucket)) {
        budget->hour = hour_before;
        budget->day = day_before;
        return false;
    }
    if (!window_charge(&budget->day, max_day, day_bucket)) {
        budget->hour = hour_before;
        budget->day = day_before;
        return false;
    }
    return true;
}

void llm_budget_counters(const llm_budget_t *budget, int32_t *hour, int32_t *day)
{
    if (hour != NULL) *hour = budget != NULL ? budget->hour.count : 0;
    if (day != NULL) *day = budget != NULL ? budget->day.count : 0;
}
