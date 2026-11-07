/* tools_gate — the authorization decision for one call.
 *
 * `06-security.md` §6 is the sole authority on gates; this file only implements the
 * table. Pure, so every refusal is a host test rather than a flashing session. */

#include "tools.h"

#include <stdio.h>
#include <string.h>

/* Extract the host from an absolute URL, or NULL. `https://h/p` -> `h`. A port and
 * userinfo are never part of the match: the allowlist names hosts (`tools/web.md`). */
static const char *url_host(const char *url, char *buf, size_t cap)
{
    if (url == NULL) return NULL;
    const char *scheme = "https://";
    size_t n = strlen(scheme);
    if (strncmp(url, scheme, n) != 0) return NULL;

    const char *p = url + n;
    const char *end = p;
    while (*end != '\0' && *end != '/' && *end != '?' && *end != '#') end++;

    const char *host = p;
    for (const char *at = p; at < end; at++) {
        if (*at == '@') host = at + 1;  /* drop userinfo */
    }
    /* Drop a `:port`: the allowlist holds host names, and `https://host:443` would
     * otherwise miss every entry and refuse a legitimate call. */
    const char *port = NULL;
    if (*host != '[') {   /* an IPv6 literal keeps its colons inside brackets */
        for (const char *c = host; c < end; c++) {
            if (*c == ':') {
                port = c;
                break;
            }
        }
    }
    size_t len = (size_t)((port != NULL ? port : end) - host);
    if (len == 0 || len >= cap) return NULL;
    memcpy(buf, host, len);
    buf[len] = '\0';
    return buf;
}

bool tools_host_allowed(const tools_policy_t *policy, const char *host)
{
    if (policy == NULL || host == NULL) return false;
    const json_node_t *list = policy->http_allow_hosts;
    for (size_t i = 0; i < json_array_len(list); i++) {
        const char *entry = json_string(json_array_at(list, i));
        if (entry != NULL && strcmp(entry, host) == 0) return true;
    }
    return false;
}

esp_err_t tools_gate_check(const tools_builtin_t *tool, const tools_policy_t *policy,
                           const char *host, char *why, size_t why_cap)
{
    if (why != NULL && why_cap > 0) why[0] = '\0';
    if (tool == NULL) return ESP_ERR_INVALID_ARG;

    switch (tool->gate) {
    case TOOLS_GATE_NONE:
        return ESP_OK;

    case TOOLS_GATE_DANGEROUS:
        if (policy != NULL && policy->allow_dangerous) return ESP_OK;
        /* The exact wording `04-protocol.md` §3.13 quotes for a refused tool. */
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap, "%s refused: tools.allow_dangerous is false",
                     tool->name);
        }
        return ESP_ERR_NOT_ALLOWED;

    case TOOLS_GATE_ALLOWLIST:
        if (host != NULL && tools_host_allowed(policy, host)) return ESP_OK;
        if (why != NULL && why_cap > 0) {
            if (host == NULL) {
                snprintf(why, why_cap, "%s refused: the URL host is not allowlisted",
                         tool->name);
            } else {
                snprintf(why, why_cap,
                         "%s refused: %s is not in tools.http_allow_hosts",
                         tool->name, host);
            }
        }
        return ESP_ERR_NOT_ALLOWED;
    }
    return ESP_ERR_INVALID_ARG;
}

/* The host a call would reach, for the ALLOWLIST gate. `http.get` carries it in
 * `url`; a declarative tool carries it in its compiled `http.url`. */
const char *tools_gate_host_for(const tools_builtin_t *tool, const json_node_t *args,
                                char *buf, size_t cap)
{
    if (tool == NULL || tool->gate != TOOLS_GATE_ALLOWLIST) return NULL;
    if (strcmp(tool->name, "http.get") != 0) return NULL;
    return url_host(json_string(json_get(args, "url")), buf, cap);
}
