/* mqtt.c — the board half of `mqtt`: one `esp_mqtt_client` per configured broker, the
 * inbound frame, and the publish path (`02-firmware.md` §mqtt, `04-protocol.md` §3.18).
 * Board-only, like `llm.c`: it opens TLS connections and reads NVS, so its defects are
 * found by reading and every rule that can be pure lives in `mqtt_parse.c`. */

#include "mqtt.h"

#include <stdio.h>
#include <string.h>

#include "cfg.h"
#include "clock.h"
#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "mqtt_client.h"
#include "store.h"

static const char *TAG = "mqtt";

typedef struct {
    const mqtt_server_t *server;
    esp_mqtt_client_handle_t client;
    bool connected;
    bool started;
} mqtt_runtime_t;

static mqtt_runtime_t s_servers[MQTT_MAX_SERVERS];
static size_t s_count;
static mqtt_sink_fn s_sink;
static mqtt_server_t s_config[MQTT_MAX_SERVERS];

void mqtt_set_sink(mqtt_sink_fn sink) { s_sink = sink; }

/* The allowlist is indexed by server id, which is what both the command and the frame
 * carry. A linear scan over at most eight entries is cheaper than a map. */
static mqtt_runtime_t *find_runtime(const char *server_id)
{
    if (server_id == NULL) return NULL;
    for (size_t i = 0; i < s_count; i++) {
        if (strcmp(s_servers[i].server->id, server_id) == 0) return &s_servers[i];
    }
    return NULL;
}

/* `rt` comes from the handler's own argument: `esp_mqtt_event_t` carries no
 * user context, and `esp_mqtt_client_handle_t` is an opaque pointer, so the runtime
 * has to be threaded through the one place that already has it. */
static void on_message(mqtt_runtime_t *rt, esp_mqtt_event_handle_t event)
{
    if (rt == NULL || event->topic == NULL) return;

    /* §3.18: an inbound message on a topic no configured entry covers is never
     * delivered. The client subscribes only to the allowlist, but a broker may send
     * anything it likes on that subscription, so the check is repeated here. */
    const char *topic = event->topic;
    bool covered = false;
    for (size_t i = 0; i < rt->server->topic_count; i++) {
        if (cfg_mqtt_filter_covers(rt->server->topics[i].filter, topic)) {
            covered = true;
            break;
        }
    }
    if (!covered) {
        ESP_LOGW(TAG, "%s: ignored message on an unlisted topic", rt->server->id);
        return;
    }

    size_t len = event->data_len > 0 ? (size_t)event->data_len : 0;
    bool truncated = mqtt_payload_truncates(len);
    size_t send = truncated ? MQTT_PAYLOAD_MAX : len;

    if (s_sink != NULL) {
        s_sink(rt->server->id, topic, event->data != NULL ? event->data : "", send,
               truncated, (uint8_t)(event->qos == 1 ? 1 : 0), event->retain != 0,
               (int64_t)clock_now());
    }

    /* A message the reader was told about is a message a task may match. The trigger is
     * exact-topic (`02-firmware.md` §5.4), so the task layer applies its own grammar. */
}

static void mqtt_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)base;
    mqtt_runtime_t *rt = arg;
    esp_mqtt_event_handle_t event = data;
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        rt->connected = true;
        ESP_LOGI(TAG, "%s: connected", rt->server->id);
        /* Subscriptions are declared in config and never chosen by the model
         * (`00-overview.md` §4.2). `*` is not an MQTT wildcard, so it becomes the MQTT
         * one-level wildcard the broker understands. */
        for (size_t i = 0; i < rt->server->topic_count; i++) {
            char wire[MQTT_FILTER_MAX + 1];
            snprintf(wire, sizeof wire, "%s", rt->server->topics[i].filter);
            size_t n = strlen(wire);
            if (n > 0 && wire[n - 1] == '*') wire[n - 1] = '+';
            esp_mqtt_client_subscribe(rt->client, wire, rt->server->topics[i].qos);
        }
        break;
    case MQTT_EVENT_DISCONNECTED:
        rt->connected = false;
        ESP_LOGW(TAG, "%s: disconnected", rt->server->id);
        break;
    case MQTT_EVENT_DATA:
        on_message(rt, event);
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGW(TAG, "%s: transport error", rt->server->id);
        break;
    default:
        break;
    }
}

/* Parse one `mqtt.servers[]` entry. `03-config.md` §mqtt, closed schema. */
static bool parse_server(json_node_t *entry, mqtt_server_t *out)
{
    memset(out, 0, sizeof *out);
    const char *id = json_string(json_get(entry, "id"));
    const char *host = json_string(json_get(entry, "host"));
    if (id == NULL || host == NULL) return false;
    snprintf(out->id, sizeof out->id, "%s", id);
    snprintf(out->host, sizeof out->host, "%s", host);

    bool tls = true;
    json_node_t *t = json_get(entry, "tls");
    if (t != NULL && !json_bool(t, &tls)) return false;
    out->tls = tls;

    int64_t v = 0;
    uint16_t port = 0;
    if (json_int(json_get(entry, "port"), &v)) {
        if (v < 1 || v > 65535) return false;
        port = (uint16_t)v;
    }
    out->port = mqtt_effective_port(tls, port);

    int64_t qos = 0;
    if (json_int(json_get(entry, "qos"), &qos)) {
        if (!mqtt_qos_ok(qos)) return false;
        out->qos = (uint8_t)qos;
    }

    int64_t keepalive = 60;
    if (json_int(json_get(entry, "keepalive_s"), &keepalive)) {
        if (keepalive < 10 || keepalive > 300) return false;
    }
    out->keepalive_s = (int32_t)keepalive;

    const char *client_id = json_string(json_get(entry, "client_id"));
    if (client_id != NULL) {
        snprintf(out->client_id, sizeof out->client_id, "%s", client_id);
    } else {
        /* `03-config.md` §mqtt's default id. */
        json_node_t *name = cfg_get("device.name");
        const char *device = name != NULL ? json_string(name) : NULL;
        if (mqtt_derive_client_id(device, out->id, out->client_id,
                                  sizeof out->client_id) == 0) {
            return false;
        }
    }

    const char *username = json_string(json_get(entry, "username"));
    if (username != NULL) snprintf(out->username, sizeof out->username, "%s", username);
    const char *ref = json_string(json_get(entry, "password_ref"));
    if (ref != NULL) snprintf(out->password_ref, sizeof out->password_ref, "%s", ref);

    json_node_t *topics = json_get(entry, "topics");
    size_t n = topics == NULL ? 0 : json_array_len(topics);
    if (n > MQTT_TOPICS_MAX) return false;
    for (size_t i = 0; i < n; i++) {
        json_node_t *e = json_array_at(topics, i);
        const char *filter = json_string(json_get(e, "filter"));
        if (filter == NULL || !cfg_mqtt_filter_is_valid(filter)) return false;
        snprintf(out->topics[i].filter, sizeof out->topics[i].filter, "%s", filter);
        int64_t q = 0;
        if (json_int(json_get(e, "qos"), &q)) {
            if (!mqtt_qos_ok(q)) return false;
            out->topics[i].qos = (uint8_t)q;
        }
        out->topic_count = i + 1;
    }
    return true;
}

static esp_err_t start_server(mqtt_runtime_t *rt, const mqtt_server_t *server)
{
    char uri[192];
    snprintf(uri, sizeof uri, "%s://%s:%u", server->tls ? "mqtts" : "mqtt", server->host,
             (unsigned)server->port);

    /* `06-security.md` T11: a TLS broker is verified against the bundle and never falls
     * back to plaintext. An unverified `mqtts` connection is worse than a refused one,
     * because the payload looks delivered. */
    char password[CFG_SECRET_MAX + 1] = "";
    if (server->password_ref[0] != '\0') {
        esp_err_t err = cfg_resolve_secret(server->password_ref, password,
                                           sizeof password, NULL);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "%s: %s not resolved: %s", server->id, server->password_ref,
                     esp_err_to_name(err));
            return err;
        }
    }

    esp_mqtt_client_config_t config = {
        .broker.address.uri = uri,
        .credentials.client_id = server->client_id,
        .credentials.username = server->username[0] != '\0' ? server->username : NULL,
        .credentials.authentication.password = password[0] != '\0' ? password : NULL,
        .session.keepalive = (int)server->keepalive_s,
        .session.disable_clean_session = false,
        .network.disable_auto_reconnect = false,
    };
    if (server->tls) config.broker.verification.crt_bundle_attach = esp_crt_bundle_attach;

    rt->client = esp_mqtt_client_init(&config);
    /* The password is copied into the client's own storage by `esp_mqtt_client_init`;
     * the local buffer is wiped so a later stack dump is not a credential dump. */
    memset(password, 0, sizeof password);
    if (rt->client == NULL) return ESP_ERR_NO_MEM;

    rt->server = server;
    esp_mqtt_client_register_event(rt->client, ESP_EVENT_ANY_ID, mqtt_event_handler, rt);
    esp_err_t err = esp_mqtt_client_start(rt->client);
    if (err != ESP_OK) {
        esp_mqtt_client_destroy(rt->client);
        rt->client = NULL;
        return err;
    }
    rt->started = true;
    return ESP_OK;
}

esp_err_t mqtt_init(void)
{
    s_count = 0;
    memset(s_servers, 0, sizeof s_servers);

    json_node_t *mqtt = cfg_get("mqtt");
    if (mqtt == NULL) return ESP_OK;

    json_node_t *list = json_get(mqtt, "servers");
    size_t n = list == NULL ? 0 : json_array_len(list);
    int64_t max_servers = MQTT_MAX_SERVERS;
    json_int(json_get(mqtt, "max_servers"), &max_servers);
    if ((int64_t)n > max_servers) return ESP_ERR_INVALID_ARG;
    if (n > MQTT_MAX_SERVERS) return ESP_ERR_INVALID_ARG;

    for (size_t i = 0; i < n; i++) {
        mqtt_server_t *parsed = &s_config[i];
        if (!parse_server(json_array_at(list, i), parsed)) {
            ESP_LOGE(TAG, "server %u: configuration refused", (unsigned)i);
            continue;
        }
        mqtt_runtime_t *rt = &s_servers[s_count];
        /* A broker that will not start is isolated: its tools are absent, the run
         * continues (`03-config.md` §mqtt). */
        if (start_server(rt, parsed) != ESP_OK) {
            ESP_LOGW(TAG, "%s: not started; its tools are absent", parsed->id);
            continue;
        }
        s_count++;
    }
    ESP_LOGI(TAG, "%u of %u brokers started", (unsigned)s_count, (unsigned)n);
    return ESP_OK;
}

size_t mqtt_states(mqtt_state_t *out, size_t cap)
{
    if (out == NULL) return 0;
    size_t n = s_count < cap ? s_count : cap;
    for (size_t i = 0; i < n; i++) {
        out[i].id = s_servers[i].server->id;
        out[i].connected = s_servers[i].connected;
        out[i].topics = s_servers[i].server->topics;
        out[i].topic_count = s_servers[i].server->topic_count;
    }
    return n;
}

esp_err_t mqtt_publish(const char *server_id, const char *topic, const char *payload,
                       size_t len, uint8_t qos)
{
    if (topic == NULL || payload == NULL) return ESP_ERR_INVALID_ARG;
    if (!mqtt_qos_ok(qos)) return ESP_ERR_INVALID_ARG;
    /* `docs/tools/mqtt.md`: the payload is the message body as given. The cap is the
     * tool result's, so a caller can always be told what it sent. */
    if (len > MQTT_PUBLISH_MAX) return ESP_ERR_INVALID_ARG;

    mqtt_runtime_t *rt = find_runtime(server_id);
    if (rt == NULL) return ESP_ERR_NOT_FOUND;
    /* The allowlist is the whole authorisation story (`00-overview.md` §4.2): a topic
     * no configured entry covers is refused even though the broker would accept it. */
    if (!mqtt_topics_cover(rt->server->topics, rt->server->topic_count, topic)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!rt->connected) return ESP_ERR_INVALID_STATE;

    int id = esp_mqtt_client_publish(rt->client, topic, payload, (int)len, qos, 0);
    /* The client answers with the message id, or -1 when it could not queue the
     * message at all. */
    return id < 0 ? ESP_ERR_INVALID_STATE : ESP_OK;
}
