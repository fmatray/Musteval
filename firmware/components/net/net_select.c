/* net_select.c — AP ordering and backoff arithmetic, no radio.
 * Pure so the rules `03-config.md` §wifi states are checked by the host suite; a
 * wrong order only shows up as "connected to the wrong hotspot", which is hard to
 * notice on a bench and trivial to test as a list. */

#include <stddef.h>
#include <stdint.h>

#include "net.h"

void net_rank(net_candidate_t *candidates, size_t count)
{
    if (candidates == NULL) return;
    /* Insertion sort: at most `NET_MAX_NETWORKS` entries, and stability is part of
     * the contract (equal priorities keep the config order), which a qsort with a
     * two-field comparator would not give without extra work. */
    for (size_t i = 1; i < count; i++) {
        net_candidate_t held = candidates[i];
        size_t j = i;
        while (j > 0 && candidates[j - 1].priority < held.priority) {
            candidates[j] = candidates[j - 1];
            j--;
        }
        candidates[j] = held;
    }
}

int32_t net_backoff_ms(const int32_t *backoff, size_t count, size_t attempt)
{
    /* `retry_backoff_ms` is a short list applied in order; an attempt past its end
     * reuses the last entry rather than waiting forever or not waiting at all. */
    if (backoff == NULL || count == 0) return 0;
    size_t index = attempt < count ? attempt : count - 1;
    return backoff[index];
}
