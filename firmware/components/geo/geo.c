/* geo.c — the board half of `geo`: config, the WiFi scan, the provider chain and the
 * cache (`02-firmware.md` §geo). Board-only, like `llm.c`: it opens sockets and reads
 * NVS, so its defects are found by reading, and every rule that can be pure lives in
 * `geo_parse.c` where a host test can fail it. */

#include "geo.h"

#include <stdio.h>
#include <string.h>

#include "cfg.h"
#include "clock.h"
#include "esp_log.h"
#include "mbedtls/platform_util.h"
#include "net.h"
#include "store.h"

static const char *TAG = "geo";

#define GEO_BODY_MAX 4096
#define GEO_BSSIDS_CAP 32
/* `net_scan` caps at 20 SSIDs; a scan also yields several BSSIDs per SSID in a mesh,
 * so the working set is the protocol cap, not the SSID cap. */
#define GEO_SCAN_MAX NET_SCAN_MAX

typedef struct {
    bool          enabled;
    int64_t       cache_ttl_s;
    int64_t       timeout_ms;
    int32_t       max_bssids;
    size_t        count;
    geo_provider_t providers[GEO_MAX_PROVIDERS];
} geo_state_t;

static geo_state_t s_state;
static char s_last_error[GEO_ERR_MAX];
/* The scanned APs as the provider sees them. Two parallel arrays because the body
 * builder takes them that way; both are the protocol's own cap. */
static char    s_bssid[GEO_BSSIDS_CAP][18];
static int32_t s_rssi[GEO_BSSIDS_CAP];

static bool      s_have_fix;
static geo_fix_t s_fix;

bool geo_cached_fix(geo_fix_t *out)
{
    if (out == NULL || !s_have_fix) return false;
    *out = s_fix;
    return true;
}

esp_err_t geo_init(void)
{
    memset(&s_state, 0, sizeof s_state);
    s_have_fix = false;
    /* A reason belongs to the chain that produced it. A `reload` installs a new
     * chain, so the old reason describes providers that are no longer configured. */
    s_last_error[0] = '\0';
    s_state.cache_ttl_s = GEO_CACHE_TTL_S_DEFAULT;
    s_state.timeout_ms = GEO_TIMEOUT_MS_DEFAULT;
    s_state.max_bssids = GEO_MAX_BSSIDS_DEFAULT;

    json_node_t *geo = cfg_get("geo");
    if (geo == NULL) return ESP_OK; /* no `geo` section: the tool is absent, not broken */

    json_node_t *on = json_get(geo, "enabled");
    bool enabled = false;
    if (on != NULL) {
        if (!json_bool(on, &enabled)) return ESP_ERR_INVALID_ARG;
    }
    s_state.enabled = enabled;
    if (!s_state.enabled) return ESP_OK;

    int64_t v = 0;
    if (json_int(json_get(geo, "cache_ttl_s"), &v)) s_state.cache_ttl_s = v;
    if (json_int(json_get(geo, "timeout_ms"), &v)) s_state.timeout_ms = v;
    if (json_int(json_get(geo, "max_bssids"), &v)) s_state.max_bssids = (int32_t)v;
    if (s_state.max_bssids > GEO_BSSIDS_CAP) s_state.max_bssids = GEO_BSSIDS_CAP;

    json_node_t *chain = json_get(geo, "providers");
    size_t n = chain == NULL ? 0 : json_array_len(chain);
    /* `03-config.md` §geo: 1..4 when enabled. An enabled geolocator with no chain is a
     * config error the validator also catches; refused here too because `tools` can be
     * built against a config the validator never saw. */
    if (n == 0 || n > GEO_MAX_PROVIDERS) return ESP_ERR_INVALID_ARG;

    for (size_t i = 0; i < n; i++) {
        json_node_t *entry = json_array_at(chain, i);
        geo_provider_t *p = &s_state.providers[i];
        const char *id = json_string(json_get(entry, "id"));
        const char *kind = json_string(json_get(entry, "kind"));
        const char *url = json_string(json_get(entry, "url"));
        const char *ref = json_string(json_get(entry, "api_key_ref"));
        if (id == NULL || kind == NULL || url == NULL) return ESP_ERR_INVALID_ARG;

        snprintf(p->id, sizeof p->id, "%s", id);
        snprintf(p->url, sizeof p->url, "%s", url);
        if (strcmp(kind, "wifi_scan") == 0) {
            p->kind = GEO_KIND_WIFI;
            /* The credential is the provider's identity: without it the lookup is a
             * 401, so it is a boot refusal (`docs/tools/geo.md` §Errors). */
            if (ref == NULL) {
                ESP_LOGE(TAG, "geo: %s: wifi_scan requires api_key_ref", p->id);
                return ESP_ERR_INVALID_ARG;
            }
        } else if (strcmp(kind, "ip") == 0) {
            p->kind = GEO_KIND_IP;
            if (ref != NULL) {
                ESP_LOGE(TAG, "geo: %s: ip must not carry api_key_ref", p->id);
                return ESP_ERR_INVALID_ARG;
            }
        } else {
            return ESP_ERR_INVALID_ARG;
        }
        if (ref != NULL) snprintf(p->api_key_ref, sizeof p->api_key_ref, "%s", ref);
        s_state.count = i + 1;
    }
    return ESP_OK;
}

bool geo_enabled(void) { return s_state.enabled; }

/* One provider attempt. `key` is the resolved secret, empty for `ip`. A provider that
 * overruns `timeout_ms` is a failure like any other, so the chain continues. The verb
 * comes from `geo_kind_posts`: an `ip` entry is a GET of a URL that reads the caller's
 * address off the connection, and a POST to one is refused by the provider itself. */
static esp_err_t try_provider(const geo_provider_t *p, const char *key, const char *body,
                              size_t body_len, geo_fix_t *out, int32_t *status_out)
{
    net_response_t resp = {0};
    esp_err_t err;
    if (geo_kind_posts(p->kind)) {
        char auth[CFG_SECRET_MAX + 16];
        snprintf(auth, sizeof auth, "Bearer %s", key);
        err = net_post(p->url, "application/json", auth, body, body_len,
                       (int32_t)s_state.timeout_ms, &resp);
    } else {
        err = net_get(p->url, NULL, NULL, 0, (int32_t)s_state.timeout_ms, &resp);
    }
    if (status_out != NULL) *status_out = resp.status;
    if (err != ESP_OK) {
        if (resp.body != NULL) free(resp.body);
        return err;
    }
    bool parsed = resp.body != NULL && geo_parse_fix(resp.body, resp.body_len,
                                                     p->kind == GEO_KIND_IP ? 5000 : 100,
                                                     out);
    if (resp.body != NULL) free(resp.body);
    return parsed ? ESP_OK : ESP_ERR_INVALID_RESPONSE;
}

esp_err_t geo_locate(geo_fix_t *out)
{
    if (out == NULL) return ESP_ERR_INVALID_ARG;
    if (!s_state.enabled) return ESP_ERR_INVALID_STATE;

    int64_t now = (int64_t)clock_now();
    if (s_have_fix && geo_fix_is_fresh(s_fix.source_ts, now, s_state.cache_ttl_s)) {
        *out = s_fix;
        return ESP_OK;
    }

    /* Step 2: scan only when some entry can use the result. A chain of `ip` providers
     * has nothing to send a BSSID list to, and an `esp_wifi_scan_start` in station mode
     * disturbs the link the very next request needs: the scan made the TLS connect that
     * followed it fail with `ESP_ERR_HTTP_CONNECT` on a board whose link was healthy.
     * `GEO_SCAN_MAX` entries are 1.4 KiB of this stack, so an `ip`-only chain also
     * stops paying for an array it cannot read. */
    bool need_scan = geo_chain_needs_scan(s_state.providers, s_state.count);

    size_t count = 0;
    size_t usable = 0;
    if (need_scan) {
        net_scan_entry_t entries[GEO_SCAN_MAX];
        bool truncated = false;
        esp_err_t err = net_scan(entries, GEO_SCAN_MAX, &count, &truncated);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "locate: scan failed: %s", esp_err_to_name(err));
            count = 0; /* an `ip` entry later in the chain is still worth trying */
        } else {
            net_scan_condense(entries, &count, GEO_SCAN_MAX, &truncated);
        }

        for (size_t i = 0; i < count && usable < (size_t)s_state.max_bssids; i++) {
            /* A positioning provider wants the radio's address, not the network's
             * name. `net_scan` already formatted it from the driver record. */
            if (entries[i].bssid[0] == '\0') continue;
            snprintf(s_bssid[usable], sizeof s_bssid[usable], "%s", entries[i].bssid);
            s_rssi[usable] = entries[i].rssi;
            usable++;
        }
    }

    static char s_body[GEO_BODY_MAX];
    size_t body_len = 0;
    if (usable > 0) {
        const char *names[GEO_BSSIDS_CAP];
        for (size_t i = 0; i < usable; i++) names[i] = s_bssid[i];
        body_len = geo_scan_body(names, s_rssi, usable, s_body, sizeof s_body);
        if (body_len == 0) usable = 0;
    }

    /* Steps 3-4: walk the chain as written. The first fix wins; a failure is logged
     * with its provider id and the next entry is tried. */
    esp_err_t last = ESP_ERR_NOT_FOUND;
    for (size_t i = 0; i < s_state.count; i++) {
        const geo_provider_t *p = geo_provider_at(s_state.providers, s_state.count, i);
        if (p == NULL) continue;
        /* A `wifi_scan` provider with nothing scanned cannot answer; skipping it costs
         * one request that is certain to fail. */
        if (p->kind == GEO_KIND_WIFI && usable == 0) continue;

        char key[CFG_SECRET_MAX + 1] = "";
        if (p->kind == GEO_KIND_WIFI) {
            esp_err_t r = cfg_resolve_secret(p->api_key_ref, key, sizeof key, NULL);
            if (r != ESP_OK) {
                ESP_LOGW(TAG, "locate: %s: %s not resolved", p->id, p->api_key_ref);
                last = r;
                continue;
            }
        }

        geo_fix_t candidate = {0};
        int32_t status = 0;
        esp_err_t r = try_provider(p, key, body_len > 0 ? s_body : "{}",
                                   body_len > 0 ? body_len : 2, &candidate, &status);
        /* The key is out of scope after the attempt; wipe it so a stack dump of a
         * failed lookup is not a credential dump (`06-security.md` §11). */
        mbedtls_platform_zeroize(key, sizeof key);
        if (r == ESP_OK) {
            snprintf(candidate.provider, sizeof candidate.provider, "%s", p->id);
            candidate.source_ts = (int64_t)clock_now();
            s_fix = candidate;
            s_have_fix = true;
            s_last_error[0] = '\0';
            *out = candidate;
            return ESP_OK;
        }
        /* Kept for the command surface as well as the log: `geo_locate`'s return code
         * cannot carry the difference between a DNS miss and a refused handshake, and
         * those need different repairs. The last failed provider is the one worth
         * naming - the chain stopped there. */
        snprintf(s_last_error, sizeof s_last_error, "%s: %s (http %d)", p->id,
                 esp_err_to_name(r), (int)status);
        ESP_LOGW(TAG, "locate: %s", s_last_error);
        last = r;
    }

    /* Step 5 is the cache write, which only happens on success: a failed lookup caches
     * nothing, so the next call retries the whole chain.
     *
     * `tools/geo.md` §Errors: `E_TIMEOUT` is the *last* attempt timing out, not any
     * attempt. A chain whose first provider timed out and whose second refused is a
     * refusal — reporting a timeout there would tell the reader to retry a lookup that
     * will refuse again. */
    if (last == ESP_ERR_TIMEOUT) return ESP_ERR_TIMEOUT;
    return last == ESP_OK ? ESP_ERR_NOT_FOUND : last;
}

const char *geo_last_error(void)
{
    return s_last_error;
}


