/* The config schema and the secret rules, asserted through `cfg.h` only.
 * Every expectation names its rule: `03-config.md` §Full schema and validation
 * steps 3-9, `04-protocol.md` §4.10, `06-security.md` §secret handling. Pure
 * functions, so no board and no filesystem. */

#include <stdio.h>
#include <string.h>

#include "unity.h"

#include "cfg.h"
#include "store.h"

/* The smallest document the schema accepts: one network and one provider, both
 * with a secret reference. */
static const char *k_minimal =
    "{\"version\":1,"
    "\"wifi\":{\"networks\":[{\"ssid\":\"home\",\"password_ref\":\"nvs:wifi_psk\"}]},"
    "\"llm\":{\"providers\":[{\"id\":\"openai\",\"base_url\":\"https://api.example/v1\","
    "\"model\":\"m\",\"api_key_ref\":\"nvs:llm_key\"}]}}";

static esp_err_t validate(const char *src, char *why, size_t cap)
{
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(src, strlen(src), &doc));
    esp_err_t err = cfg_validate(doc, why, cap);
    json_free(doc);
    return err;
}

TEST_CASE("the minimal document is valid", "[cfg]")
{
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_OK, validate(k_minimal, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("", why);
}

TEST_CASE("a wrong version is refused before anything else", "[cfg]")
{
    /* Step 3: the version gate runs first, so the message names it and not the
     * first other thing that happens to be wrong. */
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate("{\"version\":2}", why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "version"));
}

TEST_CASE("an unknown key is refused so a typo cannot be ignored", "[cfg]")
{
    /* Step 4: the tree is frozen, so a key nobody defined is a refusal rather
     * than a silently stored setting. */
    char why[96] = "";
    const char *src =
        "{\"version\":1,"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
        "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]},"
        "\"llm.mode\":\"fast\"}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "llm.mode"));
}

TEST_CASE("a bound is enforced on both ends", "[cfg]")
{
    char why[96] = "";
    /* `serial.max_line_bytes` is 512..4096; 511 is below and 4097 above. */
    const char *low =
        "{\"version\":1,\"serial\":{\"max_line_bytes\":511},"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
        "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(low, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "max_line_bytes"));

    const char *high =
        "{\"version\":1,\"serial\":{\"max_line_bytes\":4097},"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
        "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(high, why, sizeof why));
}

TEST_CASE("an input budget above the RX line is refused", "[cfg]")
{
    /* `03` note 1: the agent cannot be handed more than the link can deliver. */
    char why[96] = "";
    const char *src =
        "{\"version\":1,"
        "\"serial\":{\"max_line_bytes\":512},"
        "\"agent\":{\"max_input_bytes\":4096},"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
        "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "max_input_bytes"));
}

/* The minimal document plus top-level members, so a mutation test still carries the
 * required keys and pins only the rule it names. */
/* The schema tests name root members (`"device":{...}`); the `extra_body` tests
 * name extra_body members (`{"reasoning_effort":"high"}`). Two helpers because the
 * two jobs splice at different depths, and a single one that guessed from the
 * argument's first byte would be a rule the tests are not asserting. 768, not 512,
 * so a member list plus the surrounding document cannot truncate. */
static esp_err_t validate_root(const char *members, char *why, size_t cap)
{
    char src[768];
    int n = snprintf(src, sizeof src,
                     "{\"version\":1,"
                     "\"wifi\":{\"networks\":[{\"ssid\":\"home\","
                     "\"password_ref\":\"nvs:wifi_psk\"}]},"
                     "\"llm\":{\"providers\":[{\"id\":\"openai\","
                     "\"base_url\":\"https://api.example/v1\",\"model\":\"m\","
                     "\"api_key_ref\":\"nvs:llm_key\"}]},%s}",
                     members);
    TEST_ASSERT_LESS_THAN((int)sizeof src, n);
    return validate(src, why, cap);
}

static esp_err_t validate_refusal(const char *members, char *why, size_t cap)
{
    char src[768];
    int n = snprintf(src, sizeof src,
                     "{\"version\":1,"
                     "\"wifi\":{\"networks\":[{\"ssid\":\"home\","
                     "\"password_ref\":\"nvs:wifi_psk\"}]},"
                     "\"llm\":{\"providers\":[{\"id\":\"openai\","
                     "\"base_url\":\"https://api.example/v1\",\"model\":\"m\","
                     "\"api_key_ref\":\"nvs:llm_key\","
                     "\"extra_body\":%s}]}}",
                     members);
    TEST_ASSERT_LESS_THAN((int)sizeof src, n);
    return validate(src, why, cap);
}

TEST_CASE("a boolean key takes only true or false", "[cfg]")
{
    /* `03-config.md` §Full schema: `tools.allow_dangerous` is a bool, and a string
     * `"true"` would be read as false by a caller that trusts the type. */
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_OK,
                      validate_root("\"tools\":{\"allow_dangerous\":true}", why,
                                     sizeof why));
    TEST_ASSERT_EQUAL_STRING("", why);

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_root("\"tools\":{\"allow_dangerous\":\"true\"}", why,
                                     sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "allow_dangerous"));
}

TEST_CASE("a string key's length bound is enforced", "[cfg]")
{
    /* `device.name` is 1..32 characters, so neither end is free. */
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_root("\"device\":{\"name\":\"\"}", why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "device.name"));

    const char *long_name =
        "\"device\":{\"name\":\"0123456789012345678901234567890123\"}"; /* 34 */
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_root(long_name, why, sizeof why));

    /* The bound is inclusive: 32 is the longest accepted name. */
    const char *edge =
        "\"device\":{\"name\":\"01234567890123456789012345678901\"}"; /* 32 */
    TEST_ASSERT_EQUAL(ESP_OK, validate_root(edge, why, sizeof why));
}

TEST_CASE("an enum key takes a listed member only", "[cfg]")
{
    /* Step 7: membership is matched whole, so a prefix of a member is not a member. */
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_OK,
                      validate_root("\"device\":{\"log_level\":\"info\"}", why,
                                     sizeof why));
    TEST_ASSERT_EQUAL(ESP_OK,
                      validate_root("\"device\":{\"log_level\":\"none\"}", why,
                                     sizeof why));

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_root("\"device\":{\"log_level\":\"inf\"}", why,
                                     sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "log_level"));

    /* A number is not a member, and must not be read as a string. */
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_root("\"device\":{\"log_level\":5}", why, sizeof why));
}

TEST_CASE("an array element's type and range are enforced", "[cfg]")
{
    /* A table row can say "array of 0..8" but not "array of strings". */
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_OK,
                      validate_root("\"ntp\":{\"servers\":[\"pool.ntp.org\"]}", why,
                                     sizeof why));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_root("\"ntp\":{\"servers\":[1]}", why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "ntp.servers"));

    TEST_ASSERT_EQUAL(ESP_OK,
                      validate_root("\"tools\":{\"http_allow_hosts\":[\"a.example\"]}",
                                     why, sizeof why));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_root("\"tools\":{\"http_allow_hosts\":[true]}", why,
                                     sizeof why));

    /* `wifi.retry_backoff_ms` is an array of 100..60000 ms integers. The whole
     * document is written out, not spliced: the member lives inside `wifi`, and a
     * second `wifi` key would be the duplicate cJSON resolves to the first one, so
     * the test would pass without ever reaching the row it names. */
    TEST_ASSERT_EQUAL(ESP_OK,
                      validate("{\"version\":1,"
                              "\"wifi\":{\"networks\":[{\"ssid\":\"h\","
                              "\"password_ref\":\"nvs:k\"}],"
                              "\"retry_backoff_ms\":[100,60000]},"
                              "\"llm\":{\"providers\":[{\"id\":\"p\","
                              "\"base_url\":\"http://h\",\"model\":\"m\","
                              "\"api_key_ref\":\"nvs:k\"}]}}",
                              why, sizeof why));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate("{\"version\":1,"
                              "\"wifi\":{\"networks\":[{\"ssid\":\"h\","
                              "\"password_ref\":\"nvs:k\"}],"
                              "\"retry_backoff_ms\":[99]},"
                              "\"llm\":{\"providers\":[{\"id\":\"p\","
                              "\"base_url\":\"http://h\",\"model\":\"m\","
                              "\"api_key_ref\":\"nvs:k\"}]}}",
                              why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "retry_backoff_ms"));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate("{\"version\":1,"
                              "\"wifi\":{\"networks\":[{\"ssid\":\"h\","
                              "\"password_ref\":\"nvs:k\"}],"
                              "\"retry_backoff_ms\":[1000,\"fast\"]},"
                              "\"llm\":{\"providers\":[{\"id\":\"p\","
                              "\"base_url\":\"http://h\",\"model\":\"m\","
                              "\"api_key_ref\":\"nvs:k\"}]}}",
                              why, sizeof why));
}

TEST_CASE("a required key is refused when the whole group is absent", "[cfg]")
{
    /* Step 6 runs over the table, not over what the document happens to contain, so
     * a config with no `wifi` block is refused by name rather than passing as empty. */
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate("{\"version\":1}", why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "wifi.networks"));
}

TEST_CASE("an id is unique and dot-free because it becomes a namespace", "[cfg]")
{
    /* `03` §Full schema note 4: `llm.providers[].id` is a wire namespace, so a dot
     * would make `tool.0.5` ambiguous. */
    char why[96] = "";
    const char *dotted =
        "{\"version\":1,"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":[{\"id\":\"open.ai\",\"base_url\":\"http://h\","
        "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(dotted, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "llm.providers"));

    const char *twice =
        "{\"version\":1,"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":["
        "{\"id\":\"p\",\"base_url\":\"http://h\",\"model\":\"m\","
        "\"api_key_ref\":\"nvs:k\"},"
        "{\"id\":\"p\",\"base_url\":\"http://h\",\"model\":\"m\","
        "\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(twice, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "duplicate"));
}

TEST_CASE("a bare secret in config.json is refused", "[cfg]")
{
    /* `03` §config.json rule 3: a credential in the file is a ref or nothing. */
    char why[96] = "";
    const char *src =
        "{\"version\":1,"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"hunter2\"}]},"
        "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
        "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "password_ref"));
}

TEST_CASE("a ref is nvs: plus 1 to 15 identifier bytes", "[cfg]")
{
    /* `04` §4.10 key grammar, and the bound NVS itself enforces. */
    TEST_ASSERT_TRUE(cfg_ref_is_valid("nvs:a"));
    TEST_ASSERT_TRUE(cfg_ref_is_valid("nvs:llm_key_1"));
    TEST_ASSERT_TRUE(cfg_ref_is_valid("nvs:ABCDEFGHIJKLMNO")); /* 15 */
    TEST_ASSERT_FALSE(cfg_ref_is_valid("nvs:ABCDEFGHIJKLMNOP")); /* 16 */
    TEST_ASSERT_FALSE(cfg_ref_is_valid("nvs:"));
    TEST_ASSERT_FALSE(cfg_ref_is_valid("nvs:has-dash"));
    TEST_ASSERT_FALSE(cfg_ref_is_valid("NVS:key"));
    TEST_ASSERT_FALSE(cfg_ref_is_valid(NULL));
}

TEST_CASE("a duplicated provider id is refused", "[cfg]")
{
    /* Note 4: the id is a namespace on the wire, so two providers with one id make
     * an ambiguous reply. */
    char why[96] = "";
    const char *src =
        "{\"version\":1,"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":["
        "{\"id\":\"p\",\"base_url\":\"http://h\",\"model\":\"m\",\"api_key_ref\":\"nvs:k\"},"
        "{\"id\":\"p\",\"base_url\":\"http://h\",\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}"
        "]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "duplicate"));
}

TEST_CASE("a provider without a model is refused", "[cfg]")
{
    char why[96] = "";
    const char *src =
        "{\"version\":1,"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
        "\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "model"));
}

TEST_CASE("a base url must be http or https", "[cfg]")
{
    char why[96] = "";
    const char *src =
        "{\"version\":1,"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"ftp://h\","
        "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
}

TEST_CASE("an empty network list is refused", "[cfg]")
{
    /* `03` §Full schema: at least one network, or the device cannot get online. */
    char why[96] = "";
    const char *src =
        "{\"version\":1,\"wifi\":{\"networks\":[]},"
        "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
        "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
}

/* `03-config.md` §llm `extra_body`: at most four members, flat scalars only, and none
 * of `model`/`messages`/`tools`/`stream`. The table's `CFG_OBJECT` row proves only
 * that the value is an object, so before this the rules were decorative: the device
 * accepted a fifth member, a nested object and a `stream` override, and the provider
 * answered 400 on the next prompt instead of the write naming what was wrong. */

TEST_CASE("extra_body accepts one flat member", "[cfg]")
{
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_OK,
                      validate_refusal("{\"reasoning_effort\":\"high\"}", why, sizeof why));
    TEST_ASSERT_EQUAL(ESP_OK, validate_refusal("{\"temperature\":0.2}", why, sizeof why));
}

TEST_CASE("a fifth extra_body member is refused", "[cfg]")
{
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_refusal("{\"a\":1,\"b\":2,\"c\":3,\"d\":4,\"e\":5}",
                                     why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "4"));
}

TEST_CASE("extra_body refuses the four reserved keys", "[cfg]")
{
    /* A member named `stream` or `tools` rewrites the request's structure, which no
     * provider accepts and no reader intended. */
    char why[96] = "";
    const char *reserved[] = { "model", "messages", "tools", "stream" };
    for (size_t i = 0; i < sizeof reserved / sizeof reserved[0]; i++) {
        char extra[64];
        snprintf(extra, sizeof extra, "{\"%s\":\"x\"}", reserved[i]);
        TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate_refusal(extra, why, sizeof why));
        TEST_ASSERT_NOT_NULL(strstr(why, reserved[i]));
    }
}

TEST_CASE("extra_body refuses a nested member", "[cfg]")
{
    /* Flat scalars only: the merge copies members into the request root, and a nested
     * object is exactly the shape Anthropic's thinking member wants, which
     * `03-config.md` says this field cannot express. */
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_refusal("{\"thinking\":{\"type\":\"enabled\"}}",
                                     why, sizeof why));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      validate_refusal("{\"stop\":[\"a\"]}", why, sizeof why));
}

TEST_CASE("redaction replaces every occurrence and never grows the line", "[cfg]")
{
    /* `06-security.md` §secret handling: a secret leaves the device once. */
    char line[128];
    const char *secrets[2] = { "sk-SECRET", "hunter2" };

    snprintf(line, sizeof line, "key=sk-SECRET and again sk-SECRET");
    TEST_ASSERT_TRUE(cfg_redact(line, sizeof line, secrets, 2));
    TEST_ASSERT_EQUAL_STRING("key=[redacted] and again [redacted]", line);

    snprintf(line, sizeof line, "pw=hunter2");
    TEST_ASSERT_TRUE(cfg_redact(line, sizeof line, secrets, 2));
    TEST_ASSERT_EQUAL_STRING("pw=[redacted]", line);

    snprintf(line, sizeof line, "nothing to hide");
    TEST_ASSERT_FALSE(cfg_redact(line, sizeof line, secrets, 2));
    TEST_ASSERT_EQUAL_STRING("nothing to hide", line);

    /* An empty secret would match at every offset; it is skipped, not applied. */
    const char *empty[1] = { "" };
    snprintf(line, sizeof line, "a line");
    TEST_ASSERT_FALSE(cfg_redact(line, sizeof line, empty, 1));
    TEST_ASSERT_EQUAL_STRING("a line", line);
}

TEST_CASE("redaction keeps the buffer terminated when the mask does not fit", "[cfg]")
{
    /* A caller with a full buffer must get a shorter string, never a write past
     * the end. */
    char line[20];
    const char *secrets[1] = { "SECRET" };
    snprintf(line, sizeof line, "0123456789SECRET");
    cfg_redact(line, sizeof line, secrets, 1);
    TEST_ASSERT_LESS_THAN(sizeof line, strlen(line) + 1);
    TEST_ASSERT_NOT_NULL(strchr(line, '\0'));
}

/* `04` §4.9: a set creates a missing object parent, so `ntp.timezone` lands on a
 * document with no `ntp` block at all. That is the `/onboard` write on a fresh
 * device, where `config.json` has no `ntp` yet. */
TEST_CASE("a set creates a missing object parent", "[cfg]")
{
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(k_minimal, strlen(k_minimal), &doc));

    json_doc_t *value = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse("\"CET-1CEST\"", 11, &value));
    TEST_ASSERT_TRUE(cfg_set(doc, "ntp.timezone", json_root(value)));
    TEST_ASSERT_EQUAL_STRING("CET-1CEST",
                             json_string(json_path_get(doc, "ntp.timezone")));

    /* Two levels deep, and the existing siblings are untouched. */
    TEST_ASSERT_TRUE(cfg_set(doc, "a.b.c", json_root(value)));
    TEST_ASSERT_EQUAL_STRING("CET-1CEST", json_string(json_path_get(doc, "a.b.c")));
    TEST_ASSERT_NOT_NULL(json_path_get(doc, "llm"));

    json_free(value);
    json_free(doc);
}

/* The other half of §4.9, and the reason the rule is "object parent" and not
 * "create anything": an array index implies an array whose other elements are not
 * known, and `03-config.md` requires each `wifi.networks[]` entry complete. */
/* `04` §4.9: a set may create a missing object parent, but a numeric segment with
 * no array behind it creates nothing — the whole write is refused, not partially
 * applied. Dotted syntax is the only path form the parser accepts. */
TEST_CASE("a set under an absent array creates nothing at all", "[cfg]")
{
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse("{\"version\":1}", 13, &doc));
    json_doc_t *val_doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse("{\"a\":1}", 7, &val_doc));

    TEST_ASSERT_FALSE(cfg_set(doc, "wifi.networks.0.ssid", json_root(val_doc)));
    /* The refusal is total: `wifi` was not invented as a side effect. */
    TEST_ASSERT_NULL(json_path_get(doc, "wifi"));

    json_free(val_doc);
    json_free(doc);
}

TEST_CASE("an id is unique and dot-free", "[cfg]")
{
    char why[96] = "";
    const char *dotted =
        "{\"version\":1,"
        "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
        "\"llm\":{\"providers\":[{\"id\":\"open.ai\",\"base_url\":\"http://h\","
        "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]}}";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(dotted, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "llm.providers"));
}
TEST_CASE("a set never extends an existing array beyond its last index", "[cfg]")
{
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(k_minimal, strlen(k_minimal), &doc));

    json_doc_t *value = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse("\"other\"", 7, &value));

    /* Index 3 of a one-entry array: no array, no element, and nothing written. */
    TEST_ASSERT_FALSE(cfg_set(doc, "wifi.networks.3.ssid", json_root(value)));
    TEST_ASSERT_NULL(json_path_get(doc, "wifi.networks.3"));
    TEST_ASSERT_EQUAL(1, (int)json_array_len(json_path_get(doc, "wifi.networks")));

    /* A missing array is not created either, even though its parent exists. */
    TEST_ASSERT_FALSE(cfg_set(doc, "ntp.servers.0", json_root(value)));
    TEST_ASSERT_NULL(json_path_get(doc, "ntp"));

    /* The index that does exist still takes the write. */
    TEST_ASSERT_TRUE(cfg_set(doc, "wifi.networks.0.ssid", json_root(value)));
    TEST_ASSERT_EQUAL_STRING("other",
                             json_string(json_path_get(doc, "wifi.networks.0.ssid")));
    TEST_ASSERT_EQUAL(1, (int)json_array_len(json_path_get(doc, "wifi.networks")));

    json_free(value);
    json_free(doc);
}

/* `04` §4.2: a set to a boot-only field reports `requires_reboot`, and the flag is
 * derived from the path rather than hardcoded. `ntp.timezone` is the interesting
 * negative: `clock_init` applies it and `reload` re-runs `clock_init`. */
TEST_CASE("requires_reboot follows the path prefix", "[cfg]")
{
    TEST_ASSERT_TRUE(cfg_needs_reboot("wifi.networks"));
    TEST_ASSERT_TRUE(cfg_needs_reboot("wifi.connect_timeout_ms"));
    TEST_ASSERT_TRUE(cfg_needs_reboot("serial.baud"));
    TEST_ASSERT_TRUE(cfg_needs_reboot("ntp.servers"));

    TEST_ASSERT_FALSE(cfg_needs_reboot("ntp.timezone"));
    TEST_ASSERT_FALSE(cfg_needs_reboot("llm.providers"));
    TEST_ASSERT_FALSE(cfg_needs_reboot("device.name"));
    TEST_ASSERT_FALSE(cfg_needs_reboot(NULL));

    /* A prefix, not a substring: `xwifi.x` is not the `wifi` block. */
    TEST_ASSERT_FALSE(cfg_needs_reboot("xwifi.networks"));
}

/* `04` §4.9: `applied` is true only when the running firmware took the value, so the
 * paths whose value it holds a copy of have to be named. `llm.*` is the whole set:
 * `llm_init` copies the chain and `config.set` re-runs it. */
TEST_CASE("a path whose value the firmware copies is not applied by writing", "[cfg]")
{
    TEST_ASSERT_TRUE(cfg_is_live_path("llm.providers"));
    TEST_ASSERT_TRUE(cfg_is_live_path("llm.providers.0.model"));
    TEST_ASSERT_TRUE(cfg_is_live_path("llm.budget.hour"));

    TEST_ASSERT_FALSE(cfg_is_live_path("ntp.timezone"));
    TEST_ASSERT_FALSE(cfg_is_live_path("device.name"));
    TEST_ASSERT_FALSE(cfg_is_live_path(NULL));

    /* A prefix, not a substring: an unrelated path starting with the same letters is
     * not the `llm` block. */
    TEST_ASSERT_FALSE(cfg_is_live_path("llmx.providers"));
}

TEST_CASE("a config round-trips through the canonical writer", "[cfg]")
{
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(k_minimal, strlen(k_minimal), &doc));
    char out[1024];
    size_t written = 0;
    TEST_ASSERT_EQUAL(ESP_OK, cfg_emit(doc, out, sizeof out, &written));
    TEST_ASSERT_EQUAL(strlen(out), written);

    json_doc_t *again = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(out, written, &again));
    /* The writer sorts keys, so a second pass is byte-identical: the file the
     * device writes is the file it will read back (`AGENTS.md` rule 4). */
    char twice[1024];
    size_t re_written = 0;
    TEST_ASSERT_EQUAL(ESP_OK, cfg_emit(again, twice, sizeof twice, &re_written));
    TEST_ASSERT_EQUAL_STRING(out, twice);
    TEST_ASSERT_EQUAL(ESP_OK, cfg_validate(again, NULL, 0));
    json_free(again);
    json_free(doc);
}

/* --- The loaded document (`cfg_doc.c`) ----------------------------------- */

TEST_CASE("an adopted document is readable by dotted path and cloneable", "[cfg]")
{
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(k_minimal, strlen(k_minimal), &doc));
    cfg_adopt(doc);
    TEST_ASSERT_TRUE(cfg_loaded());

    const char *model = json_string(cfg_get("llm.providers.0.model"));
    TEST_ASSERT_NOT_NULL(model);
    TEST_ASSERT_EQUAL_STRING("m", model);
    /* A key that is not there is NULL, never a borrowed neighbour's value. */
    TEST_ASSERT_NULL(cfg_get("llm.providers.9.model"));
    TEST_ASSERT_NULL(cfg_get(""));

    json_doc_t *copy = cfg_clone();
    TEST_ASSERT_NOT_NULL(copy);
    /* The clone is independent: mutating it leaves the loaded document alone. */
    json_node_t *value = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse("\"other\"", 7, &doc));
    value = json_root(doc);
    TEST_ASSERT_TRUE(cfg_set(copy, "llm.providers.0.model", value));
    TEST_ASSERT_EQUAL_STRING("m", json_string(cfg_get("llm.providers.0.model")));
    TEST_ASSERT_EQUAL_STRING("other", json_string(json_path_get(copy, "llm.providers.0.model")));
    json_free(doc);
    json_free(copy);
    cfg_unload();
}

TEST_CASE("an unloaded document reads as absent, not as empty", "[cfg]")
{
    cfg_unload();
    TEST_ASSERT_FALSE(cfg_loaded());
    TEST_ASSERT_NULL(cfg_get("device.name"));
    TEST_ASSERT_NULL(cfg_clone());
}

TEST_CASE("adopting a second document releases the first", "[cfg]")
{
    /* `reload` replaces the document whole; the old one must not leak and must not
     * stay reachable through a stale pointer. */
    json_doc_t *first = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(k_minimal, strlen(k_minimal), &first));
    cfg_adopt(first);

    const char *second_src = "{\"version\":1,\"device\":{\"name\":\"fresh\"}}";
    json_doc_t *second = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(second_src, strlen(second_src), &second));
    cfg_adopt(second);

    TEST_ASSERT_EQUAL_STRING("fresh", json_string(cfg_get("device.name")));
    TEST_ASSERT_NULL(cfg_get("llm"));
    cfg_unload();
}

/* --- Redaction (`06-security.md` §secret handling) ------------------------ */

TEST_CASE("every occurrence of a secret is masked and the tail survives", "[cfg]")
{
    /* The offset bug this catches: a moving pointer plus its own offset wrote past
     * the match and left a stale tail, so a line with two hits lost the text after
     * them and kept garbage where the mask should have ended. */
    char line[128];
    snprintf(line, sizeof line, "key=%s and again %s tail", "s3cret", "s3cret");
    const char *secrets[] = {"s3cret"};
    TEST_ASSERT_TRUE(cfg_redact(line, sizeof line, secrets, 1));
    TEST_ASSERT_EQUAL_STRING("key=[redacted] and again [redacted] tail", line);
    TEST_ASSERT_NULL(strstr(line, "s3cret"));
}

TEST_CASE("redaction leaves a line without the secret untouched", "[cfg]")
{
    char line[64] = "nothing to hide";
    const char *secrets[] = {"s3cret"};
    TEST_ASSERT_FALSE(cfg_redact(line, sizeof line, secrets, 1));
    TEST_ASSERT_EQUAL_STRING("nothing to hide", line);
}

TEST_CASE("an empty secret is skipped rather than shredding the line", "[cfg]")
{
    /* An empty needle matches at offset 0 of every line, so redacting it would
     * replace the whole string. */
    char line[64] = "plain text";
    const char *secrets[] = {""};
    TEST_ASSERT_FALSE(cfg_redact(line, sizeof line, secrets, 1));
    TEST_ASSERT_EQUAL_STRING("plain text", line);
    TEST_ASSERT_FALSE(cfg_redact(line, sizeof line, NULL, 1));
}

TEST_CASE("a mask that cannot fit leaves the buffer terminated", "[cfg]")
{
    char line[16] = "xx s3cret yy";
    const char *secrets[] = {"s3cret"};
    TEST_ASSERT_TRUE(cfg_redact(line, sizeof line, secrets, 1));
    TEST_ASSERT_EQUAL(strlen(line), strnlen(line, sizeof line));
    TEST_ASSERT_NULL(strstr(line, "s3cret"));
}

TEST_CASE("the shipped userfs seed config passes the schema", "[cfg][docs]")
{
    /* A fresh device boots on the seed image, so a seed the schema refuses is a
     * device that never leaves safe mode. Read from the repo rather than copied
     * here: a copy would keep passing after the real seed drifted. */
    const char *path = USERFS_SEED_CONFIG_PATH;
    FILE *f = fopen(path, "rb");
    if (f == NULL) TEST_IGNORE_MESSAGE("seed config not reachable");
    static char src[8192];
    size_t n = fread(src, 1, sizeof src - 1, f);
    fclose(f);
    src[n] = '\0';
    char why[96] = "";
    TEST_ASSERT_EQUAL(ESP_OK, validate(src, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("", why);
}

/* `03-config.md` §tools: the three keys the doc's own example carries. Before these
 * rows existed the example was refused as unknown keys, so a documented config could
 * not load. */
TEST_CASE("the i2c pins and the task ceiling are known keys, bounded", "[cfg]")
{
    char why[96] = "";
    char src[640];

    snprintf(src, sizeof src,
             "{\"version\":1,"
             "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
             "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
             "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]},"
             "\"tools\":{\"i2c_sda\":5,\"i2c_scl\":6,\"task_max\":16}}");
    TEST_ASSERT_EQUAL(ESP_OK, validate(src, why, sizeof why));

    /* `01-hardware.md` §6.1: GPIO3 is a strapping pin between two usable ones, so the
     * allowlist is a set and the check cannot be a range. */
    snprintf(src, sizeof src,
             "{\"version\":1,"
             "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
             "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
             "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]},"
             "\"tools\":{\"i2c_sda\":3,\"i2c_scl\":6}}");
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "tools.i2c_sda"));

    /* One pin alone leaves the bus with one wire. */
    snprintf(src, sizeof src,
             "{\"version\":1,"
             "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
             "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
             "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]},"
             "\"tools\":{\"i2c_sda\":5}}");
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "set together"));

    /* The same pin twice is not a bus. */
    snprintf(src, sizeof src,
             "{\"version\":1,"
             "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
             "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
             "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]},"
             "\"tools\":{\"i2c_sda\":6,\"i2c_scl\":6}}");
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "must differ"));

    /* `task_max` may not exceed the schema's own 16-entry array cap. */
    snprintf(src, sizeof src,
             "{\"version\":1,"
             "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
             "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
             "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]},"
             "\"tools\":{\"task_max\":17}}");
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate(src, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "tools.task_max"));
}

TEST_CASE("an optional key set to null reads as absent", "[cfg]")
{
    /* `03-config.md` §Default / reject matrix. `tools.i2c_sda` defaults to null, so
     * without this rule the documented default is a document the device refuses. */
    char why[96] = "";
    char src[640];
    snprintf(src, sizeof src,
             "{\"version\":1,"
             "\"wifi\":{\"networks\":[{\"ssid\":\"a\",\"password_ref\":\"nvs:k\"}]},"
             "\"llm\":{\"providers\":[{\"id\":\"p\",\"base_url\":\"http://h\","
             "\"model\":\"m\",\"api_key_ref\":\"nvs:k\"}]},"
             "\"tools\":{\"i2c_sda\":null,\"i2c_scl\":null,\"task_max\":null}}");
    TEST_ASSERT_EQUAL(ESP_OK, validate(src, why, sizeof why));

    /* A required key may not be null: `version` is checked before the walk, and a
     * null there cannot satisfy `== 1`. */
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, validate("{\"version\":null}", why, sizeof why));
}
