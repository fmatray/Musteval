#pragma once

#include <stdint.h>

/* Private to the components that own a caller's cancel path; `net.h` is the frozen
 * transport contract and carries neither of these. */

/* Parse a `Retry-After` header value into seconds. Bare digits only: an HTTP-date
 * form, a list or any trailing junk is -1, which the caller reads as "no delay
 * named". A value beyond `INT32_MAX` saturates, because only the ceiling comparison
 * downstream matters. Pure, so the wire rule is host-tested. */
int32_t net_retry_after_seconds(const char *value);

/* Cancel the exchange `net_*` is blocked in, if any, so a `cancel` reaches a request
 * already on the wire. Safe with no exchange in flight and from another task. The
 * aborted call returns `ESP_ERR_NOT_FINISHED`. */
void net_abort_inflight(void);

/* Should a `WIFI_EVENT_STA_DISCONNECTED` be ignored rather than scored as a refused
 * attempt? `leaving` is the caller's own `esp_wifi_disconnect`, `attempting` says an
 * AP is still being tried, and `reason` is the driver's code. Pure, because the
 * board-only version of this rule made `net_connect` abandon an AP in milliseconds
 * and log "timeout 15000 ms", which is only observable on hardware. */
bool net_disconnect_ignored(bool leaving, bool attempting, int reason);

/* The driver's reason codes this predicate needs, named here rather than taken from
 * `esp_wifi_types_generic.h`: that header is not on the include path of this pure
 * translation unit, and a `#ifndef` fallback silently compiled a *different* value
 * (0) than the driver's (1), so the rule tested false on the board and true in the
 * host suite. Values are ESP-IDF's own, and each one below was observed on target. */
#define NET_WIFI_REASON_UNSPECIFIED 1
#define NET_WIFI_REASON_ASSOC_LEAVE 8
/* The driver reports the station leaving the AP with either code depending on how far
 * the association got; both are its own `esp_wifi_disconnect`, never a refusal. */
#define NET_WIFI_REASON_STA_LEAVING 36
