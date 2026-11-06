/* The config schema and the secret rules, asserted through `cfg.h` only.
 * Every expectation names its rule: `03-config.md` §Full schema and validation
 * steps 3-9, `04-protocol.md` §4.10, `06-security.md` §secret handling. Pure
 * functions, so no board and no filesystem. */

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

TEST_CASE("a set never creates a parent key", "[cfg]")
{
    /* `04` §4.9: a typo in a path is E_CONFIG, not a new key in the file. */
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(k_minimal, strlen(k_minimal), &doc));

    json_doc_t *value = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse("\"gpt-4o\"", 8, &value));
    TEST_ASSERT_FALSE(cfg_set(doc, "llm.missing.model", json_root(value)));
    TEST_ASSERT_TRUE(cfg_set(doc, "llm.providers.0.model", json_root(value)));

    TEST_ASSERT_EQUAL_STRING("gpt-4o",
                             json_string(json_path_get(doc, "llm.providers.0.model")));
    json_free(value);
    json_free(doc);
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
