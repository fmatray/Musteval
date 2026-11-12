#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Private to the component: `net.h` is the frozen transport contract and does not
 * carry this rule. */

/* Should a `WIFI_EVENT_STA_DISCONNECTED` be ignored rather than scored as a refused
 * attempt? `leaving` is the caller's own `esp_wifi_disconnect`, `attempting` says an
 * AP is still being tried, and `reason` is the driver's code. Pure, because the
 * board-only version of this rule made `net_connect` abandon an AP in milliseconds
 * and log "timeout 15000 ms", which is only observable on hardware. */
bool net_disconnect_ignored(bool leaving, bool attempting, int reason);

/* The driver's reason codes this predicate needs, named here rather than taken from
 * `esp_wifi_types_generic.h`: that header is not on the include path of this pure
 * translation unit, and a `#ifndef` fallback silently compiled a different value (0)
 * than the driver's (1), so the rule tested false on the board and true in the host
 * suite. Values are ESP-IDF's own, and each one below was observed on target. */
#define NET_WIFI_REASON_UNSPECIFIED 1
#define NET_WIFI_REASON_ASSOC_LEAVE 8
/* The driver reports the station leaving the AP with either code depending on how far
 * the association got; both are its own `esp_wifi_disconnect`, never a refusal. */
#define NET_WIFI_REASON_STA_LEAVING 36
