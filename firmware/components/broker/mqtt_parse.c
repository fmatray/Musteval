/* mqtt_parse.c — the pure half of `mqtt`: the allowlist question, the port and client-id
 * defaults, and the payload cap (`03-config.md` §mqtt, `04-protocol.md` §3.18,
 * `docs/tools/mqtt.md`). No client, no socket, so every rule here is host-testable. */

#include "mqtt.h"

#include <stdio.h>
#include <string.h>

#include "cfg.h"

bool mqtt_topics_cover(const mqtt_topic_t *topics, size_t count, const char *topic)
{
    if (topics == NULL || topic == NULL) return false;
    for (size_t i = 0; i < count; i++) {
        if (cfg_mqtt_filter_covers(topics[i].filter, topic)) return true;
    }
    return false;
}

bool mqtt_qos_ok(int64_t qos)
{
    /* QoS 2 needs a four-way handshake the client is not configured for, and
     * `03-config.md` §mqtt says it is permanently unsupported. */
    return qos == 0 || qos == 1;
}

uint16_t mqtt_effective_port(bool tls, uint16_t configured)
{
    if (configured != 0) return configured;
    return tls ? 8883 : 1883;
}

size_t mqtt_derive_client_id(const char *device, const char *server_id, char *buf,
                             size_t cap)
{
    if (buf == NULL || cap == 0 || server_id == NULL) return 0;
    int n = snprintf(buf, cap, "musteval-%s-%s", device != NULL ? device : "device",
                     server_id);
    if (n <= 0 || (size_t)n >= cap) {
        buf[0] = '\0';
        return 0;
    }
    return (size_t)n;
}

bool mqtt_payload_truncates(size_t len) { return len > MQTT_PAYLOAD_MAX; }

size_t mqtt_chunk_take(size_t have, size_t len)
{
    if (have >= MQTT_PAYLOAD_MAX) return 0;
    size_t room = MQTT_PAYLOAD_MAX - have;
    return len < room ? len : room;
}

bool mqtt_chunk_completes(size_t offset, size_t len, size_t total)
{
    if (total == 0) return true;
    return offset + len >= total;
}
