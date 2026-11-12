/* net.c — WiFi station and the HTTPS transport `llm` posts through.
 * Board-only: it needs the radio, lwIP and mbedTLS. The ordering and backoff rules it
 * applies live in `net_select.c` and are covered on the host. */

#include "net.h"
#include "net_internal.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_netif.h"
#include "esp_tls.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "lwip/inet.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "ping/ping_sock.h"

#include "cfg.h"

static const char *TAG = "net";

#define NET_GOT_IP_BIT BIT0
#define NET_FAIL_BIT   BIT1

typedef struct {
    char    ssid[33];      /* copied: a `reload` frees the config document */
    char    password[65];
    int32_t priority;
} net_ap_t;

static EventGroupHandle_t s_events;
static esp_netif_t *s_sta;
static net_ap_t s_aps[NET_MAX_NETWORKS];
static size_t s_ap_count;
static int32_t s_backoff[NET_MAX_BACKOFF];
static size_t s_backoff_count;
static int32_t s_connect_timeout_ms = NET_DEFAULT_CONNECT_TIMEOUT_MS;
static int32_t s_failovers;

static net_state_t s_state = NET_STATE_OFF;
static char s_ip[16];
static char s_ssid[33];
static int32_t s_rssi;
/* True while `net_connect`'s attempt loop is driving the radio. The disconnect
 * handler must not reconnect then: the loop is trying the next access point, and a
 * connect from the event task would race it. */
static bool s_attempting;
/* Set once the station has held a link. `02-firmware.md` §4.2's `WiFi Lost` row is
 * about an *established* link dropping; a boot in which every network refused is a
 * config problem and must stay `failed` rather than spin `connecting` forever. */
static bool s_ever_connected;
/* Set around the `esp_wifi_disconnect` that `net_connect` issues before each attempt.
 * The driver answers it with `WIFI_EVENT_STA_DISCONNECTED`, and that event used to set
 * `NET_FAIL_BIT` like any other drop: it woke the wait in `net_connect` immediately, so
 * every attempt was scored "no association (timeout 15000 ms)" in a few milliseconds,
 * before the radio had a chance to associate. The reason code distinguishes the two
 * cases, so the handler can tell a deliberate leave from a real drop. */
static bool s_leaving;

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    if (id == WIFI_EVENT_STA_DISCONNECTED) {
        /* An intentional leave is not an attempt that failed. `net_connect` issues one
         * before configuring each access point, and scoring it as a failure is what
         * made every network look refused within a few milliseconds. The reason code
         * is the reliable signal: the event can be dispatched after the call that
         * caused it has returned, so a flag set around it races the handler. */
        const wifi_event_sta_disconnected_t *gone = data;
        int reason = gone != NULL ? gone->reason : NET_WIFI_REASON_UNSPECIFIED;
        if (net_disconnect_ignored(s_leaving, s_attempting, reason)) {
            ESP_LOGD(TAG, "wifi: ignoring disconnect, reason %d", reason);
            return;
        }
        /* A drop must downgrade the state, not just raise the flag: otherwise
         * `net_connected()` keeps answering true and `status` reports the SSID and IP
         * of a link that is gone (`04-protocol.md` §3.9 types them as the *active*
         * network). The identity is cleared for the same reason — a stale IP is worse
         * than none, because it invites a request that cannot be delivered. */
        s_rssi = 0;
        s_ip[0] = '\0';
        s_ssid[0] = '\0';
        if (s_events != NULL) {
            xEventGroupClearBits(s_events, NET_GOT_IP_BIT);
            xEventGroupSetBits(s_events, NET_FAIL_BIT);
        }
        if (!s_attempting && s_ever_connected) {
            /* `02-firmware.md` §4.2 `WiFi Lost`: `net` attempts reconnect. It is the
             * attempt loop's absence, not the previous state, that says whether this
             * handler owns the retry — a reconnect that itself fails must try again,
             * and a `was_live` test would stop after one.
             * shortcut: the retry is immediate and unbounded, so an AP that stays gone
             * is retried at the driver's own rate. Upgrade to the `retry_backoff_ms`
             * schedule if a site ever shows power cost from the retries. */
            s_state = NET_STATE_CONNECTING;
            ESP_LOGW(TAG, "wifi: link lost, reconnecting");
            esp_wifi_connect();
        }
    } else if (id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    }
}

static void on_ip_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    (void)id;
    if (data == NULL) return;
    ip_event_got_ip_t *got = (ip_event_got_ip_t *)data;
    snprintf(s_ip, sizeof s_ip, IPSTR, IP2STR(&got->ip_info.ip));
    /* The state is set here, not only in `net_connect`'s success path: a reconnect
     * after a drop arrives through this handler alone, and a state left at CONNECTING
     * would make `net_connected()` answer false forever on a link that is up. */
    s_state = NET_STATE_CONNECTED;
    s_ever_connected = true;
    s_ssid[0] = '\0';
    wifi_ap_record_t record;
    if (esp_wifi_sta_get_ap_info(&record) == ESP_OK) {
        snprintf(s_ssid, sizeof s_ssid, "%s", (const char *)record.ssid);
        s_rssi = record.rssi;
    }
    if (s_events != NULL) {
        xEventGroupClearBits(s_events, NET_FAIL_BIT);
        xEventGroupSetBits(s_events, NET_GOT_IP_BIT);
    }
}

esp_err_t net_init(void)
{
    s_ap_count = 0;
    s_failovers = 0;
    s_state = NET_STATE_OFF;
    /* Reset before reading: `reload` calls this again, and a timeout left over from
     * the previous config would apply to a document that no longer sets it. */
    s_connect_timeout_ms = NET_DEFAULT_CONNECT_TIMEOUT_MS;

    json_node_t *wifi = cfg_get("wifi");
    if (wifi == NULL) {
        return ESP_ERR_NOT_FOUND;
    }

    json_node_t *networks = json_get(wifi, "networks");
    size_t n = json_array_len(networks);
    if (n == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    /* Rank before copying so the copy is already in try order. `net_rank` is pure and
     * takes the candidate view, so build that view over the array indices first. */
    net_candidate_t order[NET_MAX_NETWORKS];
    size_t order_count = 0;
    for (size_t i = 0; i < n && order_count < NET_MAX_NETWORKS; i++) {
        json_node_t *entry = json_array_at(networks, i);
        const char *ssid = json_string(json_get(entry, "ssid"));
        if (ssid == NULL || ssid[0] == '\0') continue;
        int64_t priority = 0;
        json_int(json_get(entry, "priority"), &priority);
        order[order_count].ssid = ssid;
        order[order_count].priority = (int32_t)priority;
        order_count++;
    }
    net_rank(order, order_count);

    for (size_t i = 0; i < order_count; i++) {
        /* Resolve by ssid, which is unique enough for a hotspot list and is what the
         * rank array kept. A duplicate ssid ranks twice; the second attempt simply
         * fails, which is the same outcome as listing it once. */
        for (size_t j = 0; j < n; j++) {
            json_node_t *entry = json_array_at(networks, j);
            const char *ssid = json_string(json_get(entry, "ssid"));
            if (ssid == NULL || strcmp(ssid, order[i].ssid) != 0) continue;

            net_ap_t *ap = &s_aps[s_ap_count];
            snprintf(ap->ssid, sizeof ap->ssid, "%s", ssid);
            ap->priority = order[i].priority;
            ap->password[0] = '\0';
            const char *ref = json_string(json_get(entry, "password_ref"));
            size_t got = 0;
            if (ref != NULL) {
                esp_err_t err = cfg_resolve_secret(ref, ap->password,
                                                   sizeof ap->password, &got);
                if (err != ESP_OK) {
                    /* A network whose password does not resolve cannot be joined;
                     * skipping it with a named log beats an authentication failure
                     * that looks like a wrong password. */
                    ESP_LOGW(TAG, "wifi: %s: %s not resolved: %s", ap->ssid, ref,
                             esp_err_to_name(err));
                    break;
                }
            }
            s_ap_count++;
            break;
        }
    }

    int64_t configured = 0;
    if (json_int(json_get(wifi, "connect_timeout_ms"), &configured) && configured > 0) {
        s_connect_timeout_ms = (int32_t)configured;
    }
    s_backoff_count = 0;
    json_node_t *backoff = json_get(wifi, "retry_backoff_ms");
    for (size_t i = 0; i < json_array_len(backoff) && s_backoff_count < NET_MAX_BACKOFF; i++) {
        int64_t value = 0;
        if (json_int(json_array_at(backoff, i), &value)) {
            s_backoff[s_backoff_count++] = (int32_t)value;
        }
    }

    /* `03-config.md` §wifi defaults the backoff when the key is absent, so an empty
     * list here is the default list, not "no wait". */
    if (s_backoff_count == 0) {
        static const int32_t defaults[] = {1000, 2000, 5000, 10000};
        for (size_t i = 0; i < sizeof defaults / sizeof defaults[0]; i++) {
            s_backoff[s_backoff_count++] = defaults[i];
        }
    }

    if (s_ap_count == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}

/* Bring the station interface up, once. Returns ESP_ERR_INVALID_STATE when the radio
 * is not available, which is what `net_scan` reports as `E_WIFI`.
 *
 * Split out of `net_connect` because a scan has to work on a device that has no
 * `wifi.networks[]` yet — that is exactly the device `/onboard` is pointed at, and
 * `net_init` refuses an empty list before the radio is touched (`03-config.md`
 * §wifi). Scanning is how the wizard learns an SSID, so the radio must come up
 * before any SSID is configured. */
static esp_err_t radio_up(void)
{
    if (s_events == NULL) {
        s_events = xEventGroupCreate();
        if (s_events == NULL) return ESP_ERR_NO_MEM;
    }

    esp_err_t err = esp_netif_init();
    if (err != ESP_OK) return err;
    err = esp_event_loop_create_default();
    /* Already created by another subsystem is the normal case on a second boot path,
     * not a failure. */
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return err;

    if (s_sta != NULL) return ESP_OK;

    s_sta = esp_netif_create_default_wifi_sta();
    if (s_sta == NULL) return ESP_ERR_NO_MEM;

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&init);
    if (err != ESP_OK) return err;

    err = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               &on_wifi_event, NULL, NULL);
    if (err != ESP_OK) return err;
    err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               &on_ip_event, NULL, NULL);
    if (err != ESP_OK) return err;
    err = esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (err != ESP_OK) return err;
    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK) return err;
    return esp_wifi_start();
}

esp_err_t net_connect(void)
{
    if (s_ap_count == 0) {
        s_state = NET_STATE_FAILED;
        s_attempting = false;
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = radio_up();
    if (err != ESP_OK) return err;

    s_state = NET_STATE_CONNECTING;
    s_attempting = true;
    for (size_t attempt = 0; attempt < s_ap_count; attempt++) {
        net_ap_t *ap = &s_aps[attempt];
        xEventGroupClearBits(s_events, NET_GOT_IP_BIT | NET_FAIL_BIT);

        wifi_config_t config = {0};
        /* `memcpy` into the fixed SSID/PSK fields, not `snprintf`: the driver's fields
         * are exactly 32 and 64 bytes with no terminator, and both values were bounded
         * when they were copied in, so the compiler's truncation warning is about the
         * absent NUL rather than a real overrun. */
        memcpy(config.sta.ssid, ap->ssid, strnlen(ap->ssid, sizeof config.sta.ssid));
        memcpy(config.sta.password, ap->password,
               strnlen(ap->password, sizeof config.sta.password));
        config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

        err = esp_wifi_set_config(WIFI_IF_STA, &config);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "wifi: %s: set_config failed: %s", ap->ssid,
                     esp_err_to_name(err));
        } else {
            s_leaving = true;
            esp_wifi_disconnect();
            s_leaving = false;
            esp_wifi_connect();
        }

        EventBits_t bits = xEventGroupWaitBits(
            s_events, NET_GOT_IP_BIT | NET_FAIL_BIT, pdFALSE, pdFALSE,
            pdMS_TO_TICKS(s_connect_timeout_ms));
        if (bits & NET_GOT_IP_BIT) {
            wifi_ap_record_t record;
            s_rssi = 0;
            if (esp_wifi_sta_get_ap_info(&record) == ESP_OK) {
                s_rssi = record.rssi;
            }
            snprintf(s_ssid, sizeof s_ssid, "%s", ap->ssid);
            s_state = NET_STATE_CONNECTED;
            s_attempting = false;
            s_failovers = (int32_t)attempt;
            ESP_LOGI(TAG, "wifi: connected to %s, rssi %" PRId32 ", ip %s",
                     s_ssid, s_rssi, s_ip);
            return ESP_OK;
        }

        s_ip[0] = '\0';
        s_ssid[0] = '\0';
        /* One AP refused. Back off before the next, by the documented schedule. */
        int32_t wait_ms = net_backoff_ms(s_backoff, s_backoff_count, attempt);
        ESP_LOGW(TAG, "wifi: %s: no association (timeout %" PRId32 " ms), backoff %"
                 PRId32 " ms", ap->ssid, s_connect_timeout_ms, wait_ms);
        vTaskDelay(pdMS_TO_TICKS(wait_ms));
    }

    s_attempting = false;
    s_state = NET_STATE_FAILED;
    s_failovers = (int32_t)s_ap_count;
    ESP_LOGE(TAG, "wifi: every configured network refused (E_WIFI)");
    return ESP_ERR_INVALID_STATE;
}

bool net_connected(void)
{
    return s_state == NET_STATE_CONNECTED;
}

esp_err_t net_scan(net_scan_entry_t *out, size_t cap, size_t *count, bool *truncated)
{
    if (out == NULL || count == NULL || cap == 0) return ESP_ERR_INVALID_ARG;
    *count = 0;
    if (truncated != NULL) *truncated = false;

    esp_err_t err = radio_up();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "scan: radio unavailable: %s", esp_err_to_name(err));
        return ESP_ERR_INVALID_STATE;
    }

    /* `block: true` keeps this synchronous, which is what a command handler needs:
     * the reply carries the networks or it carries an error, never neither. */
    wifi_scan_config_t config = {0};
    err = esp_wifi_scan_start(&config, true);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "scan: start failed: %s", esp_err_to_name(err));
        return (err == ESP_ERR_WIFI_TIMEOUT) ? ESP_ERR_TIMEOUT : ESP_ERR_INVALID_STATE;
    }

    uint16_t found = 0;
    err = esp_wifi_scan_get_ap_num(&found);
    if (err != ESP_OK) return ESP_ERR_INVALID_STATE;
    if (found == 0) {
        esp_wifi_clear_ap_list();
        return ESP_OK;
    }
    if (found > NET_SCAN_MAX) found = NET_SCAN_MAX;

    /* The driver's records are ~100 bytes each; on the agent task's stack a 20-entry
     * array would be 2 KiB, so it is a static. Dispatch is single-threaded
     * (`02-firmware.md` §4.16) and a scan is not `E_BUSY`-safe against anything else,
     * so nothing else is in this buffer. */
    static wifi_ap_record_t s_records[NET_SCAN_MAX];
    uint16_t got = found;
    err = esp_wifi_scan_get_ap_records(&got, s_records);
    /* Free the driver's copy whether or not it was read: a scan that is not cleared
     * leaks the list until the next one. */
    esp_wifi_clear_ap_list();
    if (err != ESP_OK) return ESP_ERR_INVALID_STATE;

    size_t n = 0;
    for (uint16_t i = 0; i < got && n < cap; i++) {
        /* The driver's `ssid` is 32 bytes with no guarantee of a terminator, so the
         * copy is bounded by both fields. */
        size_t len = strnlen((const char *)s_records[i].ssid,
                             sizeof s_records[i].ssid);
        if (len >= sizeof out[n].ssid) len = sizeof out[n].ssid - 1;
        memcpy(out[n].ssid, s_records[i].ssid, len);
        out[n].ssid[len] = '\0';
        out[n].rssi = s_records[i].rssi;
        out[n].auth = s_records[i].authmode;
        n++;
    }
    *count = n;
    net_scan_condense(out, count, cap, truncated);
    ESP_LOGI(TAG, "scan: %u in range, %u reported", (unsigned)got, (unsigned)*count);
    return ESP_OK;
}

void net_status(net_status_t *out)
{
    if (out == NULL) return;
    out->state = s_state;
    out->ssid = s_state == NET_STATE_CONNECTED ? s_ssid : NULL;
    out->rssi = s_state == NET_STATE_CONNECTED ? s_rssi : 0;
    out->ip = s_state == NET_STATE_CONNECTED ? s_ip : NULL;
    out->failovers = s_failovers;
}

/* `esp_http_client_perform` owns the body read and drops it on the way out (it
 * resets `buffer->raw_len`), so the body is only ever available through the event
 * handler. Reading with `esp_http_client_read` after `perform` returns 0 for every
 * response, including a 200: that is why a caller asking for the body used to get an
 * empty string and the caller here has to collect it as it streams. */
typedef struct {
    char *out;
    size_t cap;
    size_t len;
    bool overflow;
} net_capture_t;

static esp_err_t net_capture_event(esp_http_client_event_t *evt)
{
    net_capture_t *cap = evt->user_data;
    if (evt->event_id != HTTP_EVENT_ON_DATA || cap == NULL) return ESP_OK;
    if (evt->data_len <= 0) return ESP_OK;
    size_t room = cap->cap - cap->len;
    if ((size_t)evt->data_len > room) {
        /* Refused, not truncated: a half body is a JSON error the caller cannot tell
         * from a provider's own bad reply. */
        cap->overflow = true;
        return ESP_OK;
    }
    memcpy(cap->out + cap->len, evt->data, (size_t)evt->data_len);
    cap->len += (size_t)evt->data_len;
    return ESP_OK;
}

/* One exchange, shared by every caller that reaches the wire. The method, the extra
 * headers and whether there is an entity are the only things that differ, so TLS
 * policy, the body cap and the redirect rule are stated once
 * (`06-security.md` T11). */
static esp_err_t net_exchange(esp_http_client_method_t method, const char *url,
                              const char *content_type, const char *authorization,
                              const char *const *header_names,
                              const char *const *header_values, size_t header_count,
                              const char *body, size_t body_len, int32_t timeout_ms,
                              net_response_t *resp)
{
    if (url == NULL || resp == NULL) return ESP_ERR_INVALID_ARG;
    if (!net_connected()) return ESP_ERR_INVALID_STATE;

    memset(resp, 0, sizeof *resp);

    esp_http_client_config_t config = {
        .url = url,
        .method = method,
        .timeout_ms = timeout_ms,
        /* The bundled roots are the whole trust decision (`06-security.md` T11). A
         * `http://` URL is the caller's explicit choice for a trusted LAN; this
         * function never downgrades an `https://` one. */
        .crt_bundle_attach = esp_crt_bundle_attach,
        .disable_auto_redirect = true,
        .buffer_size = 2048,
        .event_handler = net_capture_event,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) return ESP_ERR_NO_MEM;

    for (size_t i = 0; i < header_count; i++) {
        if (header_names[i] != NULL && header_values[i] != NULL) {
            esp_http_client_set_header(client, header_names[i], header_values[i]);
        }
    }
    if (content_type != NULL) {
        esp_http_client_set_header(client, "Content-Type", content_type);
    }
    if (authorization != NULL && authorization[0] != '\0') {
        esp_http_client_set_header(client, "Authorization", authorization);
    }
    if (body != NULL) {
        esp_http_client_set_post_field(client, body, (int)body_len);
    }

    resp->body = malloc(NET_BODY_MAX + 1);
    if (resp->body == NULL) {
        esp_http_client_cleanup(client);
        return ESP_ERR_NO_MEM;
    }
    net_capture_t capture = { .out = resp->body, .cap = NET_BODY_MAX };
    esp_http_client_set_user_data(client, &capture);

    int64_t start = esp_timer_get_time() / 1000;
    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "http: %s: %s", url, esp_err_to_name(err));
        free(resp->body);
        resp->body = NULL;
        esp_http_client_cleanup(client);
        return err;
    }

    resp->status = esp_http_client_get_status_code(client);
    int64_t declared = esp_http_client_get_content_length(client);
    if (declared > NET_BODY_MAX || capture.overflow) {
        /* The cap is a refusal, not a truncation: a half body is an error the caller
         * cannot tell from a provider's own bad reply. */
        free(resp->body);
        resp->body = NULL;
        esp_http_client_cleanup(client);
        return ESP_ERR_INVALID_SIZE;
    }

    resp->body[capture.len] = '\0';
    resp->body_len = capture.len;
    resp->elapsed_ms = esp_timer_get_time() / 1000 - start;

    esp_http_client_cleanup(client);
    return ESP_OK;
}

esp_err_t net_post(const char *url, const char *content_type,
                   const char *authorization, const char *body, size_t body_len,
                   int32_t timeout_ms, net_response_t *resp)
{
    if (body == NULL) return ESP_ERR_INVALID_ARG;
    return net_exchange(HTTP_METHOD_POST, url, content_type, authorization, NULL, NULL,
                        0, body, body_len, timeout_ms, resp);
}

esp_err_t net_get(const char *url, const char *const *header_names,
                  const char *const *header_values, size_t count,
                  int32_t timeout_ms, net_response_t *resp)
{
    return net_exchange(HTTP_METHOD_GET, url, NULL, NULL, header_names, header_values,
                        count, NULL, 0, timeout_ms, resp);
}

esp_err_t net_request(const char *method, const char *url,
                      const char *const *header_names,
                      const char *const *header_values, size_t count,
                      const char *body, size_t body_len, int32_t timeout_ms,
                      net_response_t *resp)
{
    if (method == NULL) return ESP_ERR_INVALID_ARG;
    esp_http_client_method_t verb = HTTP_METHOD_GET;
    if (strcmp(method, "POST") == 0) {
        verb = HTTP_METHOD_POST;
    } else if (strcmp(method, "GET") != 0) {
        /* `tools/web.md` allows exactly GET and POST; anything else is the file's
         * own validation failure, refused rather than mapped to a nearest verb. */
        return ESP_ERR_NOT_SUPPORTED;
    }
    return net_exchange(verb, url, body != NULL ? "application/json" : NULL, NULL,
                        header_names, header_values, count, body, body_len, timeout_ms,
                        resp);
}

/* --- Reachability (`04-protocol.md` §4.25) -------------------------------- */

typedef struct {
    int32_t *rtt_ms;
    size_t   count;
    SemaphoreHandle_t done;
} net_ping_ctx_t;

static void net_ping_on_success(esp_ping_handle_t hdl, void *args)
{
    net_ping_ctx_t *ctx = args;
    uint16_t seqno = 0;
    uint32_t elapsed = 0;
    if (esp_ping_get_profile(hdl, ESP_PING_PROF_SEQNO, &seqno, sizeof seqno) != ESP_OK) return;
    if (esp_ping_get_profile(hdl, ESP_PING_PROF_TIMEGAP, &elapsed, sizeof elapsed) != ESP_OK) return;
    if (seqno >= 1 && (size_t)seqno <= ctx->count) {
        ctx->rtt_ms[seqno - 1] = (int32_t)elapsed;
    }
}

static void net_ping_on_end(esp_ping_handle_t hdl, void *args)
{
    (void)hdl;
    net_ping_ctx_t *ctx = args;
    xSemaphoreGive(ctx->done);
}

esp_err_t net_gateway(char *out, size_t cap)
{
    if (out == NULL) return ESP_ERR_INVALID_ARG;
    if (cap < IP4ADDR_STRLEN_MAX) return ESP_ERR_INVALID_SIZE;
    if (!net_connected() || s_sta == NULL) return ESP_ERR_INVALID_STATE;

    esp_netif_ip_info_t info;
    esp_err_t err = esp_netif_get_ip_info(s_sta, &info);
    if (err != ESP_OK) return ESP_ERR_INVALID_STATE;

    ip4_addr_t gw;
    gw.addr = info.gw.addr;
    if (ip4addr_ntoa_r(&gw, out, (int)cap) == NULL) return ESP_ERR_INVALID_SIZE;

    return ESP_OK;
}

esp_err_t net_ping_icmp(const char *host, int32_t *rtt_ms, size_t count)
{
    if (host == NULL || host[0] == '\0' || rtt_ms == NULL || count == 0) return ESP_ERR_INVALID_ARG;
    if (!net_connected()) return ESP_ERR_INVALID_STATE;

    ip_addr_t target;
    ip4_addr_t quad;
    if (ip4addr_aton(host, &quad)) {
        ip_addr_copy_from_ip4(target, quad);
    } else {
        struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM };
        struct addrinfo *res = NULL;
        if (getaddrinfo(host, NULL, &hints, &res) != 0 || res == NULL) return ESP_ERR_NOT_FOUND;
        const struct sockaddr_in *sin = (const struct sockaddr_in *)res->ai_addr;
        /* `ip_addr_set_ip4_u32` guards its argument with `if (ipaddr)`, which a
         * stack address makes constant-true, and GCC 14 refuses that under
         * `-Werror=address`. Write the v4 member directly instead. */
        target.type = IPADDR_TYPE_V4;
        ip4_addr_set_u32(ip_2_ip4(&target), sin->sin_addr.s_addr);
        freeaddrinfo(res);
    }

    for (size_t i = 0; i < count; i++) rtt_ms[i] = -1;

    net_ping_ctx_t ctx = { .rtt_ms = rtt_ms, .count = count, .done = xSemaphoreCreateBinary() };
    if (ctx.done == NULL) return ESP_FAIL;

    esp_ping_config_t config = ESP_PING_DEFAULT_CONFIG();
    config.count = (uint32_t)count;
    config.interval_ms = NET_PING_INTERVAL_MS;
    config.timeout_ms = NET_PING_TIMEOUT_MS;
    config.data_size = 32;
    config.task_prio = 2;
    config.target_addr = target;

    const esp_ping_callbacks_t cbs = {
        .cb_args = &ctx,
        .on_ping_success = net_ping_on_success,
        .on_ping_timeout = NULL,
        .on_ping_end = net_ping_on_end,
    };

    esp_ping_handle_t hdl = NULL;
    esp_err_t err = esp_ping_new_session(&config, &cbs, &hdl);
    if (err != ESP_OK) {
        vSemaphoreDelete(ctx.done);
        return ESP_FAIL;
    }

    err = esp_ping_start(hdl);
    if (err != ESP_OK) {
        esp_ping_delete_session(hdl);
        vSemaphoreDelete(ctx.done);
        return ESP_FAIL;
    }

    uint32_t timeout_ms = (uint32_t)count * (NET_PING_INTERVAL_MS + NET_PING_TIMEOUT_MS) + 1000;
    if (xSemaphoreTake(ctx.done, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
        err = ESP_FAIL;
    } else {
        err = ESP_OK;
    }

    esp_ping_stop(hdl);
    esp_ping_delete_session(hdl);
    vSemaphoreDelete(ctx.done);
    return err;
}
