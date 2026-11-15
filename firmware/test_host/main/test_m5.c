/* test_m5.c — the scheduler's pure half: the closed task schema, the trigger
 * matchers, the due-time arithmetic, the template renderer and the packed runtime
 * state.
 *
 * The board half (`sched.c`) is not here by design: it owns NVS, `esp_timer` and the
 * GPIO ISR, and a test against a stub of those would prove the stub. What *is* here
 * is every decision that half delegates, which is where a scheduler bug lives.
 */

#include <string.h>

#include "sched.h"
#include "tools.h"
#include "test_util.h"
#include "unity.h"

/* One minimal task object, in the shape `03-config.md` §tasks documents. */
static const char *k_boot_task =
    "{\"version\":1,\"tasks\":[{\"id\":\"greet\",\"trigger\":{\"type\":\"boot\"},"
    "\"action\":{\"kind\":\"prompt\",\"prompt\":\"Say hi.\"}}]}";

static size_t warnings;
static sched_tasks_t tasks;

static esp_err_t load(const char *json)
{
    char why[96] = "";
    warnings = 0;
    return sched_parse(json, strlen(json), &tasks, why, sizeof why, &warnings);
}

TEST_CASE("a minimal task takes the documented defaults", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    TEST_ASSERT_EQUAL_UINT(1, tasks.count);

    const sched_task_t *task = &tasks.tasks[0];
    TEST_ASSERT_EQUAL_STRING("greet", task->id);
    TEST_ASSERT_EQUAL(SCHED_TRIGGER_BOOT, task->type);
    TEST_ASSERT_TRUE(task->enabled);
    /* `03-config.md` §Task schema: `repeat` defaults to forever and `missed` to skip,
     * because a task nobody configured should keep doing its job. */
    TEST_ASSERT_EQUAL(SCHED_REPEAT_FOREVER, task->repeat_mode);
    TEST_ASSERT_EQUAL(SCHED_MISSED_SKIP, task->missed);
    TEST_ASSERT_EQUAL_STRING("Say hi.", task->prompt);
    TEST_ASSERT_EQUAL_INT32(0, task->fired_count);
}

TEST_CASE("an unknown key rejects the task it is in", "[m5]")
{
    /* The schema is closed: a task carrying a key outside the table is rejected
     * whole, rather than half-applied. */
    const char *json =
        "{\"version\":1,\"tasks\":["
        "{\"id\":\"ok\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}},"
        "{\"id\":\"bad\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"},\"colour\":\"red\"}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(1, tasks.count);
    TEST_ASSERT_EQUAL_STRING("ok", tasks.tasks[0].id);
    TEST_ASSERT_EQUAL_UINT(1, warnings);
}

TEST_CASE("an id outside the charset is refused", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"Greet Me\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
    TEST_ASSERT_EQUAL_UINT(1, warnings);
}

TEST_CASE("an id longer than fifteen bytes is refused", "[m5]")
{
    /* The ceiling is the NVS key limit, not a style rule: `03-config.md` §Open
     * questions 1 says the id is the runtime key. */
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"abcdefghijklmnop\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
}

TEST_CASE("a duplicate id rejects the whole file", "[m5]")
{
    /* Two tasks sharing an id would share a counter, so the file is refused rather
     * than one of them silently winning. */
    const char *json =
        "{\"version\":1,\"tasks\":["
        "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}},"
        "{\"id\":\"a\",\"trigger\":{\"type\":\"manual\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"y\"}}]}";

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
}

TEST_CASE("seventeen tasks exceed the cap", "[m5]")
{
    char json[2048];
    size_t written = catf(json, sizeof json, 0, "{\"version\":1,\"tasks\":[");
    for (int i = 0; i < SCHED_MAX_TASKS + 1; i++) {
        written = catf(json, sizeof json, written,
                       "%s{\"id\":\"t%d\",\"trigger\":{\"type\":\"boot\"},"
                       "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}",
                       i == 0 ? "" : ",", i);
    }
    written = catf(json, sizeof json, written, "]}");
    (void)written;

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, load(json));
}

TEST_CASE("an oversized document is refused before parsing", "[m5]")
{
    static char big[SCHED_JSON_MAX + 32];
    memset(big, 'x', sizeof big);
    char why[96] = "";
    size_t dropped = 0;
    sched_tasks_t out;

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_SIZE,
                      sched_parse(big, sizeof big, &out, why, sizeof why, &dropped));
    TEST_ASSERT_EQUAL_STRING("/tasks.json is too large", why);
}

TEST_CASE("a missing version is refused", "[m5]")
{
    const char *json =
        "{\"tasks\":[{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, load(json));
}

TEST_CASE("a gpio task needs its pin and level", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"b\",\"trigger\":{\"type\":\"gpio\",\"pin\":5},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
    TEST_ASSERT_EQUAL_UINT(1, warnings);
}

TEST_CASE("a gpio task at level two is refused", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"b\",\"trigger\":{\"type\":\"gpio\",\"pin\":5,\"level\":2},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
}

TEST_CASE("an mqtt topic with a single level wildcard is refused", "[m5]")
{
    /* `02-firmware.md` §5.4: `+` and `#` are not accepted in a task's filter; the
     * subscription list is the allowlist and this is a narrowing. */
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"m\",\"trigger\":{\"type\":\"mqtt_message\","
        "\"server_id\":\"broker\",\"topic\":\"home/+/temp\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
}

TEST_CASE("a mqtt task defaults its cooldown to five seconds", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"m\",\"trigger\":{\"type\":\"mqtt_message\","
        "\"server_id\":\"broker\",\"topic\":\"home/temp\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(1, tasks.count);
    TEST_ASSERT_EQUAL_INT32(5, tasks.tasks[0].cooldown_s);
}

TEST_CASE("a schedule at a daily time is accepted", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"s\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"at\",\"at\":\"07:30:00\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(1, tasks.count);
    TEST_ASSERT_EQUAL(SCHED_AT, tasks.tasks[0].kind);
}

TEST_CASE("a schedule at an unreadable instant is refused", "[m5]")
{
    /* An instant nobody can read would leave the task armed and never firing, which
     * reads as a scheduler bug rather than a config error. */
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"s\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"at\",\"at\":\"25:61:00\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
}

TEST_CASE("an interval below the floor is refused", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"s\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"every\",\"interval_s\":30},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
}

TEST_CASE("a repeat count below one is refused", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"r\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"},"
        "\"repeat\":{\"mode\":\"count\",\"count\":0}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
}

TEST_CASE("a count repeat without a count is refused", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"r\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"},"
        "\"repeat\":{\"mode\":\"count\"}}]}";

    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
}

/* --- The nine trigger names are the frozen ones --------------------------- */

TEST_CASE("every trigger name round trips", "[m5]")
{
    const char *expected[] = {"boot", "wifi_connected", "ntp_synced", "serial_connected",
                              "gpio", "mqtt_message", "event", "schedule", "manual"};
    for (int i = 0; i < SCHED_TRIGGER_COUNT; i++) {
        const char *name = sched_trigger_name((sched_trigger_t)i);
        TEST_ASSERT_EQUAL_STRING(expected[i], name);
        TEST_ASSERT_EQUAL_INT(i, sched_trigger_from_name(name));
    }
    TEST_ASSERT_EQUAL_INT(-1, sched_trigger_from_name("Boot"));
    TEST_ASSERT_EQUAL_INT(-1, sched_trigger_from_name("reboot"));
    TEST_ASSERT_NULL(sched_trigger_name(SCHED_TRIGGER_COUNT));
}

/* --- Topic matching ------------------------------------------------------- */

TEST_CASE("an exact topic matches only itself", "[m5]")
{
    TEST_ASSERT_TRUE(sched_topic_matches("home/temp", "home/temp"));
    TEST_ASSERT_FALSE(sched_topic_matches("home/temp", "home/temp2"));
    TEST_ASSERT_FALSE(sched_topic_matches("home/temp", "home"));
}

TEST_CASE("a star matches exactly one final level", "[m5]")
{
    TEST_ASSERT_TRUE(sched_topic_matches("home/sensor/*", "home/sensor/a"));
    TEST_ASSERT_FALSE(sched_topic_matches("home/sensor/*", "home/sensor/a/b"));
    TEST_ASSERT_FALSE(sched_topic_matches("home/sensor/*", "home/sensor/"));
    TEST_ASSERT_FALSE(sched_topic_matches("home/sensor/*", "home/sensor"));
}

TEST_CASE("a lone star matches one level and nothing more", "[m5]")
{
    TEST_ASSERT_TRUE(sched_topic_matches("*", "temp"));
    TEST_ASSERT_FALSE(sched_topic_matches("*", "home/temp"));
}

TEST_CASE("a star inside a level is literal", "[m5]")
{
    /* Only a trailing star is special; a star anywhere else is a character. */
    TEST_ASSERT_TRUE(sched_topic_matches("home/*/temp", "home/*/temp"));
    TEST_ASSERT_FALSE(sched_topic_matches("home/*/temp", "home/a/temp"));
}

/* --- Event names ---------------------------------------------------------- */

TEST_CASE("an event name accepts the documented charset", "[m5]")
{
    TEST_ASSERT_TRUE(sched_event_name_ok("door.bell"));
    TEST_ASSERT_TRUE(sched_event_name_ok("a_b-c.9"));
    TEST_ASSERT_FALSE(sched_event_name_ok(""));
    TEST_ASSERT_FALSE(sched_event_name_ok("Door"));
    TEST_ASSERT_FALSE(sched_event_name_ok("door bell"));
}

TEST_CASE("an event name over thirty two bytes is refused", "[m5]")
{
    char name[40];
    memset(name, 'a', sizeof name - 1);
    name[32] = '\0';
    TEST_ASSERT_TRUE(sched_event_name_ok(name));
    name[32] = 'a';
    name[33] = '\0';
    TEST_ASSERT_FALSE(sched_event_name_ok(name));
}

/* --- Matching ------------------------------------------------------------- */

TEST_CASE("a disabled task never matches", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"a\",\"enabled\":false,"
        "\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    sched_hit_t hits[SCHED_MAX_TASKS];
    TEST_ASSERT_EQUAL_UINT(0, sched_match_bare(&tasks, SCHED_TRIGGER_BOOT, hits,
                                              SCHED_MAX_TASKS));
}

TEST_CASE("an event triggers on an exact name only", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"e\",\"trigger\":{\"type\":\"event\",\"name\":\"door.bell\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    sched_hit_t hits[SCHED_MAX_TASKS];
    TEST_ASSERT_EQUAL_UINT(1, sched_match_event(&tasks, "door.bell", hits, SCHED_MAX_TASKS));
    TEST_ASSERT_EQUAL_UINT(0, sched_match_event(&tasks, "door", hits, SCHED_MAX_TASKS));
    TEST_ASSERT_EQUAL_UINT(0, sched_match_event(&tasks, "Door.Bell", hits, SCHED_MAX_TASKS));
}

TEST_CASE("gpio matches only its own pin and level", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"g\",\"trigger\":{\"type\":\"gpio\",\"pin\":5,\"level\":1},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    sched_hit_t hits[SCHED_MAX_TASKS];
    TEST_ASSERT_EQUAL_UINT(1, sched_match_gpio(&tasks, 5, 1, hits, SCHED_MAX_TASKS));
    TEST_ASSERT_EQUAL_UINT(0, sched_match_gpio(&tasks, 5, 0, hits, SCHED_MAX_TASKS));
    TEST_ASSERT_EQUAL_UINT(0, sched_match_gpio(&tasks, 6, 1, hits, SCHED_MAX_TASKS));
}

static const char *k_two_mqtt_tasks =
    "{\"version\":1,\"tasks\":["
    "{\"id\":\"wild\",\"trigger\":{\"type\":\"mqtt_message\",\"server_id\":\"b\","
    "\"topic\":\"home/sensor/*\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"w\"}},"
    "{\"id\":\"exact\",\"trigger\":{\"type\":\"mqtt_message\",\"server_id\":\"b\","
    "\"topic\":\"home/sensor/temp\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"e\"}}]}";

TEST_CASE("an exact topic is offered before a wildcard one", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_two_mqtt_tasks));

    sched_hit_t hits[SCHED_MAX_TASKS];
    size_t found = sched_match_mqtt(&tasks, "b", "home/sensor/temp", "21", 0, hits,
                                    SCHED_MAX_TASKS);
    TEST_ASSERT_EQUAL_UINT(2, found);
    TEST_ASSERT_EQUAL_UINT(1, hits[0].index); /* `exact` is tasks[1] */
    TEST_ASSERT_EQUAL_UINT(0, hits[1].index); /* `wild` is tasks[0] */
}

TEST_CASE("a wildcard task also matches the exact topic", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_two_mqtt_tasks));

    sched_hit_t hits[SCHED_MAX_TASKS];
    TEST_ASSERT_EQUAL_UINT(1, sched_match_mqtt(&tasks, "b", "home/sensor/pressure", "x", 0,
                                              hits, SCHED_MAX_TASKS));
    TEST_ASSERT_EQUAL_UINT(0, hits[0].index);
}

TEST_CASE("another server does not match", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_two_mqtt_tasks));

    sched_hit_t hits[SCHED_MAX_TASKS];
    TEST_ASSERT_EQUAL_UINT(0, sched_match_mqtt(&tasks, "other", "home/sensor/temp", "x", 0,
                                              hits, SCHED_MAX_TASKS));
}

TEST_CASE("a payload filter requires the substring", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"p\",\"trigger\":{\"type\":\"mqtt_message\","
        "\"server_id\":\"b\",\"topic\":\"home/door\",\"payload_contains\":\"OPEN\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    sched_hit_t hits[SCHED_MAX_TASKS];
    TEST_ASSERT_EQUAL_UINT(1, sched_match_mqtt(&tasks, "b", "home/door", "DOOR OPEN", 0,
                                              hits, SCHED_MAX_TASKS));
    TEST_ASSERT_EQUAL_UINT(0, sched_match_mqtt(&tasks, "b", "home/door", "door closed", 0,
                                              hits, SCHED_MAX_TASKS));
    TEST_ASSERT_EQUAL_UINT(0, sched_match_mqtt(&tasks, "b", "home/door", NULL, 0, hits,
                                              SCHED_MAX_TASKS));
}

TEST_CASE("a task inside its cooldown does not match", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"c\",\"trigger\":{\"type\":\"mqtt_message\","
        "\"server_id\":\"b\",\"topic\":\"home/door\",\"cooldown_s\":60},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    tasks.tasks[0].last_fired = 1000;

    sched_hit_t hits[SCHED_MAX_TASKS];
    TEST_ASSERT_EQUAL_UINT(0, sched_match_mqtt(&tasks, "b", "home/door", "x", 1030, hits,
                                              SCHED_MAX_TASKS));
    TEST_ASSERT_EQUAL_UINT(1, sched_match_mqtt(&tasks, "b", "home/door", "x", 1060, hits,
                                              SCHED_MAX_TASKS));
}

TEST_CASE("a zero cooldown never suppresses", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"c\",\"trigger\":{\"type\":\"mqtt_message\","
        "\"server_id\":\"b\",\"topic\":\"home/door\",\"cooldown_s\":0},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    tasks.tasks[0].last_fired = 1000;

    sched_hit_t hits[SCHED_MAX_TASKS];
    TEST_ASSERT_EQUAL_UINT(1, sched_match_mqtt(&tasks, "b", "home/door", "x", 1000, hits,
                                              SCHED_MAX_TASKS));
}

/* --- Repeat --------------------------------------------------------------- */

TEST_CASE("repeat once fires only from zero", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"o\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"},"
        "\"repeat\":{\"mode\":\"once\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    TEST_ASSERT_TRUE(sched_repeat_allows(&tasks.tasks[0]));
    tasks.tasks[0].fired_count = 1;
    TEST_ASSERT_FALSE(sched_repeat_allows(&tasks.tasks[0]));
}

TEST_CASE("repeat count stops at its maximum", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"c\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"},"
        "\"repeat\":{\"mode\":\"count\",\"count\":2}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    TEST_ASSERT_TRUE(sched_repeat_allows(&tasks.tasks[0]));
    tasks.tasks[0].fired_count = 1;
    TEST_ASSERT_TRUE(sched_repeat_allows(&tasks.tasks[0]));
    tasks.tasks[0].fired_count = 2;
    TEST_ASSERT_FALSE(sched_repeat_allows(&tasks.tasks[0]));
}

TEST_CASE("repeat forever always allows", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    tasks.tasks[0].fired_count = 1000;
    TEST_ASSERT_TRUE(sched_repeat_allows(&tasks.tasks[0]));
}

/* --- Schedule arithmetic -------------------------------------------------- */

TEST_CASE("an hhmmss parses to seconds and refuses junk", "[m5]")
{
    TEST_ASSERT_EQUAL_INT32(0, sched_parse_hhmmss("00:00:00"));
    TEST_ASSERT_EQUAL_INT32(27000, sched_parse_hhmmss("07:30:00"));
    TEST_ASSERT_EQUAL_INT32(86399, sched_parse_hhmmss("23:59:59"));
    TEST_ASSERT_EQUAL_INT32(-1, sched_parse_hhmmss("24:00:00"));
    TEST_ASSERT_EQUAL_INT32(-1, sched_parse_hhmmss("07:60:00"));
    TEST_ASSERT_EQUAL_INT32(-1, sched_parse_hhmmss("7:30:00"));
    TEST_ASSERT_EQUAL_INT32(-1, sched_parse_hhmmss("07-30-00"));
    TEST_ASSERT_EQUAL_INT32(-1, sched_parse_hhmmss(""));
    TEST_ASSERT_EQUAL_INT32(-1, sched_parse_hhmmss(NULL));
}

TEST_CASE("an iso instant is recognised and refused when wrong", "[m5]")
{
    TEST_ASSERT_TRUE(sched_is_iso("2026-03-01T08:00:00"));
    TEST_ASSERT_TRUE(sched_is_iso("2026-03-01T08:00:00Z"));
    TEST_ASSERT_FALSE(sched_is_iso("2026-13-01T08:00:00"));
    TEST_ASSERT_FALSE(sched_is_iso("2026-03-01 08:00"));
    TEST_ASSERT_FALSE(sched_is_iso("07:30:00"));
    TEST_ASSERT_FALSE(sched_is_iso(NULL));
}

TEST_CASE("an every task fires on its interval boundary", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"e\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"every\",\"interval_s\":3600,\"first_at\":1000000},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    const sched_task_t *task = &tasks.tasks[0];
    /* Between boundaries the last due time is the one just passed... */
    TEST_ASSERT_EQUAL_INT64(1000000, sched_last_due(task, 1000000 + 10, 0, 0));
    /* Just past a boundary the last due time is that boundary, not `now`. */
    TEST_ASSERT_EQUAL_INT64(1000000 + 3600, sched_last_due(task, 1000000 + 3610, 0, 0));
    /* ...and the next one is the following boundary. */
    TEST_ASSERT_EQUAL_INT64(1000000 + 3600, sched_next_fire(task, 1000000 + 10, 0, 0));
}

TEST_CASE("an every task without first at counts from boot", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"e\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"every\",\"interval_s\":600},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    const sched_task_t *task = &tasks.tasks[0];
    /* Before boot there is no due time at all, which is not the same as "now". */
    TEST_ASSERT_EQUAL_INT64(0, sched_last_due(task, 500, 1000, 0));
    TEST_ASSERT_EQUAL_INT64(1000, sched_last_due(task, 1500, 1000, 0));
}

TEST_CASE("a daily at time is due once a day", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"d\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"at\",\"at\":\"07:30:00\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    const sched_task_t *task = &tasks.tasks[0];
    /* 2026-03-01T00:00:00Z is 1772323200; 07:30:00 that day is +27000. */
    int64_t midnight = 1772323200;
    TEST_ASSERT_EQUAL_INT64(midnight + 27000, sched_last_due(task, midnight + 30000, 0, 0));
    /* At 06:00 the last occurrence was yesterday's, which is how a tick at 06:00
     * does not fire today's 07:30 twice. */
    TEST_ASSERT_EQUAL_INT64(midnight - 86400 + 27000,
                            sched_last_due(task, midnight + 21600, 0, 0));
    TEST_ASSERT_EQUAL_INT64(midnight + 27000, sched_next_fire(task, midnight + 21600, 0, 0));
}

TEST_CASE("a daily time honours the timezone offset", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"d\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"at\",\"at\":\"07:30:00\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    const sched_task_t *task = &tasks.tasks[0];
    int64_t midnight = 1772323200;
    /* UTC+2 means 07:30 local is 05:30 UTC, so the due instant moves two hours back. */
    TEST_ASSERT_EQUAL_INT64(midnight + 27000 - 7200,
                            sched_next_fire(task, midnight, 0, 7200));
}

TEST_CASE("a fixed instant is absolute and fires once", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"f\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"at\",\"at\":\"2026-03-01T08:00:00Z\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    const sched_task_t *task = &tasks.tasks[0];
    int64_t expected = 1772323200 + 8 * 3600;
    TEST_ASSERT_EQUAL_INT64(expected, sched_last_due(task, expected + 100, 0, 0));
    TEST_ASSERT_EQUAL_INT64(expected, sched_next_fire(task, 0, 0, 0));
    /* A fixed instant does not recur, so before it there is no due time. */
    TEST_ASSERT_EQUAL_INT64(0, sched_last_due(task, expected - 100, 0, 0));
}

TEST_CASE("a missed fire skips by default", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"m\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"every\",\"interval_s\":60},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    const sched_task_t *task = &tasks.tasks[0];
    /* Within the grace window the fire still runs; past it, `skip` drops it. */
    TEST_ASSERT_TRUE(sched_missed_should_fire(task, 1000, 1030, 90));
    TEST_ASSERT_FALSE(sched_missed_should_fire(task, 1000, 1200, 90));
    /* A due time in the future is not missed, it is not yet. */
    TEST_ASSERT_FALSE(sched_missed_should_fire(task, 1000, 900, 90));
}

TEST_CASE("a run once task fires however late", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":[{\"id\":\"r\",\"trigger\":{\"type\":\"schedule\","
        "\"kind\":\"every\",\"interval_s\":60},\"missed\":\"run_once\","
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    TEST_ASSERT_TRUE(sched_missed_should_fire(&tasks.tasks[0], 1000, 100000, 90));
}

/* --- Upsert, remove, emit ------------------------------------------------- */

TEST_CASE("an upsert creates then updates in place", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    sched_task_t task = tasks.tasks[0];
    bool created = true;

    TEST_ASSERT_EQUAL(ESP_OK, sched_tasks_upsert(&tasks, &task, &created));
    TEST_ASSERT_FALSE(created);
    TEST_ASSERT_EQUAL_UINT(1, tasks.count);

    task.id[0] = 'x';
    task.id[1] = '\0';
    TEST_ASSERT_EQUAL(ESP_OK, sched_tasks_upsert(&tasks, &task, &created));
    TEST_ASSERT_TRUE(created);
    TEST_ASSERT_EQUAL_UINT(2, tasks.count);
}

TEST_CASE("an update keeps the runtime counter", "[m5]")
{
    /* `03-config.md` §tasks: only a delete-then-set starts a task over. Editing a
     * task must not reset a `count` budget it has already spent. */
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    tasks.tasks[0].fired_count = 7;
    tasks.tasks[0].last_fired = 1234;

    sched_task_t changed = tasks.tasks[0];
    changed.fired_count = 0;
    changed.last_fired = 0;
    bool created = true;
    TEST_ASSERT_EQUAL(ESP_OK, sched_tasks_upsert(&tasks, &changed, &created));

    TEST_ASSERT_EQUAL_INT32(7, tasks.tasks[0].fired_count);
    TEST_ASSERT_EQUAL_INT64(1234, tasks.tasks[0].last_fired);
}

TEST_CASE("an upsert past the cap is refused", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    tasks.count = SCHED_MAX_TASKS;
    sched_task_t task = tasks.tasks[0];
    task.id[0] = 'z';
    task.id[1] = '\0';
    bool created = false;

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_SIZE, sched_tasks_upsert(&tasks, &task, &created));
    TEST_ASSERT_FALSE(created);
}

TEST_CASE("a remove reports whether the id was there", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    bool deleted = true;

    TEST_ASSERT_EQUAL(ESP_OK, sched_tasks_remove(&tasks, "absent", &deleted));
    TEST_ASSERT_FALSE(deleted);
    TEST_ASSERT_EQUAL_UINT(1, tasks.count);

    TEST_ASSERT_EQUAL(ESP_OK, sched_tasks_remove(&tasks, "greet", &deleted));
    TEST_ASSERT_TRUE(deleted);
    TEST_ASSERT_EQUAL_UINT(0, tasks.count);
}

TEST_CASE("an emit round trips through the parser", "[m5]")
{
    /* The file `tasks.set` writes must be one `sched_parse` accepts, or a change made
     * over the wire is lost on the next reboot. */
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));

    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, sched_emit_json(&tasks, &doc));
    static char text[SCHED_JSON_MAX];
    size_t len = 0;
    TEST_ASSERT_EQUAL(ESP_OK, json_emit(doc, text, sizeof text, &len));
    json_free(doc);

    sched_tasks_t again;
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_OK, sched_parse(text, len, &again, why, sizeof why, NULL));
    TEST_ASSERT_EQUAL_UINT(1, again.count);
    TEST_ASSERT_EQUAL_STRING("greet", again.tasks[0].id);
    TEST_ASSERT_EQUAL_STRING("Say hi.", again.tasks[0].prompt);
    TEST_ASSERT_EQUAL(SCHED_TRIGGER_BOOT, again.tasks[0].type);
}

TEST_CASE("every trigger shape survives a round trip", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":["
        "{\"id\":\"g\",\"trigger\":{\"type\":\"gpio\",\"pin\":4,\"level\":0},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}},"
        "{\"id\":\"m\",\"trigger\":{\"type\":\"mqtt_message\",\"server_id\":\"b\","
        "\"topic\":\"a/*\",\"payload_contains\":\"on\",\"cooldown_s\":9},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}},"
        "{\"id\":\"e\",\"trigger\":{\"type\":\"event\",\"name\":\"door.bell\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}},"
        "{\"id\":\"s\",\"trigger\":{\"type\":\"schedule\",\"kind\":\"every\","
        "\"interval_s\":90,\"first_at\":1000000},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));
    TEST_ASSERT_EQUAL_UINT(4, tasks.count);

    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, sched_emit_json(&tasks, &doc));
    static char text[SCHED_JSON_MAX];
    size_t len = 0;
    TEST_ASSERT_EQUAL(ESP_OK, json_emit(doc, text, sizeof text, &len));
    json_free(doc);

    sched_tasks_t again;
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_OK, sched_parse(text, len, &again, why, sizeof why, NULL));
    TEST_ASSERT_EQUAL_UINT(4, again.count);
    TEST_ASSERT_EQUAL_INT(4, again.tasks[0].pin);
    TEST_ASSERT_EQUAL_INT(0, again.tasks[0].level);
    TEST_ASSERT_EQUAL_STRING("a/*", again.tasks[1].topic);
    TEST_ASSERT_EQUAL_STRING("on", again.tasks[1].payload_contains);
    TEST_ASSERT_EQUAL_INT32(9, again.tasks[1].cooldown_s);
    TEST_ASSERT_EQUAL_STRING("door.bell", again.tasks[2].name);
    TEST_ASSERT_EQUAL_INT32(90, again.tasks[3].interval_s);
    TEST_ASSERT_EQUAL_INT64(1000000, again.tasks[3].first_at);
}

TEST_CASE("a list reports the due time only for a schedule task", "[m5]")
{
    const char *json =
        "{\"version\":1,\"tasks\":["
        "{\"id\":\"b\",\"trigger\":{\"type\":\"boot\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}},"
        "{\"id\":\"s\",\"trigger\":{\"type\":\"schedule\",\"kind\":\"every\","
        "\"interval_s\":60,\"first_at\":1000000},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}]}";
    TEST_ASSERT_EQUAL(ESP_OK, load(json));

    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, sched_list_json(&tasks, 1000010, 0, 0, &doc));
    const json_node_t *list = json_get(json_root(doc), "tasks");
    TEST_ASSERT_EQUAL_UINT(2, json_array_len(list));

    /* A boot task has no due time, and reporting 0 would read as "now". */
    TEST_ASSERT_NULL(json_get(json_array_at(list, 0), "next_fire_at"));
    int64_t next = 0;
    TEST_ASSERT_TRUE(json_int(json_get(json_array_at(list, 1), "next_fire_at"), &next));
    TEST_ASSERT_EQUAL_INT64(1000060, next);
    json_free(doc);
}

/* --- Template rendering --------------------------------------------------- */

static const char *k_template =
    "---\nvars: [name, place]\n---\nHello {{name}}, welcome to {{place}}.{{extra}}\n";

TEST_CASE("a template drops its frontmatter and fills declared vars", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    sched_task_t *task = &tasks.tasks[0];
    snprintf(task->vars[0].name, sizeof task->vars[0].name, "name");
    snprintf(task->vars[0].value, sizeof task->vars[0].value, "Ada");
    snprintf(task->vars[1].name, sizeof task->vars[1].name, "place");
    snprintf(task->vars[1].value, sizeof task->vars[1].value, "the lab");
    task->vars_count = 2;

    char out[256];
    size_t len = 0;
    TEST_ASSERT_EQUAL(ESP_OK, sched_render_template(k_template, strlen(k_template), task,
                                                    out, sizeof out, &len));
    TEST_ASSERT_EQUAL_STRING("Hello Ada, welcome to the lab.{{extra}}\n", out);
    TEST_ASSERT_EQUAL_UINT(strlen(out), len);
}

TEST_CASE("an undeclared placeholder is left alone", "[m5]")
{
    /* Rule 2 of `03-config.md` §Template schema: undeclared survives verbatim, so a
     * prompt that mentions `{{x}}` on purpose is not mangled. */
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    char out[128];
    TEST_ASSERT_EQUAL(ESP_OK, sched_render_template("Hi {{nobody}}\n", strlen("Hi {{nobody}}\n"),
                                                    &tasks.tasks[0], out, sizeof out, NULL));
    TEST_ASSERT_EQUAL_STRING("Hi {{nobody}}\n", out);
}

TEST_CASE("a declared var left unsupplied renders empty", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    char out[128];
    TEST_ASSERT_EQUAL(ESP_OK, sched_render_template("---\nvars: [who]\n---\nHi {{who}}!\n",
                                                    strlen("---\nvars: [who]\n---\nHi {{who}}!\n"), &tasks.tasks[0], out, sizeof out,
                                                    NULL));
    TEST_ASSERT_EQUAL_STRING("Hi !\n", out);
}

TEST_CASE("a file without frontmatter is the whole prompt", "[m5]")
{
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    char out[128];
    TEST_ASSERT_EQUAL(ESP_OK, sched_render_template("Just this.\n", strlen("Just this.\n"), &tasks.tasks[0],
                                                    out, sizeof out, NULL));
    TEST_ASSERT_EQUAL_STRING("Just this.\n", out);
}

TEST_CASE("a render that does not fit is refused not cut", "[m5]")
{
    /* A silently truncated instruction is a different instruction. */
    TEST_ASSERT_EQUAL(ESP_OK, load(k_boot_task));
    char out[4];
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_SIZE,
                      sched_render_template("long enough to overflow\n", strlen("long enough to overflow\n"),
                                            &tasks.tasks[0], out, sizeof out, NULL));
}

/* --- Packed runtime state ------------------------------------------------- */

TEST_CASE("the runtime state round trips", "[m5]")
{
    char text[40];
    TEST_ASSERT_EQUAL(ESP_OK, sched_state_encode(7, 1772323200, text, sizeof text));
    TEST_ASSERT_EQUAL_STRING("7:1772323200", text);

    int32_t count = -1;
    int64_t last = -1;
    TEST_ASSERT_TRUE(sched_state_decode(text, &count, &last));
    TEST_ASSERT_EQUAL_INT32(7, count);
    TEST_ASSERT_EQUAL_INT64(1772323200, last);
}

TEST_CASE("an unreadable runtime state is reported not guessed", "[m5]")
{
    int32_t count = 0;
    int64_t last = 0;
    TEST_ASSERT_FALSE(sched_state_decode(NULL, &count, &last));
    TEST_ASSERT_FALSE(sched_state_decode("", &count, &last));
    TEST_ASSERT_FALSE(sched_state_decode("7", &count, &last));
    TEST_ASSERT_FALSE(sched_state_decode(":12", &count, &last));
    TEST_ASSERT_FALSE(sched_state_decode("7:", &count, &last));
    TEST_ASSERT_FALSE(sched_state_decode("x:12", &count, &last));
    TEST_ASSERT_FALSE(sched_state_decode("7:1x", &count, &last));
}

TEST_CASE("an encode that does not fit is refused", "[m5]")
{
    char small[4];
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_SIZE,
                      sched_state_encode(1000, 1772323200, small, sizeof small));
}

/* `tools/events.md` §Arguments: the charset and the two length bounds are prose in
 * the contract, so this predicate is the only place the rule exists. Each arm is a
 * distinct refusal the model can fix. */
TEST_CASE("an event type is 1..32 bytes of the trigger charset", "[m5]")
{
    char why[64];
    TEST_ASSERT_TRUE(tools_event_args_ok("led.changed", NULL, why, sizeof why));
    TEST_ASSERT_TRUE(tools_event_args_ok("a", NULL, why, sizeof why));
    TEST_ASSERT_TRUE(tools_event_args_ok("a_b-c.9", NULL, why, sizeof why));
    TEST_ASSERT_TRUE(tools_event_args_ok("abc", "sensor=1", why, sizeof why));

    TEST_ASSERT_FALSE(tools_event_args_ok(NULL, NULL, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("type: required string", why);
    TEST_ASSERT_FALSE(tools_event_args_ok("", NULL, why, sizeof why));

    TEST_ASSERT_FALSE(tools_event_args_ok("Led", NULL, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("type: only [a-z0-9._-] is allowed", why);
    TEST_ASSERT_FALSE(tools_event_args_ok("led changed", NULL, why, sizeof why));
    TEST_ASSERT_FALSE(tools_event_args_ok("led/1", NULL, why, sizeof why));

    /* 32 fits, 33 does not: the bound is the trigger `name` ceiling. */
    TEST_ASSERT_TRUE(tools_event_args_ok("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", NULL, why,
                                         sizeof why));
    TEST_ASSERT_FALSE(tools_event_args_ok("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", NULL, why,
                                          sizeof why));
    TEST_ASSERT_EQUAL_STRING("type: longer than 32 chars", why);
}

TEST_CASE("an event data string is at most 512 bytes", "[m5]")
{
    char why[64];
    char ok[513];
    char too_long[515];
    memset(ok, 'x', sizeof ok - 1);
    ok[sizeof ok - 1] = '\0';
    memset(too_long, 'x', sizeof too_long - 1);
    too_long[sizeof too_long - 1] = '\0';

    TEST_ASSERT_TRUE(tools_event_args_ok("t", ok, why, sizeof why));
    TEST_ASSERT_FALSE(tools_event_args_ok("t", too_long, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("data: longer than 512 chars", why);
}

/* `tools/tasks.md` §`task.list`: the tool returns the stored task objects, so the
 * result is exactly what `/tasks.json` holds — no `fired_count`, no `next_fire_at`,
 * which are `tasks.list`'s enrichment. One owner for the shape is `sched_emit_json`,
 * which is also what the whole-file rewrite writes. */
TEST_CASE("the stored task document is what task.list returns", "[m5]")
{
    sched_tasks_t tasks;
    memset(&tasks, 0, sizeof tasks);
    sched_task_t task;
    memset(&task, 0, sizeof task);
    snprintf(task.id, sizeof task.id, "nightly");
    task.enabled = true;
    task.type = SCHED_TRIGGER_SCHEDULE;
    task.kind = SCHED_AT;
    snprintf(task.at, sizeof task.at, "22:00:00");
    task.repeat_mode = SCHED_REPEAT_FOREVER;
    task.action = SCHED_ACTION_PROMPT;
    snprintf(task.prompt, sizeof task.prompt, "summarise the day");
    TEST_ASSERT_EQUAL(ESP_OK, sched_tasks_upsert(&tasks, &task, NULL));

    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, sched_emit_json(&tasks, &doc));
    TEST_ASSERT_NOT_NULL(doc);
    json_node_t *list = json_get(json_root(doc), "tasks");
    TEST_ASSERT_NOT_NULL(list);
    TEST_ASSERT_EQUAL_INT(1, json_array_len(list));
    const json_node_t *entry = json_array_at(list, 0);
    TEST_ASSERT_EQUAL_STRING("nightly", json_string(json_get(entry, "id")));
    TEST_ASSERT_EQUAL_STRING("22:00:00",
                             json_string(json_get(json_get(entry, "trigger"), "at")));
    /* Runtime state is NVS, never the file: `03-config.md` §tasks. */
    TEST_ASSERT_NULL(json_get(entry, "fired_count"));
    TEST_ASSERT_NULL(json_get(entry, "next_fire_at"));

    /* The tool copies the array out before freeing the source, so this is the same
     * move: read the copy after the original is gone. */
    json_doc_t *copy = json_new_object();
    TEST_ASSERT_NOT_NULL(copy);
    TEST_ASSERT_NOT_NULL(json_add_node(json_root(copy), "tasks", list));
    json_free(doc);
    const json_node_t *copied = json_array_at(json_get(json_root(copy), "tasks"), 0);
    TEST_ASSERT_EQUAL_STRING("nightly", json_string(json_get(copied, "id")));
    json_free(copy);
}

/* The closed-schema refusals, one row per arm of `sched_task_validate` and
 * `sched_parse`. Every row is a document `03-config.md` §tasks forbids, and each
 * carries the message it must produce: a refusal that names the wrong key sends the
 * user to the wrong line of their file. */
typedef struct {
    const char *why;
    const char *task; /* one task object, spliced into a valid file */
} refusal_t;

static const refusal_t k_refusals[] = {
    {"task.enabled: not a boolean", "{\"id\":\"a\",\"enabled\":1,\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"task.trigger: required object", "{\"id\":\"a\",\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"task.action: required object", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"}}"},
    {"trigger.type: nope is not one of the nine", "{\"id\":\"a\",\"trigger\":{\"type\":\"nope\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger.name: must be [a-z0-9._-]{1,32}", "{\"id\":\"a\",\"trigger\":{\"type\":\"event\",\"name\":\"Bad Name\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger.topic: + and # are not accepted; use a trailing *", "{\"id\":\"a\",\"trigger\":{\"type\":\"mqtt_message\",\"server_id\":\"s\",\"topic\":\"a/+\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger.at: must be HH:MM:SS or an ISO-8601 instant", "{\"id\":\"a\",\"trigger\":{\"type\":\"schedule\",\"kind\":\"at\",\"at\":\"7:00\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger.kind: must be at or every", "{\"id\":\"a\",\"trigger\":{\"type\":\"schedule\",\"kind\":\"weekly\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"action.kind: must be prompt or template", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"shell\",\"prompt\":\"x\"}}"},
    {"action.template: must be [a-z0-9_-]{1,32}", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"template\",\"template\":\"Bad Name\"}}"},
    {"action.vars: not an object", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"template\",\"template\":\"t\",\"vars\":[]}}"},
    {"action.vars: every value must be a string", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"template\",\"template\":\"t\",\"vars\":{\"who\":1}}}"},
    {"repeat.mode: must be once, count or forever", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"},\"repeat\":{\"mode\":\"twice\"}}"},
    {"task.missed: not a string", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"},\"missed\":5}"},
    {"task.missed: must be skip or run_once", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"},\"missed\":\"later\"}"},
    {"task.id: must be [a-z0-9_-]", "{\"id\":\"A\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"task: unknown key nope", "{\"id\":\"a\",\"nope\":1,\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger: unknown key nope", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\",\"nope\":1},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"action: unknown key nope", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\",\"nope\":1}}"},
    {"task.description: not a string", "{\"id\":\"a\",\"description\":5,\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"task.description: must be 0..127 bytes", "{\"id\":\"a\",\"description\":\"DESCRIPTION_130\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger: type is required", "{\"id\":\"a\",\"trigger\":{},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger: pin is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"gpio\",\"level\":1},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger: level is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"gpio\",\"pin\":9},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger: server_id is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"mqtt_message\",\"topic\":\"t\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger: name is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"event\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger: kind is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"schedule\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger: interval_s is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"schedule\",\"kind\":\"every\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger.pin: not an integer", "{\"id\":\"a\",\"trigger\":{\"type\":\"gpio\",\"pin\":\"9\",\"level\":1},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"trigger.interval_s: must be 60..604800", "{\"id\":\"a\",\"trigger\":{\"type\":\"schedule\",\"kind\":\"every\",\"interval_s\":5},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"}}"},
    {"repeat: count is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\",\"prompt\":\"x\"},\"repeat\":{\"mode\":\"count\"}}"},
    {"action: kind is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"prompt\":\"x\"}}"},
    {"action: prompt is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"prompt\"}}"},
    {"action: template is required", "{\"id\":\"a\",\"trigger\":{\"type\":\"boot\"},\"action\":{\"kind\":\"template\"}}"},
};

TEST_CASE("a task object outside the closed schema names the key it refuses", "[m5]")
{
    char file[1024];
    char why[96];
    sched_tasks_t out;
    size_t n = sizeof k_refusals / sizeof *k_refusals;
    TEST_ASSERT_TRUE(n >= 30); /* a scan over zero rows proves nothing */
    for (size_t i = 0; i < n; i++) {
        char expanded[1024];
        const char *token = strstr(k_refusals[i].task, "DESCRIPTION_130");
        if (token != NULL) {
            /* 130 bytes, two past the 128-byte field: the boundary the doc names. */
            char long_desc[131];
            memset(long_desc, 'd', 130);
            long_desc[130] = '\0';
            size_t head = (size_t)(token - k_refusals[i].task);
            snprintf(expanded, sizeof expanded, "%.*s%s%s", (int)head, k_refusals[i].task,
                     long_desc, token + strlen("DESCRIPTION_130"));
        } else {
            snprintf(expanded, sizeof expanded, "%s", k_refusals[i].task);
        }
        int written = snprintf(file, sizeof file, "{\"version\":1,\"tasks\":[%s]}",
                               expanded);
        TEST_ASSERT_TRUE(written > 0 && (size_t)written < sizeof file);
        why[0] = '\0';
        size_t dropped = 0;
        esp_err_t err = sched_parse(file, (size_t)written, &out, why, sizeof why, &dropped);
        /* One bad task does not cost the file: it is dropped and the rest stays
         * usable. The refusal still has to name the key that was wrong. */
        TEST_ASSERT_EQUAL_MESSAGE(ESP_OK, err, k_refusals[i].task);
        TEST_ASSERT_EQUAL_INT_MESSAGE(1, (int)dropped, k_refusals[i].task);
        TEST_ASSERT_EQUAL_INT_MESSAGE(0, (int)out.count, k_refusals[i].task);
        TEST_ASSERT_EQUAL_STRING_MESSAGE(k_refusals[i].why, why, k_refusals[i].task);
    }
}

/* The three whole-file refusals, which are a different layer from a refused task. */
TEST_CASE("a tasks file outside the frozen envelope is refused whole", "[m5]")
{
    char why[96];
    sched_tasks_t out;

    why[0] = '\0';
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, sched_parse("[]", 2, &out, why, sizeof why, NULL));
    TEST_ASSERT_EQUAL_STRING("root is not an object", why);

    const char *bad_version = "{\"version\":2,\"tasks\":[]}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      sched_parse(bad_version, strlen(bad_version), &out, why, sizeof why, NULL));
    TEST_ASSERT_EQUAL_STRING("version: must be 1", why);

    const char *no_tasks = "{\"version\":1}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      sched_parse(no_tasks, strlen(no_tasks), &out, why, sizeof why, NULL));
    TEST_ASSERT_EQUAL_STRING("tasks: required array", why);

    const char *bad = "{\"version\":1,";
    TEST_ASSERT_NOT_EQUAL(ESP_OK, sched_parse(bad, strlen(bad), &out, why, sizeof why, NULL));
    TEST_ASSERT_EQUAL_STRING("/tasks.json does not parse", why);
}

/* A task survives a round trip through both emitters. `sched_emit_json` is what
 * `/tasks.json` is rewritten as, so anything it drops is silently lost from the file
 * on the next save; `sched_list_json` is what `tasks.list` answers, so anything it
 * drops is invisible to the bridge. */
TEST_CASE("every task field survives both emitters", "[m5]")
{
    const char *file =
        "{\"version\":1,\"tasks\":["
        "{\"id\":\"tpl\",\"description\":\"two vars\","
        "\"trigger\":{\"type\":\"event\",\"name\":\"door.open\"},"
        "\"action\":{\"kind\":\"template\",\"template\":\"greet\","
        "\"vars\":{\"name\":\"ada\",\"place\":\"lab\"}},"
        "\"repeat\":{\"mode\":\"count\",\"count\":7},\"missed\":\"run_once\"},"
        "{\"id\":\"every\",\"enabled\":false,"
        "\"trigger\":{\"type\":\"schedule\",\"kind\":\"every\",\"interval_s\":120,"
        "\"first_at\":1700000000},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"tick\"}},"
        "{\"id\":\"at\",\"trigger\":{\"type\":\"schedule\",\"kind\":\"at\",\"at\":\"07:30:00\"},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"wake\"}},"
        "{\"id\":\"gpio\",\"trigger\":{\"type\":\"gpio\",\"pin\":9,\"level\":0},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"button\"}},"
        "{\"id\":\"mqtt\",\"trigger\":{\"type\":\"mqtt_message\",\"server_id\":\"home\","
        "\"topic\":\"sensor/*\",\"payload_contains\":\"hot\",\"cooldown_s\":30},"
        "\"action\":{\"kind\":\"prompt\",\"prompt\":\"heat\"}}"
        "]}";
    sched_tasks_t in, back;
    char why[96];
    size_t dropped = 0;
    TEST_ASSERT_EQUAL(ESP_OK, sched_parse(file, strlen(file), &in, why, sizeof why, &dropped));
    TEST_ASSERT_EQUAL_INT(0, (int)dropped);
    TEST_ASSERT_EQUAL_INT(5, (int)in.count);

    const sched_task_t *tpl = &in.tasks[0];
    TEST_ASSERT_EQUAL_STRING("two vars", tpl->description);
    TEST_ASSERT_EQUAL_INT(SCHED_ACTION_TEMPLATE, tpl->action);
    TEST_ASSERT_EQUAL_STRING("greet", tpl->template_name);
    TEST_ASSERT_EQUAL_INT(2, (int)tpl->vars_count);
    TEST_ASSERT_EQUAL_STRING("name", tpl->vars[0].name);
    TEST_ASSERT_EQUAL_STRING("ada", tpl->vars[0].value);
    TEST_ASSERT_EQUAL_STRING("place", tpl->vars[1].name);
    TEST_ASSERT_EQUAL_STRING("lab", tpl->vars[1].value);
    TEST_ASSERT_EQUAL_INT(SCHED_REPEAT_COUNT_MODE, tpl->repeat_mode);
    TEST_ASSERT_EQUAL_INT(7, tpl->repeat_count);
    TEST_ASSERT_EQUAL_INT(SCHED_MISSED_RUN_ONCE, tpl->missed);

    TEST_ASSERT_EQUAL_INT(SCHED_EVERY, in.tasks[1].kind);
    TEST_ASSERT_EQUAL_INT(120, in.tasks[1].interval_s);
    TEST_ASSERT_TRUE(in.tasks[1].has_first_at);
    TEST_ASSERT_EQUAL_INT64(1700000000, in.tasks[1].first_at);
    TEST_ASSERT_FALSE(in.tasks[1].enabled);
    TEST_ASSERT_EQUAL_INT(SCHED_AT, in.tasks[2].kind);
    TEST_ASSERT_EQUAL_STRING("07:30:00", in.tasks[2].at);
    TEST_ASSERT_EQUAL_INT(9, in.tasks[3].pin);
    TEST_ASSERT_EQUAL_INT(0, in.tasks[3].level);
    TEST_ASSERT_EQUAL_STRING("sensor/*", in.tasks[4].topic);
    TEST_ASSERT_EQUAL_STRING("hot", in.tasks[4].payload_contains);
    TEST_ASSERT_EQUAL_INT(30, in.tasks[4].cooldown_s);

    /* The stored document, re-read: a field the writer forgot would be gone here. */
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, sched_emit_json(&in, &doc));
    TEST_ASSERT_NOT_NULL(doc);
    static char text[4096];
    size_t text_len = 0;
    TEST_ASSERT_EQUAL(ESP_OK, json_emit(doc, text, sizeof text, &text_len));
    back.count = 0;
    TEST_ASSERT_EQUAL(ESP_OK, sched_parse(text, text_len, &back, why, sizeof why, &dropped));
    TEST_ASSERT_EQUAL_INT(0, (int)dropped);
    TEST_ASSERT_EQUAL_INT((int)in.count, (int)back.count);
    TEST_ASSERT_EQUAL_STRING("two vars", back.tasks[0].description);
    TEST_ASSERT_EQUAL_STRING("greet", back.tasks[0].template_name);
    TEST_ASSERT_EQUAL_INT(2, (int)back.tasks[0].vars_count);
    TEST_ASSERT_EQUAL_STRING("lab", back.tasks[0].vars[1].value);
    TEST_ASSERT_EQUAL_INT(7, back.tasks[0].repeat_count);
    TEST_ASSERT_EQUAL_INT(SCHED_MISSED_RUN_ONCE, back.tasks[0].missed);
    TEST_ASSERT_EQUAL_INT64(1700000000, back.tasks[1].first_at);
    TEST_ASSERT_FALSE(back.tasks[1].enabled);
    TEST_ASSERT_EQUAL_STRING("07:30:00", back.tasks[2].at);
    TEST_ASSERT_EQUAL_INT(9, back.tasks[3].pin);
    TEST_ASSERT_EQUAL_STRING("hot", back.tasks[4].payload_contains);
    TEST_ASSERT_EQUAL_INT(30, back.tasks[4].cooldown_s);
    json_free(doc);

    /* The list answer carries the same fields plus the runtime counters. */
    in.tasks[0].fired_count = 3;
    json_doc_t *listed = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, sched_list_json(&in, 1700000000, 1699999000, 0, &listed));
    TEST_ASSERT_NOT_NULL(listed);
    static char list_text[8192];
    size_t list_len = 0;
    TEST_ASSERT_EQUAL(ESP_OK, json_emit(listed, list_text, sizeof list_text, &list_len));
    TEST_ASSERT_TRUE(list_len > 0);
    /* `04` §4.4's result row is the authority: exactly the seven listed keys. The
     * action is not among them, so a `tasks.list` answer that carried the prompt
     * would leak the task's text to every reader of the screen. */
    TEST_ASSERT_NOT_NULL(strstr(list_text, "\"fired_count\":3"));
    TEST_ASSERT_NOT_NULL(strstr(list_text, "\"count\":7"));
    TEST_ASSERT_NOT_NULL(strstr(list_text, "\"description\":\"two vars\""));
    TEST_ASSERT_NOT_NULL(strstr(list_text, "\"name\":\"door.open\""));
    TEST_ASSERT_NOT_NULL(strstr(list_text, "\"interval_s\":120"));
    TEST_ASSERT_NULL(strstr(list_text, "\"action\""));
    TEST_ASSERT_NULL(strstr(list_text, "\"prompt\""));
    /* An enabled `every` task with a known clock reports its next instant; the
     * disabled one does not, because 0 would read as "now". */
    TEST_ASSERT_NOT_NULL(strstr(list_text, "\"next_fire_at\""));
    json_free(listed);
}

/* The field-level refusals the table above cannot reach, because a valid sibling
 * field has to be present for the key under test to be the one that fails. */
TEST_CASE("a required string, a wrong type and an oversize value each refuse", "[m5]")
{
    char why[96] = "";
    sched_task_t task;

    /* A task that is not an object at all: `03-config.md` §tasks is an array of
     * objects, and a bare string there must name the level, not a member. */
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      sched_task_validate(NULL, &task, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("task: not an object", why);

    TEST_ASSERT_EQUAL(-1, sched_parse_hhmmss(NULL));
    TEST_ASSERT_EQUAL(-1, sched_parse_hhmmss("7:00"));
    TEST_ASSERT_EQUAL(-1, sched_parse_hhmmss("07:00"));
    TEST_ASSERT_EQUAL(-1, sched_parse_hhmmss("25:00:00"));
    TEST_ASSERT_EQUAL(-1, sched_parse_hhmmss("00:61:00"));
    TEST_ASSERT_EQUAL(-1, sched_parse_hhmmss("aa:00:00"));
    TEST_ASSERT_EQUAL(0, sched_parse_hhmmss("00:00:00"));
    TEST_ASSERT_EQUAL(86399, sched_parse_hhmmss("23:59:59"));

    /* An ISO instant whose separators are right but whose digits are not is invalid,
     * not the epoch: the two must not be the same answer. */
    TEST_ASSERT_FALSE(sched_is_iso(NULL));
    TEST_ASSERT_FALSE(sched_is_iso("2025-11-1xT00:00:00Z"));
    TEST_ASSERT_FALSE(sched_is_iso("2025-13-01T00:00:00Z"));
    TEST_ASSERT_FALSE(sched_is_iso("2025-01-01T24:00:00Z"));
    TEST_ASSERT_TRUE(sched_is_iso("2025-01-01T00:00:00Z"));
}

TEST_CASE("an oversize prompt is refused by the ceiling the doc states", "[m5]")
{
    /* `03-config.md` §Action schema: `prompt` is 1..`agent.max_input_bytes`. A value
     * past the buffer is a size refusal, and one that is empty is a range refusal —
     * the two messages differ so the user knows which mistake they made. */
    char file[2 * SCHED_PROMPT_MAX];
    char prompt[SCHED_PROMPT_MAX + 64];
    char why[96];
    sched_tasks_t out;
    size_t dropped = 0;

    memset(prompt, 'a', sizeof prompt - 1);
    prompt[sizeof prompt - 1] = '\0';
    int written = snprintf(file, sizeof file,
                           "{\"version\":1,\"tasks\":[{\"id\":\"a\","
                           "\"trigger\":{\"type\":\"boot\"},"
                           "\"action\":{\"kind\":\"prompt\",\"prompt\":\"%s\"}}]}",
                           prompt);
    TEST_ASSERT_TRUE(written > 0 && (size_t)written < sizeof file);
    TEST_ASSERT_EQUAL(ESP_OK, sched_parse(file, (size_t)written, &out, why, sizeof why, &dropped));
    TEST_ASSERT_EQUAL_INT(1, (int)dropped);
    TEST_ASSERT_NOT_NULL(strstr(why, "action.prompt: must be"));

    written = snprintf(file, sizeof file,
                       "{\"version\":1,\"tasks\":[{\"id\":\"a\","
                       "\"trigger\":{\"type\":\"boot\"},"
                       "\"action\":{\"kind\":\"prompt\",\"prompt\":\"\"}}]}");
    TEST_ASSERT_EQUAL(ESP_OK, sched_parse(file, (size_t)written, &out, why, sizeof why, &dropped));
    TEST_ASSERT_EQUAL_INT(1, (int)dropped);
    TEST_ASSERT_NOT_NULL(strstr(why, "action.prompt: must be"));
}

TEST_CASE("vars beyond the declarations the struct holds are refused, not truncated", "[m5]")
{
    /* A truncated `vars` list would render a template with placeholders left in it,
     * so the ninth declaration is a refusal rather than a silent drop. */
    char file[2048];
    char why[96];
    sched_tasks_t out;
    size_t dropped = 0;
    char vars[1024];
    size_t at = 0;
    for (int i = 0; i < SCHED_MAX_VARS + 1; i++) {
        at = catf(vars, sizeof vars, at, "%s\"v%d\":\"x\"", i ? "," : "", i);
    }
    int written = snprintf(file, sizeof file,
                           "{\"version\":1,\"tasks\":[{\"id\":\"a\","
                           "\"trigger\":{\"type\":\"boot\"},"
                           "\"action\":{\"kind\":\"template\",\"template\":\"t\","
                           "\"vars\":{%s}}}]}",
                           vars);
    TEST_ASSERT_TRUE(written > 0 && (size_t)written < sizeof file);
    TEST_ASSERT_EQUAL(ESP_OK, sched_parse(file, (size_t)written, &out, why, sizeof why, &dropped));
    TEST_ASSERT_EQUAL_INT(1, (int)dropped);
    TEST_ASSERT_NOT_NULL(strstr(why, "at most 8 declarations"));

    /* A name past `SCHED_VAR_NAME_MAX` is the same class of refusal. */
    char long_name[80];
    memset(long_name, 'n', sizeof long_name - 1);
    long_name[sizeof long_name - 1] = '\0';
    written = snprintf(file, sizeof file,
                       "{\"version\":1,\"tasks\":[{\"id\":\"a\","
                       "\"trigger\":{\"type\":\"boot\"},"
                       "\"action\":{\"kind\":\"template\",\"template\":\"t\","
                       "\"vars\":{\"%s\":\"x\"}}}]}",
                       long_name);
    TEST_ASSERT_TRUE(written > 0 && (size_t)written < sizeof file);
    TEST_ASSERT_EQUAL(ESP_OK, sched_parse(file, (size_t)written, &out, why, sizeof why, &dropped));
    TEST_ASSERT_EQUAL_INT(1, (int)dropped);
    TEST_ASSERT_EQUAL_STRING("action.vars: name or value too long", why);
}

TEST_CASE("a bare trigger matches only enabled tasks of its own kind", "[m5]")
{
    sched_tasks_t tasks;
    memset(&tasks, 0, sizeof tasks);
    tasks.count = 3;
    tasks.tasks[0].type = SCHED_TRIGGER_BOOT;
    tasks.tasks[0].enabled = true;
    tasks.tasks[1].type = SCHED_TRIGGER_BOOT;
    tasks.tasks[1].enabled = false;
    tasks.tasks[2].type = SCHED_TRIGGER_BOOT;
    tasks.tasks[2].enabled = true;

    sched_hit_t hits[4];
    /* A cap of one proves the loop stops rather than overrunning the caller's array. */
    TEST_ASSERT_EQUAL_INT(1, (int)sched_match_bare(&tasks, SCHED_TRIGGER_BOOT, hits, 1));
    TEST_ASSERT_EQUAL_INT(0, (int)hits[0].index);
    TEST_ASSERT_EQUAL_INT(2, (int)sched_match_bare(&tasks, SCHED_TRIGGER_BOOT, hits, 4));
    TEST_ASSERT_EQUAL_INT(0, (int)hits[0].index);
    TEST_ASSERT_EQUAL_INT(2, (int)hits[1].index);
    TEST_ASSERT_EQUAL_INT(-1, hits[1].level);
    TEST_ASSERT_EQUAL_INT(0, (int)sched_match_bare(&tasks, SCHED_TRIGGER_WIFI_CONNECTED,
                                                   hits, 4));
    TEST_ASSERT_EQUAL_INT(0, (int)sched_match_bare(NULL, SCHED_TRIGGER_BOOT, hits, 4));
}

TEST_CASE("repeat allows is a rule about the fired count", "[m5]")
{
    sched_task_t task;
    memset(&task, 0, sizeof task);

    TEST_ASSERT_FALSE(sched_repeat_allows(NULL));

    task.repeat_mode = SCHED_REPEAT_ONCE;
    TEST_ASSERT_TRUE(sched_repeat_allows(&task));
    task.fired_count = 1;
    TEST_ASSERT_FALSE(sched_repeat_allows(&task));

    task.repeat_mode = SCHED_REPEAT_COUNT_MODE;
    task.repeat_count = 3;
    TEST_ASSERT_TRUE(sched_repeat_allows(&task));
    task.fired_count = 3;
    TEST_ASSERT_FALSE(sched_repeat_allows(&task));

    task.repeat_mode = SCHED_REPEAT_FOREVER;
    task.fired_count = 100000;
    TEST_ASSERT_TRUE(sched_repeat_allows(&task));

    /* An unset mode is not a licence to run: an enum the parser can never produce
     * must still answer false, never fall through to `forever`'s true. */
    task.repeat_mode = (sched_repeat_t)99;
    TEST_ASSERT_FALSE(sched_repeat_allows(&task));
}

TEST_CASE("an emitter that is handed no document refuses instead of writing", "[m5]")
{
    sched_tasks_t tasks;
    memset(&tasks, 0, sizeof tasks);
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, sched_emit_json(NULL, &doc));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, sched_emit_json(&tasks, NULL));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, sched_list_json(NULL, 0, 0, 0, &doc));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, sched_list_json(&tasks, 0, 0, 0, NULL));
}
