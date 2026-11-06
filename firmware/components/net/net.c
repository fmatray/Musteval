/* net.c — WiFi station and the HTTPS transport `llm` posts through.
 * Board-only: it needs the radio, lwIP and mbedTLS. The ordering and backoff rules it
 * applies live in `net_select.c` and are covered on the host. */

#include "net.h"

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
#include "freertos/task.h"

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

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    if (id == WIFI_EVENT_STA_DISCONNECTED) {
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

esp_err_t net_connect(void)
{
    if (s_ap_count == 0) {
        s_state = NET_STATE_FAILED;
        s_attempting = false;
        return ESP_ERR_INVALID_STATE;
    }

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

    if (s_sta == NULL) {
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
        err = esp_wifi_start();
        if (err != ESP_OK) return err;
    }

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
            esp_wifi_disconnect();
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

void net_status(net_status_t *out)
{
    if (out == NULL) return;
    out->state = s_state;
    out->ssid = s_state == NET_STATE_CONNECTED ? s_ssid : NULL;
    out->rssi = s_state == NET_STATE_CONNECTED ? s_rssi : 0;
    out->ip = s_state == NET_STATE_CONNECTED ? s_ip : NULL;
    out->failovers = s_failovers;
}

esp_err_t net_post(const char *url, const char *content_type,
                   const char *authorization, const char *body, size_t body_len,
                   int32_t timeout_ms, net_response_t *resp)
{
    if (url == NULL || body == NULL || resp == NULL) return ESP_ERR_INVALID_ARG;
    if (!net_connected()) return ESP_ERR_INVALID_STATE;

    memset(resp, 0, sizeof *resp);

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = timeout_ms,
        /* The bundled roots are the whole trust decision (`06-security.md` T11). A
         * `http://` URL is the caller's explicit choice for a trusted LAN; this
         * function never downgrades an `https://` one. */
        .crt_bundle_attach = esp_crt_bundle_attach,
        .disable_auto_redirect = true,
        .buffer_size = 2048,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) return ESP_ERR_NO_MEM;

    esp_http_client_set_header(client, "Content-Type",
                               content_type != NULL ? content_type : "application/json");
    if (authorization != NULL && authorization[0] != '\0') {
        esp_http_client_set_header(client, "Authorization", authorization);
    }
    esp_http_client_set_post_field(client, body, (int)body_len);

    int64_t start = esp_timer_get_time() / 1000;
    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "http: %s: %s", url, esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return err;
    }

    resp->status = esp_http_client_get_status_code(client);
    int64_t declared = esp_http_client_get_content_length(client);
    /* A body larger than the declared cap is refused rather than truncated: a
     * truncated JSON body is an error the caller cannot tell from a bad provider. */
    if (declared > NET_BODY_MAX) {
        esp_http_client_cleanup(client);
        return ESP_ERR_INVALID_SIZE;
    }

    resp->body = malloc(NET_BODY_MAX + 1);
    if (resp->body == NULL) {
        esp_http_client_cleanup(client);
        return ESP_ERR_NO_MEM;
    }
    int total = 0;
    while (total < NET_BODY_MAX) {
        int got = esp_http_client_read(client, resp->body + total, NET_BODY_MAX - total);
        if (got <= 0) break;
        total += got;
    }
    /* A chunked response declares no length, so a body that exactly fills the cap is
     * indistinguishable from one that continues. One more read tells them apart, and
     * the answer must be a refusal: truncating here would hand `llm` a half-parsed
     * reply it would report as a malformed provider response. */
    if (total == NET_BODY_MAX) {
        char probe;
        if (esp_http_client_read(client, &probe, 1) > 0) {
            free(resp->body);
            resp->body = NULL;
            esp_http_client_cleanup(client);
            return ESP_ERR_INVALID_SIZE;
        }
    }
    resp->body[total] = '\0';
    resp->body_len = (size_t)total;
    resp->elapsed_ms = esp_timer_get_time() / 1000 - start;

    esp_http_client_cleanup(client);
    return ESP_OK;
}
