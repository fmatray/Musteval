/* geo — WiFi positioning with an ordered provider chain.
 * Layering: L1 (`08-code-architecture.md` §1), above `cfg`, `net`, `clock` and
 * `store`. There is no GPS receiver: a fix comes from a WiFi scan sent to a
 * positioning provider, with an IP lookup as the last resort.
 * Contracts: `02-firmware.md` §geo, `03-config.md` §geo, `docs/tools/geo.md`. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* `03-config.md` §geo bounds. */
#define GEO_MAX_PROVIDERS 4
#define GEO_CACHE_TTL_S_DEFAULT 900
#define GEO_TIMEOUT_MS_DEFAULT 5000
#define GEO_MAX_BSSIDS_DEFAULT 12
#define GEO_ID_MAX 16
#define GEO_URL_MAX 192
#define GEO_KIND_MAX 10

typedef enum {
    GEO_KIND_WIFI = 0, /* `wifi_scan`: sends `[{bssid, rssi}]`, needs a key */
    GEO_KIND_IP,       /* `ip`: sends nothing, must not carry a key */
} geo_kind_t;

typedef struct {
    char       id[GEO_ID_MAX + 1];
    geo_kind_t kind;
    char       url[GEO_URL_MAX + 1];
    char       api_key_ref[16]; /* empty for `ip` */
} geo_provider_t;

/* One fix. `accuracy_m` is exactly what the provider reported; firmware does not
 * refine it (`docs/tools/geo.md` §Accuracy). `source_ts` is the Unix second the fix
 * was obtained, which is also the reference the cache age is measured against. */
typedef struct {
    double  lat;
    double  lon;
    int32_t accuracy_m;
    int64_t source_ts;
    char    provider[GEO_ID_MAX + 1];
} geo_fix_t;

/* Read `geo.*` and validate the chain. Returns ESP_OK with `enabled` false when
 * `geo.enabled` is absent or false — a disabled geolocator is a valid device, not a
 * config error. Returns ESP_ERR_INVALID_ARG for a provider that breaks
 * `03-config.md` §geo: a `wifi_scan` without `api_key_ref`, an `ip` with one, or a
 * URL that is not https. Also resolves each `api_key_ref`; an unresolved ref is
 * ESP_ERR_NOT_FOUND with `why` naming the provider, which is a boot refusal because
 * the chain would otherwise fail at the first lookup with a 401. */
esp_err_t geo_init(void);

bool geo_enabled(void);

/* The fix, from cache while fresh, else a new lookup. Returns ESP_ERR_INVALID_STATE
 * when disabled, ESP_ERR_TIMEOUT when a provider overran its budget and no later one
 * answered, ESP_ERR_NOT_FOUND when the whole chain failed. A failure caches nothing,
 * so the next call retries the chain. Blocks: a cold lookup holds the caller for up to
 * `geo.timeout_ms` per provider. Board-only. */
esp_err_t geo_locate(geo_fix_t *out);

/* The cached fix, if one has been taken, for `04-protocol.md` §3.9's `geo` object.
 * Returns false before the first successful lookup, which is the JSON null §3.9
 * requires: an object of zeroes would report a real place in the Gulf of Guinea.
 * `link` owns the JSON shape, so this hands over the facts rather than building them. */
bool geo_cached_fix(geo_fix_t *out);

/* --- Pure rules (`geo_parse.c`) ------------------------------------------ */

/* Parse a provider response body into `out`. The two provider families answer in
 * different shapes and neither is under our control, so this accepts the union of
 * what they document: a flat object with `lat`/`lon` plus an optional `accuracy` or
 * `accuracy_m`, and the nested `{"location":{"lat":..,"lng":..}}` form. The response
 * is untrusted input (`06-security.md` §6.4): a body that does not carry both
 * coordinates is refused, never defaulted to 0,0 — which is a real place. */
bool geo_parse_fix(const char *body, size_t len, int32_t accuracy_default, geo_fix_t *out);

/* The request body a `wifi_scan` provider receives: the `cap` strongest of
 * `entries`, as `[{"bssid":"..","rssi":-58},...]`. An `ip` provider sends nothing, so
 * the caller passes `count` 0. Returns 0 when the body does not fit `buf`, so a
 * caller never posts a truncated document that parses as fewer APs than it saw.
 * Pure. */
size_t geo_scan_body(const char *const *bssids, const int32_t *rssi, size_t count,
                     char *buf, size_t cap);

/* True while a cached fix is still usable: `now - source_ts < ttl`, and false when
 * `now` is before `source_ts` (a clock that went backwards is not a reason to trust a
 * fix forever). Pure. */
bool geo_fix_is_fresh(int64_t source_ts, int64_t now, int64_t ttl_s);

/* The provider to try at `index` for a chain of `count`, skipping nothing: the chain
 * is already ordered and `04` §4.13 walks it as written. Exists so the board half does
 * not index the array itself and a bounds bug is a host-test failure. Pure. */
const geo_provider_t *geo_provider_at(const geo_provider_t *chain, size_t count,
                                      size_t index);

/* True when this provider kind is reached with `POST`, false for `GET`. A `wifi_scan`
 * entry is a POST of the scanned-AP list; an `ip` entry is a plain GET of a URL that
 * reads the caller's address off the connection. Sending an `ip` provider a POST is not
 * a wasted request but a refusal: ipwho.is answers 405 and ipapi.co closes the
 * connection, so the chain reports a failure that has nothing to do with the network.
 * Pure, so the verb is proven on the host and neither kind can drift. */
bool geo_kind_posts(geo_kind_t kind);
