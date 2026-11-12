/* llm.c — the provider chain, its budget and its persistence.
 * Board-only: it reads NVS, resolves secrets through `cfg` and posts through `net`.
 * The rules it applies — window accounting, body shape, chain order — are in
 * `llm_budget.c` and `llm_body.c` and are covered on the host. */

#include "llm.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "cfg.h"
#include "clock.h"
#include "net_internal.h"
#include "store.h"

static const char *TAG = "llm";

typedef struct {
    char id[17];
    char base_url[128];
    char model[65];
    char api_key[CFG_SECRET_MAX + 1];
    char token_param[24];
    char extra_body[256];
    int32_t max_output_tokens;
    int32_t timeout_ms;
} llm_provider_t;

static llm_provider_t s_providers[LLM_MAX_PROVIDERS];
static size_t s_provider_count;
static llm_budget_t s_budget;
static int32_t s_max_hour = 100;
static int32_t s_max_day = 1000;
static char s_last_error[LLM_ERROR_MAX];
static char s_answered_id[17];
static char s_answered_model[65];
/* `04-protocol.md` §3.9: `llm.available` is "last call succeeded", not "a provider
 * resolved at boot". A device that boots with a valid chain but has never called out
 * reports false, and the first success flips it. */
static bool s_available;

/* NVS keys of the persisted counters (`03-config.md` §llm: "Counters are persisted in
 * NVS"). One int per counter and per bucket index, which is the whole state. */
#define NVS_HOUR_COUNT "llm_h_count"
#define NVS_HOUR_BUCKET "llm_h_bucket"
#define NVS_DAY_COUNT "llm_d_count"
#define NVS_DAY_BUCKET "llm_d_bucket"

static void budget_persist(void)
{
    store_nvs_set_int(NVS_HOUR_COUNT, s_budget.hour.count);
    store_nvs_set_int(NVS_HOUR_BUCKET, (int32_t)s_budget.hour.bucket);
    store_nvs_set_int(NVS_DAY_COUNT, s_budget.day.count);
    store_nvs_set_int(NVS_DAY_BUCKET, (int32_t)s_budget.day.bucket);
}

static void budget_restore(void)
{
    int32_t value = 0;
    if (store_nvs_get_int(NVS_HOUR_COUNT, &value) == ESP_OK) s_budget.hour.count = value;
    if (store_nvs_get_int(NVS_HOUR_BUCKET, &value) == ESP_OK) s_budget.hour.bucket = value;
    if (store_nvs_get_int(NVS_DAY_COUNT, &value) == ESP_OK) s_budget.day.count = value;
    if (store_nvs_get_int(NVS_DAY_BUCKET, &value) == ESP_OK) s_budget.day.bucket = value;
}

esp_err_t llm_init(void)
{
    s_provider_count = 0;
    s_last_error[0] = '\0';
    s_answered_id[0] = '\0';
    s_answered_model[0] = '\0';
    s_available = false;
    llm_budget_reset(&s_budget);
    budget_restore();

    json_node_t *llm = cfg_get("llm");
    if (llm == NULL) return ESP_ERR_NOT_FOUND;

    int64_t value = 0;
    if (json_int(json_get(llm, "max_calls_per_hour"), &value)) s_max_hour = (int32_t)value;
    if (json_int(json_get(llm, "max_calls_per_day"), &value)) s_max_day = (int32_t)value;

    json_node_t *chain = json_get(llm, "providers");
    size_t n = json_array_len(chain);
    for (size_t i = 0; i < n && s_provider_count < LLM_MAX_PROVIDERS; i++) {
        json_node_t *entry = json_array_at(chain, i);
        const char *base_url = json_string(json_get(entry, "base_url"));
        const char *model = json_string(json_get(entry, "model"));
        const char *ref = json_string(json_get(entry, "api_key_ref"));
        if (base_url == NULL || model == NULL || ref == NULL) continue;

        llm_provider_t *p = &s_providers[s_provider_count];
        memset(p, 0, sizeof *p);
        snprintf(p->base_url, sizeof p->base_url, "%s", base_url);
        snprintf(p->model, sizeof p->model, "%s", model);

        const char *id = json_string(json_get(entry, "id"));
        snprintf(p->id, sizeof p->id, "%s", id != NULL ? id : "provider");

        const char *param = json_string(json_get(entry, "token_param"));
        snprintf(p->token_param, sizeof p->token_param, "%s",
                 (param != NULL && param[0] != '\0') ? param : "max_tokens");

        p->max_output_tokens = 1024;
        if (json_int(json_get(entry, "max_output_tokens"), &value)) {
            p->max_output_tokens = (int32_t)value;
        }
        p->timeout_ms = 30000;
        if (json_int(json_get(entry, "timeout_ms"), &value)) {
            p->timeout_ms = (int32_t)value;
        }

        json_node_t *extra = json_get(entry, "extra_body");
        if (extra != NULL) {
            /* Flat, at the root: `extra_body`'s members *are* request members
             * (`03-config.md` §llm). The shape rule lives in the host-tested module,
             * which also refuses a document that does not fit rather than truncating
             * it. */
            size_t written = llm_flatten_members(extra, p->extra_body,
                                                 sizeof p->extra_body);
            if (written == 0) {
                /* Named, because an inert `extra_body` is a 400 whose cause is a
                 * setting too long for the buffer, which no log otherwise implies. */
                ESP_LOGW(TAG, "llm: %s: extra_body not sent; it does not fit %u bytes",
                         p->id, (unsigned)sizeof p->extra_body);
            }
        }

        esp_err_t err = cfg_resolve_secret(ref, p->api_key, sizeof p->api_key, NULL);
        if (err != ESP_OK) {
            /* A provider whose key does not resolve is skipped with a named log. It
             * stays in the chain's index order, so `llm_next_provider` still walks it
             * without an off-by-one. */
            ESP_LOGW(TAG, "llm: %s: %s not resolved: %s", p->id, ref,
                     esp_err_to_name(err));
        }
        s_provider_count++;
    }

    if (s_provider_count == 0) {
        ESP_LOGW(TAG, "llm: no usable provider in the chain");
        return ESP_ERR_NOT_FOUND;
    }
    return ESP_OK;
}

esp_err_t llm_query(const llm_request_t *request, char *response_buf, size_t response_cap,
                    int *provider_out)
{
    if (provider_out != NULL) *provider_out = -1;
    if (request == NULL || response_buf == NULL || response_cap == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    s_last_error[0] = '\0';
    static char s_body[LLM_BODY_MAX];
    static char s_endpoint[192];
    static char s_auth[CFG_SECRET_MAX + 16];

    /* `llm_next_provider` returns -1 at the end of the chain, so the guard is on the
     * next index rather than a `for` increment: `index < count` is true for -1 and
     * would walk the array backwards out of bounds forever. */
    int index = 0;
    while (llm_provider_loop_ok(index, (int)s_provider_count)) {
        llm_provider_t *p = &s_providers[index];

        if (p->api_key[0] == '\0') {
            snprintf(s_last_error, sizeof s_last_error, "%s: api key not set", p->id);
            ESP_LOGW(TAG, "llm: %s", s_last_error);
            index = llm_next_provider(index, (int)s_provider_count);
            continue;
        }

        llm_request_t attempt = *request;
        attempt.model = p->model;
        attempt.token_param = p->token_param;
        attempt.max_output_tokens = p->max_output_tokens;

        if (llm_endpoint(p->base_url, s_endpoint, sizeof s_endpoint) == 0) {
            snprintf(s_last_error, sizeof s_last_error, "%s: base_url too long", p->id);
            index = llm_next_provider(index, (int)s_provider_count);
            continue;
        }
        if (llm_assemble_prompt(&attempt, p->extra_body[0] != '\0' ? p->extra_body : NULL,
                           s_body, sizeof s_body) == 0) {
            /* Every provider shares `s_body` and its cap, so a body that does not fit
             * fits nowhere: walking the chain only spends budget. The caller trims
             * history to fit and retries (`02-firmware.md` §llm). */
            snprintf(s_last_error, sizeof s_last_error, "%s: request body too large", p->id);
            s_available = false;
            return ESP_ERR_INVALID_SIZE;
        }
        snprintf(s_auth, sizeof s_auth, "Bearer %s", p->api_key);

        /* One bounded retry against the same provider. The three cheap pre-checks
         * above are outside the loop, so they never spend budget twice. */
        for (int retry = 0;; retry++) {
            /* Charged per attempt that opens a socket, across the whole chain
             * (`02-firmware.md` §llm). A refusal stops here: another provider would
             * spend the same budget. */
            if (!llm_budget_charge(&s_budget, s_max_hour, s_max_day, clock_now())) {
                budget_persist();
                snprintf(s_last_error, sizeof s_last_error, "call budget exhausted");
                return ESP_ERR_INVALID_STATE;
            }
            budget_persist();

            net_response_t resp;
            esp_err_t err = net_post(s_endpoint, "application/json", s_auth, s_body,
                                     strlen(s_body), p->timeout_ms, &resp);

            /* Nobody is waiting for this answer any more: not a provider failure, so
             * neither retried nor failed over. `run_once` reports it as `E_CANCELLED`
             * through this value. */
            if (err == ESP_ERR_NOT_FINISHED) {
                snprintf(s_last_error, sizeof s_last_error, "%s: cancelled", p->id);
                s_available = false;
                return ESP_ERR_NOT_FINISHED;
            }

            bool retryable;
            int32_t wait_ms = 0;

            if (err != ESP_OK) {
                snprintf(s_last_error, sizeof s_last_error, "%s: %s", p->id,
                         esp_err_to_name(err));
                ESP_LOGW(TAG, "llm: %s", s_last_error);
                retryable = llm_retry_after(err, 0, -1, retry, &wait_ms);
            } else if (resp.status >= 200 && resp.status < 300) {
                size_t copy = resp.body_len < response_cap - 1 ? resp.body_len
                                                              : response_cap - 1;
                memcpy(response_buf, resp.body, copy);
                response_buf[copy] = '\0';
                free(resp.body);
                if (provider_out != NULL) *provider_out = index;
                s_available = true;
                snprintf(s_answered_id, sizeof s_answered_id, "%s", p->id);
                snprintf(s_answered_model, sizeof s_answered_model, "%s", p->model);
                return ESP_OK;
            } else {
                /* The provider's own words are the only thing that names the offending
                 * member: a 400 saying `model` points at the model dropdown, one saying
                 * `reasoning_effort` at the thinking level. Clipped and flattened to
                 * one line so it survives a JSON string in `agent.error`
                 * unescaped-looking and readable in a log. */
                llm_copy_error_detail(resp.body, resp.body_len, p->id, resp.status,
                                      s_last_error, sizeof s_last_error);
                /* A provider that echoes the request back prints the key into the
                 * reader's scrollback; `06-security.md` §7 forbids that reaching
                 * `agent.error`. */
                llm_redact(s_last_error, p->api_key);
                ESP_LOGW(TAG, "llm: %s", s_last_error);
                retryable = llm_retry_after(ESP_OK, resp.status, resp.retry_after_s,
                                            retry, &wait_ms);
                free(resp.body);
            }

            if (!retryable) break;
            vTaskDelay(pdMS_TO_TICKS(wait_ms));
        }

        index = llm_next_provider(index, (int)s_provider_count);
    }

    if (s_last_error[0] == '\0') {
        snprintf(s_last_error, sizeof s_last_error, "no provider answered");
    }
    s_available = false;
    return ESP_ERR_NOT_FOUND;
}

void llm_counters(int32_t *hour, int32_t *day)
{
    llm_budget_counters(&s_budget, hour, day);
}

bool llm_last_answer(const char **id, const char **model)
{
    if (id != NULL) *id = s_answered_id[0] != '\0' ? s_answered_id : NULL;
    if (model != NULL) *model = s_answered_model[0] != '\0' ? s_answered_model : NULL;
    return s_available;
}

void llm_limits(int32_t *hour, int32_t *day)
{
    if (hour != NULL) *hour = s_max_hour;
    if (day != NULL) *day = s_max_day;
}

const char *llm_last_error(void)
{
    return s_last_error;
}

void llm_abort(void)
{
    /* The handle belongs to `net`, which is the only module that calls
     * `esp_http_client_perform`; this is the one line of the cancel path it exposes. */
    net_abort_inflight();
}


bool llm_active_host(char *out, size_t cap)
{
    if (out == NULL || cap < 2) return false;
    out[0] = '\0';
    if (s_provider_count == 0) return false;

    const char *url = s_providers[0].base_url;
    const char *scheme = strstr(url, "://");
    const char *host = scheme != NULL ? scheme + 3 : url;
    size_t n;
    if (host[0] == '[') {
        const char *close = strchr(host, ']');
        if (close == NULL) return false;
        host++;
        n = (size_t)(close - host);
    } else {
        n = strcspn(host, ":/");
    }
    if (n == 0 || n >= cap) return false;
    memcpy(out, host, n);
    out[n] = '\0';
    return true;
}
