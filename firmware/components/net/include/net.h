/* net — WiFi station, DHCP, DNS and the mbedTLS transport.
 * Layering: L0 (`08-code-architecture.md` §1). The one L0 edge it uses is `net` →
 * `cfg`, to read `wifi.networks[]` (§1.1). It knows nothing about providers: an
 * HTTP POST is a transport, and what a body means belongs to `llm`.
 * Contracts: `02-firmware.md` §3.1 steps 5 and 7, `02-firmware.md` §4.2 (`E_WIFI`),
 * `03-config.md` §wifi, `04-protocol.md` §3.9 (`wifi`) and §3.11 (`wifi.connected`). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* `03-config.md` §wifi bounds, used when a key is absent. */
#define NET_DEFAULT_CONNECT_TIMEOUT_MS 15000
#define NET_MAX_NETWORKS 8
#define NET_MAX_BACKOFF 6

/* Largest response body the transport will hold. A provider reply that exceeds it is
 * refused, not truncated, so `llm` never parses half a document and reports it as a
 * malformed reply. */
#define NET_BODY_MAX 16384

typedef enum {
    NET_STATE_OFF = 0,  /* radio never started, or safe mode (`02` §3.2) */
    NET_STATE_CONNECTING,
    NET_STATE_CONNECTED,
    NET_STATE_FAILED,   /* every configured AP was tried and refused */
} net_state_t;

typedef struct {
    net_state_t state;
    const char *ssid;    /* borrowed; NULL unless connected */
    int32_t     rssi;    /* dBm, valid only when connected */
    const char *ip;      /* dotted quad, borrowed; NULL unless connected */
    int32_t     failovers; /* APs tried since boot */
} net_status_t;

/* Read `wifi.*` and prepare the station. Does not connect: the boot sequence calls
 * this at step 4 (with `cfg`) and `net_connect` at step 5, so a config error is
 * reported before the radio costs power. Returns ESP_ERR_NOT_FOUND when no config is
 * loaded and ESP_ERR_INVALID_ARG when `wifi.networks[]` is empty, which `03-config.md`
 * calls a config error. */
esp_err_t net_init(void);

/* Bring the station up and join the best visible AP from `wifi.networks[]`, highest
 * `priority` first, `retry_backoff_ms` between attempts, `connect_timeout_ms` per
 * attempt. Blocks for at most the sum of those bounds.
 *
 * Returns ESP_OK when an IP is assigned, `ESP_ERR_ESP_NETIF_*`-independent `E_WIFI` in
 * the caller's mapping when every AP refused. In safe mode the caller does not call
 * this at all (`02-firmware.md` §3.2). */
esp_err_t net_connect(void);

/* True once an IP is assigned and the interface is up. */
bool net_connected(void);

void net_status(net_status_t *out);

/* --- Transport (`llm`'s only way onto the wire) ---------------------------- */

typedef struct {
    int      status;        /* HTTP status, 0 when no response arrived */
    char    *body;          /* caller-owned, NUL-terminated */
    size_t   body_len;
    int64_t  elapsed_ms;
} net_response_t;

/* `POST <url>` with `content_type`, `authorization` (may be NULL) and `body`.
 * Follows no redirect. Verifies the certificate chain against the bundled roots and
 * never falls back to plaintext (`06-security.md` T11): a `https://` URL that fails
 * verification returns `ESP_ERR_ESP_TLS_CERTIFICATE_VERIFY_FAILED`.
 *
 * `timeout_ms` bounds the whole exchange, not each read. Returns ESP_ERR_TIMEOUT on
 * overrun, ESP_ERR_INVALID_STATE when the link is down, and a transport error
 * otherwise; only ESP_OK fills `resp`, whose `body` the caller frees. */
esp_err_t net_post(const char *url, const char *content_type,
                   const char *authorization, const char *body, size_t body_len,
                   int32_t timeout_ms, net_response_t *resp);

/* --- Pure selection rules (`net_select.c`) -------------------------------- */

typedef struct {
    const char *ssid;
    int32_t     priority; /* higher wins (`03-config.md` §wifi) */
} net_candidate_t;

/* Reorder `candidates` in place, highest priority first, stable for equal
 * priorities so the config's own order breaks ties. Pure, so the ordering rule is
 * proven on the host rather than inferred from a successful association. */
void net_rank(net_candidate_t *candidates, size_t count);

/* The backoff wait before attempt `attempt` (0-based), clamped to the list. Pure. */
int32_t net_backoff_ms(const int32_t *backoff, size_t count, size_t attempt);

/* `GET <url>` with the caller's headers. The same trust decision as `net_post`:
 * bundled roots, no redirect, no plaintext fallback. Every HTTP tool reaches the wire
 * through this one call, so the allowlist is the only thing deciding reachability and
 * `06-security.md` T11 holds for tools exactly as it does for `llm`.
 *
 * `header_names`/`header_values` are parallel arrays of `count` entries; either may
 * be NULL when `count` is 0. `body` receives up to `body_cap` bytes of the response
 * and is NUL-terminated; more than that is ESP_ERR_INVALID_SIZE rather than a
 * truncation (`tools/web.md`). Returns ESP_ERR_INVALID_STATE when the link is down.
 *
 * Board-only (`net.c`). */
esp_err_t net_get(const char *url, const char *const *header_names,
                  const char *const *header_values, size_t count,
                  int32_t timeout_ms, net_response_t *resp);

/* One request with a method of the caller's choosing, for declarative `POST` tools.
 * Same trust decision and same body cap as `net_get`. `body` may be NULL for a
 * request with no entity. Board-only. */
esp_err_t net_request(const char *method, const char *url,
                      const char *const *header_names,
                      const char *const *header_values, size_t count,
                      const char *body, size_t body_len, int32_t timeout_ms,
                      net_response_t *resp);

/* The wire word for a state, which `04-protocol.md` §3.9 fixes: `off`, `connecting`,
 * `connected`, `failed`. Pure, so `tools` and `status` cannot disagree about it. */
const char *net_state_name(net_state_t state);

/* --- Scanning (`04-protocol.md` §4.24) ------------------------------------ */

/* `NET_SCAN_MAX` is the §4.24 cap: a reader in a dense block gets what they can
 * reach, and `truncated` says the cap bit. */
#define NET_SCAN_MAX 20

typedef struct {
    char    ssid[33];
    int32_t rssi;
    uint8_t auth;  /* `wifi_auth_mode_t`, mapped to the wire word by `net_auth_name` */
} net_scan_entry_t;

/* Bring the radio up if needed and ask it for the access points in range. Blocks for
 * up to `wifi.connect_timeout_ms`, because a scan's dwell time is set by the radio.
 *
 * Works on a device with no `wifi.networks[]`: `/onboard` scans before one exists.
 * Returns ESP_ERR_INVALID_STATE when the radio cannot start, ESP_ERR_NO_MEM when the
 * driver's list cannot be held, ESP_ERR_TIMEOUT when the scan does not finish.
 *
 * `out` receives up to `cap` entries, strongest first. Board-only. */
esp_err_t net_scan(net_scan_entry_t *out, size_t cap, size_t *count, bool *truncated);

/* Condense a raw driver list into what §4.24 promises: drop a hidden or empty SSID,
 * collapse duplicate SSIDs to the strongest RSSI, sort strongest first, and stop at
 * `cap` setting `*truncated`. `entries` must be writable; `*count` is both in and
 * out. Pure, so the three rules are proven on the host rather than by noticing a
 * network missing from a screen. */
void net_scan_condense(net_scan_entry_t *entries, size_t *count, size_t cap,
                       bool *truncated);

/* The wire word for a `wifi_auth_mode_t`: `open`, `wep`, `wpa`, `wpa2`, `wpa3`,
 * `wpa2_enterprise`, `wpa3_enterprise`, `unknown` (§4.24). Pure. */
const char *net_auth_name(uint8_t auth);

/* --- Reachability (`04-protocol.md` §4.25) -------------------------------- */

/* The §4.25 probe shape: four echoes, 200 ms apart, each abandoned after 500 ms, so
 * `/ping router|llm|ntp` answers inside the 1 s `pong` promise of §3.7 only because
 * the caller runs it off the link task (`main.c`). */
#define NET_PING_COUNT 4
#define NET_PING_INTERVAL_MS 200
#define NET_PING_TIMEOUT_MS 500

/* `rtt_ms[i] < 0` means that probe timed out; `received` counts the rest. `loss_pct`
 * is rounded to nearest, so 1 of 3 lost is 33 and 2 of 3 is 67. Every field is 0
 * when `count` is 0, and the rtt fields are 0 when nothing came back. */
typedef struct {
    int32_t sent, received, loss_pct, rtt_min_ms, rtt_avg_ms, rtt_max_ms;
} net_ping_stats_t;

/* Pure, so the §4.25 arithmetic is proven on the host rather than inferred from a
 * ping that happened to be answered. */
void net_ping_summarise(const int32_t *rtt_ms, size_t count, net_ping_stats_t *out);

/* The default gateway as a dotted quad, which `/ping router` probes. Returns
 * ESP_ERR_INVALID_STATE when the link is down and ESP_ERR_INVALID_SIZE when `cap`
 * is below `IP4ADDR_STRLEN_MAX`. Board-only. */
esp_err_t net_gateway(char *out, size_t cap);

/* ICMP echo `count` times to `host`, resolving a name with `getaddrinfo` when it is
 * not already a dotted quad, and filling `rtt_ms[i]` with the round trip in ms or
 * `-1` for a timeout. Blocks for up to `count * (interval + timeout)`, so it must not
 * be called on the link task. Returns ESP_ERR_INVALID_STATE when the link is down,
 * ESP_ERR_INVALID_ARG for a NULL `rtt_ms` or an empty host, ESP_ERR_NOT_FOUND when
 * resolution fails, ESP_FAIL when the probe could not start. Total loss is a result,
 * not an error. Board-only. */
esp_err_t net_ping_icmp(const char *host, int32_t *rtt_ms, size_t count);
