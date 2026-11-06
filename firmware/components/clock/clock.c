/* clock.c — SNTP client against the ESP-IDF lwIP module, plus the timezone apply.
 * Board-only: it needs a radio, the system clock and `setenv`. The arithmetic it
 * relies on lives in `clock_pure.c` and is covered on the host. */

#include "clock.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "esp_log.h"
#include "esp_sntp.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "cfg.h"

static const char *TAG = "clock";

/* A sync in 2020 or later is a real one: `esp_timer`'s boot epoch starts near zero,
 * and TLS verification against a 1970 clock accepts nothing (`06-security.md` T11). */
#define CLOCK_PLAUSIBLE_UNIX 1577836800LL /* 2020-01-01T00:00:00Z */

static char s_servers[CLOCK_MAX_SERVERS][64];
static size_t s_server_count;
/* The POSIX spelling, so `status`'s `time.tz` is the same string `tzset` was given
 * even when `clock_init` never ran because there is no `ntp` block. */
static char s_tz[64] = "UTC0";
static bool s_synced;
static bool s_started;
static int32_t s_interval_s = CLOCK_DEFAULT_SYNC_INTERVAL_S;
static volatile bool s_notified;

static void on_sync(struct timeval *tv)
{
    (void)tv;
    s_notified = true;
}

esp_err_t clock_init(void)
{
    s_server_count = 0;
    s_synced = false;
    s_notified = false;

    json_node_t *ntp = cfg_get("ntp");
    if (ntp == NULL) {
        return ESP_ERR_NOT_FOUND;
    }

    json_node_t *servers = json_get(ntp, "servers");
    size_t n = json_array_len(servers);
    for (size_t i = 0; i < n && s_server_count < CLOCK_MAX_SERVERS; i++) {
        const char *host = json_string(json_array_at(servers, i));
        if (host == NULL || host[0] == '\0') continue;
        /* Copied: `cfg_get` returns a borrowed pointer and a `reload` frees the
         * document under us, so SNTP must not hold it. */
        snprintf(s_servers[s_server_count], sizeof s_servers[0], "%s", host);
        s_server_count++;
    }

    const char *tz = json_string(json_get(ntp, "timezone"));
    /* `03-config.md` §ntp defaults to the POSIX string `UTC0`, not `UTC`: `UTC` is a
     * zone name the C library may not know, while `UTC0` is always a valid POSIX TZ. */
    snprintf(s_tz, sizeof s_tz, "%s", (tz != NULL && tz[0] != '\0') ? tz : "UTC0");
    /* The TZ string is applied once here rather than per call: `setenv` is not
     * thread-safe, and every `localtime_r` afterwards depends on it. */
    setenv("TZ", s_tz, 1);
    tzset();

    int32_t interval = CLOCK_DEFAULT_SYNC_INTERVAL_S;
    int64_t value = 0;
    if (json_int(json_get(ntp, "sync_interval_s"), &value)) {
        interval = (int32_t)value;
    }
    s_interval_s = interval;

    if (s_server_count == 0) {
        /* Every server was blank. One documented default beats no time at all, and
         * the caller is told so it can report the config as degraded. */
        snprintf(s_servers[0], sizeof s_servers[0], "pool.ntp.org");
        s_server_count = 1;
        return ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}

esp_err_t clock_sync(void)
{
    if (s_server_count == 0) {
        /* `clock_init` was never called or found no config: there is no server to
         * ask, which is not a timeout. */
        return ESP_ERR_INVALID_STATE;
    }

    if (!s_started) {
        esp_sntp_setoperatingmode(ESP_SNTP_OPMODE_POLL);
        for (size_t i = 0; i < s_server_count; i++) {
            esp_sntp_setservername((uint8_t)i, s_servers[i]);
        }
        esp_sntp_set_time_sync_notification_cb(on_sync);
        esp_sntp_set_sync_mode(SNTP_SYNC_MODE_IMMED);
        /* `ntp.sync_interval_s` is a documented resync period (`03-config.md` §ntp),
         * and SNTP takes it in milliseconds. Without this the library polls once and
         * the clock free-runs at the crystal's drift for the rest of the uptime. */
        esp_sntp_set_sync_interval((uint32_t)clock_next_interval_ms(s_interval_s));
        esp_sntp_init();
        s_started = true;
    }

    int64_t timeout_ms = CLOCK_DEFAULT_SYNC_TIMEOUT_MS;
    json_node_t *ntp = cfg_get("ntp");
    int64_t configured = 0;
    if (json_int(json_get(ntp, "sync_timeout_ms"), &configured) && configured > 0) {
        timeout_ms = configured;
    }

    /* Poll rather than subscribe to a notification: the callback sets a flag, and a
     * bounded wait keeps a dead radio from blocking the link indefinitely. The wait
     * is in 100 ms slices so it does not depend on FreeRTOS tick granularity. */
    const int64_t deadline = clock_uptime_ms() + timeout_ms;
    while (clock_uptime_ms() < deadline) {
        struct timeval now;
        gettimeofday(&now, NULL);
        if ((int64_t)now.tv_sec >= CLOCK_PLAUSIBLE_UNIX) {
            s_synced = true;
            ESP_LOGI(TAG, "ntp: synced, unix %" PRId64, (int64_t)now.tv_sec);
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    ESP_LOGW(TAG, "ntp: no reply from %u server(s) in %" PRId64 " ms",
             (unsigned)s_server_count, timeout_ms);
    return ESP_ERR_TIMEOUT;
}

bool clock_synced(void)
{
    if (s_synced) return true;
    struct timeval now;
    gettimeofday(&now, NULL);
    s_synced = (int64_t)now.tv_sec >= CLOCK_PLAUSIBLE_UNIX;
    return s_synced;
}

int64_t clock_now(void)
{
    struct timeval now;
    gettimeofday(&now, NULL);
    return (int64_t)now.tv_sec;
}

int64_t clock_uptime_ms(void)
{
    /* `esp_timer`, not `gettimeofday`: the wall clock is stepped by the SNTP sync,
     * so a difference of two wall readings would jump by the correction. The uptime
     * must stay monotonic across the sync. */
    return esp_timer_get_time() / 1000;
}

const char *clock_tz(void)
{
    return s_tz;
}

int32_t clock_utc_offset_s(void)
{
    /* Asking `localtime_r` and `gmtime_r` for the same instant is the only way to
     * honour an arbitrary POSIX TZ string, DST rules included, without parsing one. */
    time_t now = (time_t)clock_now();
    struct tm local;
    struct tm utc;
    if (localtime_r(&now, &local) == NULL || gmtime_r(&now, &utc) == NULL) {
        return 0;
    }
    int64_t local_s = (int64_t)local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec;
    int64_t utc_s = (int64_t)utc.tm_hour * 3600 + utc.tm_min * 60 + utc.tm_sec;
    int64_t delta = local_s - utc_s;
    /* A day boundary: local is behind UTC by up to 12 h, ahead by up to 14 h. */
    if (delta > 12 * 3600) delta -= 86400;
    if (delta < -12 * 3600) delta += 86400;
    return (int32_t)delta;
}
