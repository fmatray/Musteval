/* mqtt — independent broker connections with a config-declared allowlist.
 * Layering: L1 (`08-code-architecture.md` §1), above `cfg` and `store`.
 * Contracts: `02-firmware.md` §mqtt, `03-config.md` §mqtt, `04-protocol.md`
 * §3.18, `docs/tools/mqtt.md`. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* `03-config.md` §mqtt bounds. */
#define MQTT_MAX_SERVERS 8
#define MQTT_ID_MAX 16
#define MQTT_HOST_MAX 128
#define MQTT_CLIENT_ID_MAX 64
#define MQTT_TOPICS_MAX 8
#define MQTT_FILTER_MAX 128
/* §3.18: the frame's `payload` is capped, and `truncated` says the cap bit. */
#define MQTT_PAYLOAD_MAX 1024
/* `docs/tools/mqtt.md`: `payload` is the whole message body, and no bound is stated
 * in the tool contract. It is bounded here by the result cap rather than by the 16 KiB
 * link frame, so a tool reply can always carry what was sent. */
#define MQTT_PUBLISH_MAX 2048

typedef struct {
    char    filter[MQTT_FILTER_MAX + 1];
    uint8_t qos;
} mqtt_topic_t;

typedef struct {
    char        id[MQTT_ID_MAX + 1];
    char        host[MQTT_HOST_MAX + 1];
    uint16_t    port;
    bool        tls;
    char        client_id[MQTT_CLIENT_ID_MAX + 1];
    char        username[MQTT_CLIENT_ID_MAX + 1];
    char        password_ref[16];
    int32_t     keepalive_s;
    uint8_t     qos; /* default publish QoS */
    size_t      topic_count;
    mqtt_topic_t topics[MQTT_TOPICS_MAX];
} mqtt_server_t;

/* The sink `main` installs so `mqtt` can emit §3.18 without depending on `link`
 * (`08-code-architecture.md` §1.1 has no `mqtt`→`link` edge). */
typedef void (*mqtt_sink_fn)(const char *server_id, const char *topic, const char *payload,
                             size_t len, bool truncated, uint8_t qos, bool retain,
                             int64_t received_ts);

/* Read `mqtt.*`, validate every server and start one client each. A server that fails
 * to start is skipped with a named log and its tools are absent; the others keep
 * running (`03-config.md` §mqtt: "a failing server is isolated"). */
esp_err_t mqtt_init(void);

void mqtt_set_sink(mqtt_sink_fn sink);

/* Per-server state for `04-protocol.md` §3.9's `status.mqtt[]`, §4.15's
 * `mqtt.status` and `mqtt.list`. The subscription list is the configured
 * `topics[].filter` set: `§4.15` reports what was subscribed to, not what the broker
 * acknowledged, because a reconnect re-subscribes and a refusal would be invisible. */
typedef struct {
    const char *id;
    bool        connected;
    const mqtt_topic_t *topics;
    size_t      topic_count;
} mqtt_state_t;

/* Fill up to `cap` entries; returns the number written. */
size_t mqtt_states(mqtt_state_t *out, size_t cap);

/* Publish `payload` to `topic` on `server_id`, refused unless a configured
 * `topics[]` entry of that server covers `topic` and the topic is a legal publish
 * destination. `qos` 0 or 1. Returns ESP_ERR_NOT_FOUND when the server id is not
 * configured, ESP_ERR_INVALID_ARG when the allowlist does not cover the topic or the
 * arguments are out of bounds, ESP_ERR_INVALID_STATE when that server is down.
 * Board-only. */
esp_err_t mqtt_publish(const char *server_id, const char *topic, const char *payload,
                       size_t len, uint8_t qos);

/* --- Pure rules (`mqtt_parse.c`) ----------------------------------------- */

/* True when any entry of `topics` covers `topic`. The allowlist question the publish
 * path and the inbound path both ask; the grammar itself is `cfg`'s
 * (`cfg_mqtt_filter_covers`), so the validator and the runtime cannot disagree. Pure. */
bool mqtt_topics_cover(const mqtt_topic_t *topics, size_t count, const char *topic);

/* The wire `qos` from a config value: 0 or 1 accepted, anything else refused. Pure. */
bool mqtt_qos_ok(int64_t qos);

/* The effective port: the configured one, or TLS 8883 / plaintext 1883. Pure. */
uint16_t mqtt_effective_port(bool tls, uint16_t configured);

/* The client id to use: the configured one, or `musteval-<device>-<id>` truncated to
 * the 64-byte ceiling. Returns 0 when the derived id does not fit `buf`. Pure. */
size_t mqtt_derive_client_id(const char *device, const char *server_id, char *buf,
                             size_t cap);

/* True when an inbound payload exceeded `MQTT_PAYLOAD_MAX`, so the frame's
 * `truncated` is set rather than the payload being silently short. Pure. */
bool mqtt_payload_truncates(size_t len);
