/* geo_parse.c — the pure half of `geo`: provider-response parsing, the scan body and
 * the cache-freshness rule (`02-firmware.md` §geo, `docs/tools/geo.md`). No radio, no
 * socket, no clock, so every rule here is provable from a host test. */

#include "geo.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "store.h"

/* The union of the two provider families' documented shapes. Both are third-party, so
 * the parser is the one place their differences are absorbed; a field this does not
 * name is ignored rather than refused, because a provider adding `city` is not a
 * failure. */
static const char *const k_lat_keys[] = { "lat", "latitude" };
static const char *const k_lon_keys[] = { "lon", "lng", "longitude" };
static const char *const k_acc_keys[] = { "accuracy_m", "accuracy" };

static bool coord_from(const json_node_t *obj, const char *const *keys, size_t n,
                       double *out)
{
    for (size_t i = 0; i < n; i++) {
        const json_node_t *v = json_get(obj, keys[i]);
        if (v != NULL && json_double(v, out)) return true;
    }
    return false;
}

bool geo_parse_fix(const char *body, size_t len, int32_t accuracy_default, geo_fix_t *out)
{
    if (body == NULL || out == NULL || len == 0) return false;
    /* Some providers answer with a JSON *string* containing JSON — a quoted body is not
     * a fix, and unquoting it would make `geo` a general JSON un-wrapper. */
    json_doc_t *doc = NULL;
    if (json_parse(body, len, &doc) != ESP_OK || doc == NULL) return false;

    bool ok = false;
    json_node_t *root = json_root(doc);
    /* `{"location":{"lat":..,"lng":..}}` and the flat form are both documented by
     * providers in this space; accept either rather than guess one. */
    const json_node_t *holder = json_get(root, "location");
    if (holder == NULL || !json_is_object(holder)) holder = root;

    double lat = 0, lon = 0;
    if (coord_from(holder, k_lat_keys, sizeof k_lat_keys / sizeof k_lat_keys[0], &lat) &&
        coord_from(holder, k_lon_keys, sizeof k_lon_keys / sizeof k_lon_keys[0], &lon)) {
        /* A place on Earth, or a provider bug. 0,0 is a real location (the Gulf of
         * Guinea), so it is accepted; out-of-range is not. */
        if (lat >= -90.0 && lat <= 90.0 && lon >= -180.0 && lon <= 180.0) {
            out->lat = lat;
            out->lon = lon;
            out->accuracy_m = accuracy_default;
            int64_t acc = 0;
            for (size_t i = 0; i < sizeof k_acc_keys / sizeof k_acc_keys[0]; i++) {
                const json_node_t *v = json_get(holder, k_acc_keys[i]);
                if (v != NULL && json_int(v, &acc)) {
                    if (acc >= 0 && acc <= 1000000) out->accuracy_m = (int32_t)acc;
                    break;
                }
            }
            ok = true;
        }
    }
    json_free(doc);
    return ok;
}

size_t geo_scan_body(const char *const *bssids, const int32_t *rssi, size_t count,
                     char *buf, size_t cap)
{
    if (buf == NULL || cap == 0) return 0;
    if (count > 0 && (bssids == NULL || rssi == NULL)) return 0;

    size_t used = 0;
    int n = snprintf(buf, cap, "[");
    if (n <= 0 || (size_t)n >= cap) return 0;
    used = (size_t)n;

    for (size_t i = 0; i < count; i++) {
        if (bssids[i] == NULL) return 0;
        /* BSSID strings come from the driver, not from a config file, but they are
         * still spliced into JSON: a quote in one would produce a body the provider
         * rejects with a 400 the reader cannot act on. */
        for (const char *c = bssids[i]; *c != '\0'; c++) {
            if (*c == '"' || *c == '\\') return 0;
        }
        n = snprintf(buf + used, cap - used, "%s{\"bssid\":\"%s\",\"rssi\":%d}",
                     i == 0 ? "" : ",", bssids[i], (int)rssi[i]);
        if (n <= 0 || (size_t)n >= cap - used) return 0;
        used += (size_t)n;
    }

    n = snprintf(buf + used, cap - used, "]");
    if (n <= 0 || (size_t)n >= cap - used) return 0;
    return used + (size_t)n;
}

bool geo_fix_is_fresh(int64_t source_ts, int64_t now, int64_t ttl_s)
{
    if (source_ts <= 0 || ttl_s <= 0) return false;
    /* `now < source_ts` is a clock that went backwards (an NTP step after a fix). The
     * fix is not made eternal by it: the caller re-syncs and takes a new one. */
    if (now < source_ts) return false;
    /* Strictly less than the TTL: a fix exactly `ttl_s` old is stale, which is what
     * "held for `geo.cache_ttl_s`" reads as and what the test pins. */
    return (now - source_ts) < ttl_s;
}

const geo_provider_t *geo_provider_at(const geo_provider_t *chain, size_t count,
                                      size_t index)
{
    if (chain == NULL || index >= count) return NULL;
    return &chain[index];
}

bool geo_kind_posts(geo_kind_t kind)
{
    /* Exhaustive over the enum rather than `kind != GEO_KIND_IP`, so a third kind
     * added later has to state its verb here instead of inheriting POST silently. */
    switch (kind) {
    case GEO_KIND_WIFI: return true;
    case GEO_KIND_IP:   return false;
    }
    return true;
}
