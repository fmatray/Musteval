/* clock — SNTP client, time sync and timezone offset.
 * Layering: L0 (`08-code-architecture.md` §1). May call `net`, `cfg` and `store`
 * (§1.1's five enumerated L0 edges). It is L0 rather than a capability because every
 * layer needs the time: `llm` and `geo` for window buckets, `sched` for due times,
 * `agent` for frame ordering. As a capability it would need a port; as a platform
 * service it needs none.
 * Contracts: `02-firmware.md` §3.1 step 6, `03-config.md` §ntp, `04-protocol.md`
 * §3.9 (`time`), §4.13 (`time.now` is an M4 tool, but its source of truth is here). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* `03-config.md` §ntp defaults, used when the key is absent. */
#define CLOCK_DEFAULT_SYNC_INTERVAL_S 3600
#define CLOCK_DEFAULT_SYNC_TIMEOUT_MS 3000
#define CLOCK_MAX_SERVERS 4

/* Read `ntp.*` and apply `ntp.timezone` with `setenv("TZ", ...)` followed by
 * `tzset()`. Must run before `clock_sync`. Returns ESP_ERR_NOT_FOUND when no config
 * is loaded, ESP_ERR_INVALID_ARG when `ntp.servers` is empty, in which case the
 * documented default server is used and the caller is told. */
esp_err_t clock_init(void);

/* Start SNTP and block until the first sync or a timeout. Called at boot step 6, in
 * safe mode this is skipped entirely (`02-firmware.md` §3.2).
 *
 * Returns ESP_OK on a successful sync, ESP_ERR_TIMEOUT when no server answered
 * within `ntp.sync_timeout_ms`, and ESP_ERR_INVALID_STATE when the radio is down:
 * a caller that skips the network check should still get a code that says why no
 * time arrived rather than a generic failure. */
esp_err_t clock_sync(void);

/* True once a sync has succeeded and the system clock is past the 2020 floor. The
 * floor is what separates a real sync from `esp_timer`'s boot epoch, because TLS
 * verification depends on it (`06-security.md` T11) and an unset clock verifies
 * nothing. */
bool clock_synced(void);

/* Unix seconds. Before a sync this is `esp_timer`-relative plus the build epoch, so
 * callers that need a trustworthy instant must ask `clock_synced()` first. */
int64_t clock_now(void);

/* Milliseconds since boot, monotonic. Never depends on the sync. */
int64_t clock_uptime_ms(void);

/* The configured POSIX TZ string, or "UTC" when none is set. Borrowed; stable for
 * the life of the process. */
const char *clock_tz(void);

/* Milliseconds to add for local time, derived from the TZ string by asking `mktime`
 * for the current instant. Used by `time.now`'s local field (`03-config.md` §ntp). */
int32_t clock_utc_offset_s(void);

/* The first configured SNTP server, borrowed and stable for the life of the process,
 * or NULL before `clock_init` populated the list (`04-protocol.md` §4.25, `net.ping`
 * target `ntp`). */
const char *clock_server_name(void);

/* Pure: how long to wait before the next re-sync, clamped so a nonsensical
 * `sync_interval_s` cannot disable the timer. Separated from the SNTP call so the
 * host suite can prove the clamp without a radio. */
int64_t clock_next_interval_ms(int32_t configured_s);
