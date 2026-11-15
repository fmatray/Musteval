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

static const json_node_t *field_of(const json_node_t *holder, const json_node_t *root,
                                   const char *key)
{
    const json_node_t *v = json_get(holder, key);
    /* A wrapped body keeps its coordinates under `location`; place fields sit beside
     * them, but falling back to the top level costs nothing when a provider splits the
     * two. */
    return (v == NULL && holder != root) ? json_get(root, key) : v;
}

static void copy_str(const json_node_t *node, char *out, size_t cap)
{
    const char *s = node == NULL ? NULL : json_string(node);
    if (s != NULL) snprintf(out, cap, "%s", s);
}

static bool two_digits(const char *s, int *out)
{
    if (s[0] < '0' || s[0] > '9' || s[1] < '0' || s[1] > '9') return false;
    *out = (s[0] - '0') * 10 + (s[1] - '0');
    return true;
}

bool geo_parse_fix(const char *body, size_t len, int32_t accuracy_default, geo_fix_t *out)
{
    if (body == NULL || out == NULL || len == 0) return false;
    json_doc_t *doc = NULL;
    if (json_parse(body, len, &doc) != ESP_OK || doc == NULL) return false;

    bool ok = false;
    json_node_t *root = json_root(doc);
    const json_node_t *holder = json_get(root, "location");
    if (holder == NULL || !json_is_object(holder)) holder = root;

    double lat = 0, lon = 0;
    if (coord_from(holder, k_lat_keys, sizeof k_lat_keys / sizeof k_lat_keys[0], &lat) &&
        coord_from(holder, k_lon_keys, sizeof k_lon_keys / sizeof k_lon_keys[0], &lon)) {
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

            /* Place and locale: absorb provider dialects and normalize. */
            out->in_eu = -1;
            copy_str(field_of(holder, root, "city"), out->city, sizeof out->city);
            copy_str(field_of(holder, root, "region"), out->region, sizeof out->region);
            copy_str(field_of(holder, root, "region_code"), out->region_code,
                    sizeof out->region_code);
            copy_str(field_of(holder, root, "country_code"), out->country_code,
                    sizeof out->country_code);
            copy_str(field_of(holder, root, "country_code_iso3"), out->country_code_iso3,
                    sizeof out->country_code_iso3);
            copy_str(field_of(holder, root, "country_tld"), out->country_tld,
                    sizeof out->country_tld);
            copy_str(field_of(holder, root, "continent_code"), out->continent_code,
                    sizeof out->continent_code);
            copy_str(field_of(holder, root, "postal"), out->postal, sizeof out->postal);
            copy_str(field_of(holder, root, "currency"), out->currency,
                    sizeof out->currency);
            copy_str(field_of(holder, root, "currency_name"), out->currency_name,
                    sizeof out->currency_name);
            copy_str(field_of(holder, root, "languages"), out->languages,
                    sizeof out->languages);

            /* Country Name: prefer `country_name`; else take `country` only when a code
             * also exists and differs, so a bare `country:"FR"` is not reported as a
             * name. Both providers in the chain send `country_code`, which is what
             * makes the comparison decidable. */
            const json_node_t *name = field_of(holder, root, "country_name");
            if (name != NULL) copy_str(name, out->country, sizeof out->country);
            else {
                const json_node_t *c = field_of(holder, root, "country");
                const json_node_t *cc = field_of(holder, root, "country_code");
                const char *cv = c ? json_string(c) : NULL;
                const char *ccv = cc ? json_string(cc) : NULL;
                if (cv != NULL && ccv != NULL && strcmp(cv, ccv) != 0) {
                    snprintf(out->country, sizeof out->country, "%s", cv);
                }
            }

            /* Capital: `country_capital` or `capital`. */
            const json_node_t *cap = field_of(holder, root, "country_capital");
            if (cap == NULL) cap = field_of(holder, root, "capital");
            copy_str(cap, out->country_capital, sizeof out->country_capital);

            /* Calling Code: `country_calling_code` or `calling_code`. */
            const json_node_t *call = field_of(holder, root, "country_calling_code");
            if (call == NULL) call = field_of(holder, root, "calling_code");
            const char *ccs = call ? json_string(call) : NULL;
            if (ccs != NULL) geo_normalize_calling_code(ccs, out->country_calling_code,
                                                       sizeof out->country_calling_code);

            /* Timezone and Offset: handle nested object or top-level string. */
            const json_node_t *tz = field_of(holder, root, "timezone");
            if (tz != NULL) {
                const char *tzs = json_string(tz);
                if (tzs != NULL) snprintf(out->timezone, sizeof out->timezone, "%s", tzs);
                else if (json_is_object(tz)) {
                    copy_str(json_get(tz, "id"), out->timezone, sizeof out->timezone);
                }
            }

            const char *off = json_string(field_of(holder, root, "utc_offset"));
            if (off == NULL && json_is_object(tz)) off = json_string(json_get(tz, "utc"));
            if (off != NULL) geo_normalize_utc_offset(off, out->utc_offset,
                                                       sizeof out->utc_offset);

            /* EU membership: only JSON boolean. */
            const json_node_t *eu = field_of(holder, root, "in_eu");
            if (eu == NULL) eu = field_of(holder, root, "is_eu");
            bool eu_flag = false;
            if (eu != NULL && json_bool(eu, &eu_flag)) out->in_eu = eu_flag ? 1 : 0;

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

bool geo_chain_needs_scan(const geo_provider_t *chain, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        if (chain[i].kind == GEO_KIND_WIFI) return true;
    }
    return false;
}

bool geo_normalize_utc_offset(const char *in, char *out, size_t cap)
{
    /* `geo.h`: a refused value leaves `out` empty. `geo_parse_fix` guards on the
     * return, but a caller that ignored it would otherwise publish whatever the
     * previous provider left in the buffer, which is a real place's offset. */
    if (out != NULL && cap > 0) out[0] = '\0';
    if (in == NULL || out == NULL || cap < 7) return false;
    if (strcmp(in, "Z") == 0 || strcmp(in, "+0000") == 0 || strcmp(in, "+00:00") == 0) {
        snprintf(out, cap, "+00:00");
        return true;
    }
    if (in[0] != '+' && in[0] != '-') return false;
    int h = 0, m = 0;
    const char *p = in + 1;
    if (!two_digits(p, &h)) return false;
    p += 2;
    if (*p == ':') p++;
    if (!two_digits(p, &m)) return false;
    /* A trailing character means this is not an offset we understand (`+01:00Z`,
     * `+0100x`); accepting a prefix would publish an offset the provider never sent. */
    if (p[2] != '\0') return false;
    if (h > 23 || m > 59) return false;
    snprintf(out, cap, "%c%02d:%02d", in[0], h, m);
    return true;
}

bool geo_normalize_calling_code(const char *in, char *out, size_t cap)
{
    if (out != NULL && cap > 0) out[0] = '\0';
    if (in == NULL || out == NULL || cap < 2) return false;
    const char *p = (in[0] == '+') ? in + 1 : in;
    if (*p == '\0') return false;
    size_t len = 0;
    while (p[len] >= '0' && p[len] <= '9') len++;
    /* The digits must be the whole string: `3x` and `33 1` are not calling codes, and
     * truncating them to `+3` would name a country the provider did not. */
    if (len == 0 || p[len] != '\0' || len + 1 >= cap) return false;
    snprintf(out, cap, "+%.*s", (int)len, p);
    return true;
}
