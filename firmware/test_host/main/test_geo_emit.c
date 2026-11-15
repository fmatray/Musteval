/* `geo_add_place` — the one place the place-and-locale member names are written
 * (`04-protocol.md` §4.13). Two surfaces report them, so the rule that matters is
 * the one both share: what the fix does not carry is not emitted.
 *
 * Host-testable because it is pure JSON emission; the provider that fills the fix is
 * `geo_parse`'s business and is tested in `test_m6.c`. */
#include "unity.h"

#include "geo.h"
#include "store.h"

#include <string.h>

TEST_CASE("geo_add_place emits every member the provider sent", "[geo]")
{
    geo_fix_t fix = {0};
    strcpy(fix.city, "Paris");
    strcpy(fix.region, "Ile-de-France");
    strcpy(fix.region_code, "IDF");
    strcpy(fix.country, "France");
    strcpy(fix.country_code, "FR");
    strcpy(fix.country_code_iso3, "FRA");
    strcpy(fix.country_capital, "Paris");
    strcpy(fix.country_tld, ".fr");
    strcpy(fix.continent_code, "EU");
    fix.in_eu = 1;
    strcpy(fix.postal, "75001");
    strcpy(fix.country_calling_code, "+33");
    strcpy(fix.timezone, "Europe/Paris");
    strcpy(fix.utc_offset, "+01:00");
    strcpy(fix.currency, "EUR");
    strcpy(fix.currency_name, "Euro");
    strcpy(fix.languages, "fr-FR,frp,br");

    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    geo_add_place(json_root(doc), &fix);

    /* Each member carries the value it was given, not merely a non-empty one: a
     * swapped pair (`country` and `country_code`) would still pass a presence check
     * and would mislead every reader. */
    static const struct { const char *key, *value; } strings[] = {
        {"city", "Paris"},
        {"region", "Ile-de-France"},
        {"region_code", "IDF"},
        {"country", "France"},
        {"country_code", "FR"},
        {"country_code_iso3", "FRA"},
        {"country_capital", "Paris"},
        {"country_tld", ".fr"},
        {"continent_code", "EU"},
        {"postal", "75001"},
        {"country_calling_code", "+33"},
        {"timezone", "Europe/Paris"},
        {"utc_offset", "+01:00"},
        {"currency", "EUR"},
        {"currency_name", "Euro"},
        {"languages", "fr-FR,frp,br"},
    };
    for (size_t i = 0; i < sizeof(strings) / sizeof(strings[0]); i++) {
        const char *got = json_string(json_get(json_root(doc), strings[i].key));
        TEST_ASSERT_EQUAL_STRING_MESSAGE(strings[i].value, got ? got : "<absent>",
                                         strings[i].key);
    }
    bool eu = false;
    TEST_ASSERT_TRUE(json_bool(json_get(json_root(doc), "in_eu"), &eu));
    TEST_ASSERT_TRUE(eu);
    json_free(doc);
}

TEST_CASE("geo_add_place omits every member the provider did not send", "[geo]")
{
    geo_fix_t fix = {0};
    strcpy(fix.city, "Paris");
    /* `in_eu` stays at its `{0}` value here on purpose: zero-initialising the struct
     * means `0`, which is a claim, so this case covers the empty strings only. The
     * tri-state case below covers the boolean. */
    fix.in_eu = -1;

    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    geo_add_place(json_root(doc), &fix);

    /* `04` §4.13: absent, never `""` and never `null`. A city of `""` reads as
     * "there is no city here", which is a claim the provider never made. */
    static const char *const keys[] = {
        "region", "region_code", "country", "country_code", "country_code_iso3",
        "country_capital", "country_tld", "continent_code", "in_eu", "postal",
        "country_calling_code", "timezone", "utc_offset", "currency",
        "currency_name", "languages",
    };
    for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); i++) {
        TEST_ASSERT_NULL_MESSAGE(json_get(json_root(doc), keys[i]), keys[i]);
    }
    TEST_ASSERT_EQUAL_STRING("Paris",
                             json_string(json_get(json_root(doc), "city")));
    json_free(doc);
}

TEST_CASE("geo_add_place reports in_eu only when the provider said", "[geo]")
{
    /* Tri-state: `-1` is "no answer", which is not the claim `false` makes. Emitting
     * `false` for it would tell a reader this country is outside the EU. */
    geo_fix_t unset = {0};
    unset.in_eu = -1;
    json_doc_t *absent = json_new_object();
    TEST_ASSERT_NOT_NULL(absent);
    geo_add_place(json_root(absent), &unset);
    TEST_ASSERT_NULL(json_get(json_root(absent), "in_eu"));
    json_free(absent);

    geo_fix_t outside = {0};
    outside.in_eu = 0;
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    geo_add_place(json_root(doc), &outside);
    const json_node_t *v = json_get(json_root(doc), "in_eu");
    TEST_ASSERT_NOT_NULL(v);
    bool eu = true;
    TEST_ASSERT_TRUE(json_bool(v, &eu));
    TEST_ASSERT_FALSE(eu);
    json_free(doc);
}

TEST_CASE("geo_add_place tolerates a null argument", "[geo]")
{
    /* The callers are command branches that may fail before they have a fix; a crash
     * there would take the link down instead of answering a refusal. */
    geo_fix_t fix = {0};
    geo_add_place(NULL, NULL);
    geo_add_place(NULL, &fix);
}
