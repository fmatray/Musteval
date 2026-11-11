/* net_select.c — AP ordering and backoff arithmetic, no radio.
 * Pure so the rules `03-config.md` §wifi states are checked by the host suite; a
 * wrong order only shows up as "connected to the wrong hotspot", which is hard to
 * notice on a bench and trivial to test as a list. */

#include <stddef.h>
#include <stdint.h>
#include <string.h>

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

const char *net_state_name(net_state_t state)
{
    switch (state) {
    case NET_STATE_OFF:        return "off";
    case NET_STATE_CONNECTING: return "connecting";
    case NET_STATE_CONNECTED:  return "connected";
    case NET_STATE_FAILED:     return "failed";
    }
    return "off";
}

/* --- Scan condensation (`04-protocol.md` §4.24) --------------------------- */

const char *net_auth_name(uint8_t auth)
{
    /* The numbers are `wifi_auth_mode_t`, fixed by the driver's ABI; the words are
     * §4.24's. Two enterprise modes keep their own words because a reader choosing a
     * network benefits from knowing it wants a username. */
    switch (auth) {
    case 0: return "open";             /* WIFI_AUTH_OPEN */
    case 1: return "wep";              /* WIFI_AUTH_WEP */
    case 2: return "wpa";              /* WIFI_AUTH_WPA_PSK */
    case 3: return "wpa2";             /* WIFI_AUTH_WPA2_PSK */
    case 4: return "wpa";              /* WIFI_AUTH_WPA_WPA2_PSK */
    case 5: return "wpa2_enterprise";  /* WIFI_AUTH_WPA2_ENTERPRISE */
    case 6: return "wpa3";             /* WIFI_AUTH_WPA3_PSK */
    case 7: return "wpa2";             /* WIFI_AUTH_WPA2_WPA3_PSK */
    case 8: return "wpa3_enterprise";  /* WIFI_AUTH_WPA3_ENT_192 */
    default: return "unknown";
    }
}

void net_scan_condense(net_scan_entry_t *entries, size_t *count, size_t cap,
                       bool *truncated)
{
    if (truncated != NULL) *truncated = false;
    if (entries == NULL || count == NULL) return;

    size_t kept = 0;
    for (size_t i = 0; i < *count; i++) {
        const char *ssid = entries[i].ssid;
        /* A hidden network has no name, and an empty string is not a usable
         * `wifi.networks[].ssid` under `03-config.md` (1..32 chars). */
        if (ssid[0] == '\0') continue;

        size_t at = kept;
        for (size_t j = 0; j < kept; j++) {
            if (strcmp(entries[j].ssid, ssid) == 0) { at = j; break; }
        }
        if (at == kept) {
            entries[kept++] = entries[i];
        } else if (entries[i].rssi > entries[at].rssi) {
            /* One name per row, carrying the signal the reader can actually use: a
             * router's 2.4 GHz and 5 GHz radios share an SSID. */
            entries[at] = entries[i];
        }
    }

    /* Insertion sort, strongest first. At most `NET_SCAN_MAX` entries; the list is
     * already small enough that a sort's asymptotics do not matter and stability is
     * not required. */
    for (size_t i = 1; i < kept; i++) {
        net_scan_entry_t key = entries[i];
        size_t j = i;
        while (j > 0 && entries[j - 1].rssi < key.rssi) {
            entries[j] = entries[j - 1];
            j--;
        }
        entries[j] = key;
    }

    if (kept > cap) {
        kept = cap;
        if (truncated != NULL) *truncated = true;
    }
    *count = kept;
}

/* --- Reachability (`04-protocol.md` §4.25) -------------------------------- */

void net_ping_summarise(const int32_t *rtt_ms, size_t count, net_ping_stats_t *out)
{
    if (out == NULL) return;
    *out = (net_ping_stats_t){0};
    if (rtt_ms == NULL || count == 0) return;

    int64_t sum = 0;
    int32_t min = INT32_MAX, max = 0;
    int32_t received = 0;

    for (size_t i = 0; i < count; i++) {
        int32_t rtt = rtt_ms[i];
        if (rtt < 0) continue;
        if (received == 0 || rtt < min) min = rtt;
        if (received == 0 || rtt > max) max = rtt;
        sum += rtt;
        received++;
    }

    out->sent = (int32_t)count;
    out->received = received;
    out->rtt_min_ms = received ? min : 0;
    out->rtt_avg_ms = received ? (int32_t)((sum + received / 2) / received) : 0;
    out->rtt_max_ms = received ? max : 0;
    out->loss_pct = (int32_t)(((int64_t)(count - received) * 100 + count / 2) / count);
}
