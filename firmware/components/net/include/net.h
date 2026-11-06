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
