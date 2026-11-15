/* `04-protocol.md` §4.13 owns the place-and-locale member names, and two surfaces
 * emit them: the `geo.locate` command in `main` and the `geo.locate` tool in
 * `tools`. Two copies of a seventeen-name list drift the first time a provider
 * gains a field, so the list lives here once and both callers use it.
 *
 * Pure and host-tested: it only writes into a JSON node. */
#include "geo.h"

#include "store.h"

/* An absent member is left out rather than emitted as `""`: `04` §4.13 says a
 * provider that did not report a city produced no city, and a bridge that receives
 * `"city":""` has to decide whether that means "unknown" or "nowhere", which is a
 * question the wire should never have asked. `json_add_string` on an existing key is
 * a no-op, so a caller that already set one keeps its value. */
static void add_if_set(json_node_t *root, const char *key, const char *value)
{
    if (value != NULL && value[0] != '\0') json_add_string(root, key, value);
}

void geo_add_place(json_node_t *root, const geo_fix_t *fix)
{
    if (root == NULL || fix == NULL) return;
    add_if_set(root, "city", fix->city);
    add_if_set(root, "region", fix->region);
    add_if_set(root, "region_code", fix->region_code);
    add_if_set(root, "country", fix->country);
    add_if_set(root, "country_code", fix->country_code);
    add_if_set(root, "country_code_iso3", fix->country_code_iso3);
    add_if_set(root, "country_capital", fix->country_capital);
    add_if_set(root, "country_tld", fix->country_tld);
    add_if_set(root, "continent_code", fix->continent_code);
    /* Tri-state: `-1` is "the provider did not say", which is not the same claim as
     * `false` and must not be emitted as one. */
    if (fix->in_eu >= 0) json_add_bool(root, "in_eu", fix->in_eu != 0);
    add_if_set(root, "postal", fix->postal);
    add_if_set(root, "country_calling_code", fix->country_calling_code);
    add_if_set(root, "timezone", fix->timezone);
    add_if_set(root, "utc_offset", fix->utc_offset);
    add_if_set(root, "currency", fix->currency);
    add_if_set(root, "currency_name", fix->currency_name);
    add_if_set(root, "languages", fix->languages);
}
