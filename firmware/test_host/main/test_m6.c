/* M6: the pure halves of `geo` and `mqtt`, and the `task.set` cap. Each module keeps
 * its board half out of the host build (`02-firmware.md` §geo, §mqtt), so what is
 * asserted here is the logic those halves call. */

#include <stdint.h>
#include <string.h>

#include "unity.h"

#include "store.h"
#include "cfg.h"
#include "geo.h"
#include "mqtt.h"
#include "tools.h"

/* --- Provider responses --------------------------------------------------- */

TEST_CASE("a flat provider response yields a fix", "[geo]")
{
    const char *body = "{\"lat\":52.3676,\"lon\":4.9041,\"accuracy\":65}";
    geo_fix_t fix = {0};
    TEST_ASSERT_TRUE(geo_parse_fix(body, strlen(body), 100, &fix));
    TEST_ASSERT_EQUAL_DOUBLE(52.3676, fix.lat);
    TEST_ASSERT_EQUAL_DOUBLE(4.9041, fix.lon);
    /* The provider's own number wins over the caller's default. */
    TEST_ASSERT_EQUAL_INT32(65, fix.accuracy_m);
}

TEST_CASE("the nested location form yields the same fix", "[geo]")
{
    /* Providers in this space answer in both shapes and neither is ours to change. */
    const char *body = "{\"location\":{\"lat\":-33.8688,\"lng\":151.2093}}";
    geo_fix_t fix = {0};
    TEST_ASSERT_TRUE(geo_parse_fix(body, strlen(body), 5000, &fix));
    TEST_ASSERT_EQUAL_DOUBLE(-33.8688, fix.lat);
    TEST_ASSERT_EQUAL_DOUBLE(151.2093, fix.lon);
    /* No accuracy reported: the caller's default is what the model is told. */
    TEST_ASSERT_EQUAL_INT32(5000, fix.accuracy_m);
}

TEST_CASE("a response without both coordinates is refused", "[geo]")
{
    /* The dangerous default is 0,0: it is a real place (the Gulf of Guinea), so a
     * half-parsed fix would be reported as a confident wrong position rather than an
     * error. Every one of these must be a refusal. */
    const char *bodies[] = {
        "{}",
        "{\"lat\":52.3}",
        "{\"lon\":4.9}",
        "{\"location\":{\"lat\":52.3}}",
        "{\"lat\":\"52.3\",\"lon\":\"4.9\"}",
        "{\"lat\":null,\"lon\":null}",
        "not json at all",
        "",
    };
    for (size_t i = 0; i < sizeof bodies / sizeof bodies[0]; i++) {
        geo_fix_t fix = {0};
        TEST_ASSERT_FALSE_MESSAGE(geo_parse_fix(bodies[i], strlen(bodies[i]), 100, &fix),
                                  bodies[i]);
    }
}

TEST_CASE("a coordinate outside the planet is refused", "[geo]")
{
    const char *bodies[] = { "{\"lat\":91,\"lon\":0}",   "{\"lat\":-90.1,\"lon\":0}",
                             "{\"lat\":0,\"lon\":181}",  "{\"lat\":0,\"lon\":-180.1}" };
    for (size_t i = 0; i < sizeof bodies / sizeof bodies[0]; i++) {
        geo_fix_t fix = {0};
        TEST_ASSERT_FALSE_MESSAGE(geo_parse_fix(bodies[i], strlen(bodies[i]), 100, &fix),
                                  bodies[i]);
    }
    /* The poles and the antimeridian are on the planet. */
    geo_fix_t fix = {0};
    const char *edges = "{\"lat\":90,\"lon\":-180}";
    TEST_ASSERT_TRUE(geo_parse_fix(edges, strlen(edges), 100, &fix));
    TEST_ASSERT_EQUAL_DOUBLE(90.0, fix.lat);
    TEST_ASSERT_EQUAL_DOUBLE(-180.0, fix.lon);
}

TEST_CASE("0,0 is accepted as the place it is", "[geo]")
{
    /* Explicit, because the obvious over-fix is to treat 0,0 as "no fix" and refuse a
     * real coordinate. */
    geo_fix_t fix = {0};
    const char *body = "{\"lat\":0,\"lon\":0}";
    TEST_ASSERT_TRUE(geo_parse_fix(body, strlen(body), 100, &fix));
    TEST_ASSERT_EQUAL_DOUBLE(0.0, fix.lat);
    TEST_ASSERT_EQUAL_DOUBLE(0.0, fix.lon);
}

TEST_CASE("a negative or absurd accuracy is not trusted", "[geo]")
{
    geo_fix_t fix = {0};
    const char *neg = "{\"lat\":1,\"lon\":2,\"accuracy\":-5}";
    TEST_ASSERT_TRUE(geo_parse_fix(neg, strlen(neg), 100, &fix));
    TEST_ASSERT_EQUAL_INT32(100, fix.accuracy_m);

    const char *huge = "{\"lat\":1,\"lon\":2,\"accuracy\":1000001}";
    TEST_ASSERT_TRUE(geo_parse_fix(huge, strlen(huge), 100, &fix));
    TEST_ASSERT_EQUAL_INT32(100, fix.accuracy_m);
}

/* --- The scan payload ----------------------------------------------------- */

TEST_CASE("the scan body lists the BSSIDs it was given", "[geo]")
{
    const char *names[] = { "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02" };
    const int32_t rssi[] = { -47, -71 };
    char buf[256];
    size_t n = geo_scan_body(names, rssi, 2, buf, sizeof buf);
    TEST_ASSERT_GREATER_THAN(0, n);
    TEST_ASSERT_EQUAL_STRING(
        "[{\"bssid\":\"aa:bb:cc:dd:ee:01\",\"rssi\":-47},"
        "{\"bssid\":\"aa:bb:cc:dd:ee:02\",\"rssi\":-71}]",
        buf);
    TEST_ASSERT_EQUAL_UINT32(n, strlen(buf));
}

TEST_CASE("an empty scan produces an empty array, not a malformed body", "[geo]")
{
    char buf[64];
    TEST_ASSERT_EQUAL_UINT32(2, geo_scan_body(NULL, NULL, 0, buf, sizeof buf));
    TEST_ASSERT_EQUAL_STRING("[]", buf);
}

TEST_CASE("a body that does not fit is refused rather than truncated", "[geo]")
{
    /* A truncated body parses as fewer APs than were scanned, which is a fix computed
     * from a list the reader never saw. The caller then posts nothing and the chain
     * moves on. */
    const char *names[] = { "aa:bb:cc:dd:ee:01" };
    const int32_t rssi[] = { -47 };
    char buf[20];
    TEST_ASSERT_EQUAL_UINT32(0, geo_scan_body(names, rssi, 1, buf, sizeof buf));
}

TEST_CASE("a quote in a BSSID is refused, not spliced into JSON", "[geo]")
{
    /* BSSIDs come from the driver, but they are still text spliced into a body: a quote
     * would produce JSON the provider rejects with a 400 the reader cannot act on. */
    const char *names[] = { "aa\"bb" };
    const int32_t rssi[] = { -47 };
    char buf[128];
    TEST_ASSERT_EQUAL_UINT32(0, geo_scan_body(names, rssi, 1, buf, sizeof buf));

    const char *backslash[] = { "aa\\bb" };
    TEST_ASSERT_EQUAL_UINT32(0, geo_scan_body(backslash, rssi, 1, buf, sizeof buf));
}

/* --- The cache rule ------------------------------------------------------- */

TEST_CASE("a fix is fresh strictly within its ttl", "[geo]")
{
    TEST_ASSERT_TRUE(geo_fix_is_fresh(1000, 1000, 900));
    TEST_ASSERT_TRUE(geo_fix_is_fresh(1000, 1899, 900));
    /* Exactly the TTL old is stale: "held for `geo.cache_ttl_s`" reads as an upper
     * bound, and a lookup at the boundary must refresh. */
    TEST_ASSERT_FALSE(geo_fix_is_fresh(1000, 1900, 900));
    TEST_ASSERT_FALSE(geo_fix_is_fresh(1000, 2000, 900));
}

TEST_CASE("a clock that went backwards does not make a fix eternal", "[geo]")
{
    /* An NTP step after a fix moves `now` behind `source_ts`. The naive subtraction
     * underflows to a huge positive age and the fix looks fresh forever. */
    TEST_ASSERT_FALSE(geo_fix_is_fresh(2000, 1000, 900));
    /* No fix yet, and a non-positive TTL, are both stale. */
    TEST_ASSERT_FALSE(geo_fix_is_fresh(0, 1000, 900));
    TEST_ASSERT_FALSE(geo_fix_is_fresh(1000, 1000, 0));
}

/* --- Chain indexing ------------------------------------------------------- */

TEST_CASE("the provider chain is walked in the order it was written", "[geo]")
{
    geo_provider_t chain[2] = { { .id = "a" }, { .id = "b" } };
    TEST_ASSERT_EQUAL_STRING("a", geo_provider_at(chain, 2, 0)->id);
    TEST_ASSERT_EQUAL_STRING("b", geo_provider_at(chain, 2, 1)->id);
    /* Past the end is NULL, never the next slot: an unchecked index here would post a
     * stored key to whatever followed the array. */
    TEST_ASSERT_NULL(geo_provider_at(chain, 2, 2));
    TEST_ASSERT_NULL(geo_provider_at(chain, 0, 0));
    TEST_ASSERT_NULL(geo_provider_at(NULL, 2, 0));
}

/* --- The mqtt allowlist ---------------------------------------------------
 * `03-config.md` §mqtt: `topics[]` is the complete publish/subscribe allowlist, and the
 * grammar is deliberately smaller than MQTT's. The grammar itself is `cfg`'s, so these
 * cases exercise it through the cover predicate rather than restating it. */

TEST_CASE("a double survives the JSON round trip", "[json]")
{
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    json_node_t *root = json_root(doc);
    json_add_double(root, "lat", 48.858222);
    json_add_double(root, "lon", -2.2945);

    char buf[256];
    size_t n = 0;
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_emit(doc, buf, sizeof buf, &n));

    json_doc_t *back = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(buf, n, &back));
    double lat = 0, lon = 0;
    TEST_ASSERT_TRUE(json_double(json_get(json_root(back), "lat"), &lat));
    TEST_ASSERT_TRUE(json_double(json_get(json_root(back), "lon"), &lon));
    /* A tolerance, because the emitter's decimal form is finite; a full-precision
     * compare would pin the number of digits rather than the value. */
    TEST_ASSERT_FLOAT_WITHIN(1e-9, 48.858222, lat);
    TEST_ASSERT_FLOAT_WITHIN(1e-9, -2.2945, lon);
    json_free(back);
    json_free(doc);
}

TEST_CASE("a non-finite double is refused rather than emitted as null", "[json]")
{
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    json_node_t *root = json_root(doc);
    /* `json_add_double` returns bool; a NaN in a coordinate would emit as a bare token
     * no parser accepts, so the insertion is refused instead. */
    double nan_value = 0.0 / 0.0;
    TEST_ASSERT_FALSE(json_add_double(root, "nan", nan_value));
    TEST_ASSERT_NULL(json_get(root, "nan"));
    json_free(doc);

    /* And a non-number reads as false, not as 0.0: a coordinate of 0,0 is a real place
     * in the Gulf of Guinea, so "absent" and "zero" must stay distinguishable. */
    json_doc_t *other = json_new_object();
    json_add_string(json_root(other), "s", "48.8");
    double out = 12345;
    TEST_ASSERT_FALSE(json_double(json_get(json_root(other), "s"), &out));
    TEST_ASSERT_FLOAT_WITHIN(1e-9, 12345, out);
    TEST_ASSERT_FALSE(json_double(NULL, &out));
    json_free(other);
}

TEST_CASE("a filter covers its own topic and one level below it", "[mqtt]")
{
    TEST_ASSERT_TRUE(cfg_mqtt_filter_covers("home/office/temp", "home/office/temp"));
    TEST_ASSERT_TRUE(cfg_mqtt_filter_covers("sensors/*", "sensors/temp"));

    /* Exactly one level: a trailing star is not a subtree matcher. */
    TEST_ASSERT_FALSE(cfg_mqtt_filter_covers("sensors/*", "sensors/temp/raw"));
    /* Nor is it the parent. */
    TEST_ASSERT_FALSE(cfg_mqtt_filter_covers("sensors/*", "sensors"));
    /* Nor a sibling. */
    TEST_ASSERT_FALSE(cfg_mqtt_filter_covers("sensors/*", "actuators/temp"));
    /* An exact filter covers nothing else. */
    TEST_ASSERT_FALSE(cfg_mqtt_filter_covers("home/office/temp", "home/office/humidity"));
    /* A prefix is not a match: `home/office` must not cover `home/office/temp`. */
    TEST_ASSERT_FALSE(cfg_mqtt_filter_covers("home/office", "home/office/temp"));
    TEST_ASSERT_FALSE(cfg_mqtt_filter_covers(NULL, "x"));
    TEST_ASSERT_FALSE(cfg_mqtt_filter_covers("x", NULL));
}

TEST_CASE("the allowlist is a set, so any covering entry admits the topic", "[mqtt]")
{
    mqtt_topic_t topics[2] = { { .filter = "home/office/temp", .qos = 0 },
                               { .filter = "garage/*", .qos = 1 } };
    TEST_ASSERT_TRUE(mqtt_topics_cover(topics, 2, "home/office/temp"));
    TEST_ASSERT_TRUE(mqtt_topics_cover(topics, 2, "garage/door"));

    /* The important case: a topic no entry covers is refused, even though the broker
     * would accept it. This is the whole authorisation story. */
    TEST_ASSERT_FALSE(mqtt_topics_cover(topics, 2, "home/office/humidity"));
    TEST_ASSERT_FALSE(mqtt_topics_cover(topics, 2, "garage/door/state"));
    /* Only the first `count` entries count, so a stale tail cannot widen the list. */
    TEST_ASSERT_FALSE(mqtt_topics_cover(topics, 1, "garage/door"));
    TEST_ASSERT_FALSE(mqtt_topics_cover(NULL, 2, "x"));
    TEST_ASSERT_FALSE(mqtt_topics_cover(topics, 2, NULL));
}

TEST_CASE("qos 2 is refused wherever it appears", "[mqtt]")
{
    /* `03-config.md` §mqtt: unsupported permanently, not merely unimplemented. */
    TEST_ASSERT_TRUE(mqtt_qos_ok(0));
    TEST_ASSERT_TRUE(mqtt_qos_ok(1));
    TEST_ASSERT_FALSE(mqtt_qos_ok(2));
    TEST_ASSERT_FALSE(mqtt_qos_ok(-1));
    TEST_ASSERT_FALSE(mqtt_qos_ok(3));
}

TEST_CASE("the port defaults follow the transport", "[mqtt]")
{
    /* A configured port always wins; an absent one is 8883 or 1883 by transport, which
     * is what `03-config.md` §mqtt promises. */
    TEST_ASSERT_EQUAL_UINT16(8883, mqtt_effective_port(true, 0));
    TEST_ASSERT_EQUAL_UINT16(1883, mqtt_effective_port(false, 0));
    TEST_ASSERT_EQUAL_UINT16(1884, mqtt_effective_port(false, 1884));
    TEST_ASSERT_EQUAL_UINT16(8884, mqtt_effective_port(true, 8884));
}

TEST_CASE("the derived client id names the device and the broker", "[mqtt]")
{
    char buf[64];
    size_t n = mqtt_derive_client_id("xiao-s3-01", "home", buf, sizeof buf);
    TEST_ASSERT_EQUAL_STRING("musteval-xiao-s3-01-home", buf);
    TEST_ASSERT_EQUAL_UINT32(n, strlen(buf));

    /* A missing device name must not produce `musteval--home`. */
    TEST_ASSERT_GREATER_THAN(0, mqtt_derive_client_id(NULL, "home", buf, sizeof buf));
    TEST_ASSERT_EQUAL_STRING("musteval-device-home", buf);
}

TEST_CASE("a derived client id that does not fit is refused, not truncated", "[mqtt]")
{
    /* A truncated id is a different client to the broker, so it would shadow the real
     * device's session instead of failing where the reader can see it. */
    char buf[16] = "untouched";
    TEST_ASSERT_EQUAL_UINT32(0, mqtt_derive_client_id("xiao-s3-01", "home", buf, sizeof buf));
    TEST_ASSERT_EQUAL_STRING("", buf);
    TEST_ASSERT_EQUAL_UINT32(0, mqtt_derive_client_id("d", "s", NULL, 8));
    TEST_ASSERT_EQUAL_UINT32(0, mqtt_derive_client_id("d", NULL, buf, sizeof buf));
}

TEST_CASE("an oversize payload sets truncated rather than shortening silently", "[mqtt]")
{
    /* `04-protocol.md` §3.18: the frame carries a capped payload and a `truncated` flag.
     * A frame that quietly carried 1024 of 2000 bytes would read as a complete message. */
    TEST_ASSERT_FALSE(mqtt_payload_truncates(0));
    TEST_ASSERT_FALSE(mqtt_payload_truncates(MQTT_PAYLOAD_MAX));
    TEST_ASSERT_TRUE(mqtt_payload_truncates(MQTT_PAYLOAD_MAX + 1));
}

/* --- `task.set`'s cap -----------------------------------------------------
 * `tools/tasks.md` §Authority split: the model's cap is `tools.task_max`, the bridge's
 * is the schema's 16. An update must survive a full schedule, or the model could never
 * disable the task it just filled the last slot with. */
