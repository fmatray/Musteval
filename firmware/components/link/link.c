/* link — NDJSON framing over USB-Serial/JTAG CDC and envelope assembly
 * (`04-protocol.md` §1, §2). Layering: L2 (`08-code-architecture.md` §1). */

#include "link.h"
#include "link_internal.h"

#include <inttypes.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

/* One scratch frame, static because the link task's 4 KiB stack (`02-firmware.md`
 * §3.3) cannot hold it and `out` must stay untouched on failure. The 16 KiB
 * BSS cost is acceptable for the sole link task. */
static char s_tx[LINK_TX_MAX];

/* The assembled frame for the emit paths below. Static for the same reason as
 * `s_tx`: a 16 KiB local would overflow the link task's stack. Two tasks emit —
 * the link task and the agent task — so `s_emit_lock` guards both this buffer and
 * `s_tx`, and every frame is assembled and written inside it. */
static char s_emit[LINK_TX_MAX];
static SemaphoreHandle_t s_emit_lock;

/* `link_emit_body` is called from the link task (`command.result`, `hello_ack`,
 * `event`) and from the agent task (`agent.start`, `agent.tool`, `agent.final`);
 * the log hook can fire from either. Without the lock two tasks would interleave
 * halves of two lines in `s_tx`/`s_emit` and desynchronise the stream — the one
 * failure the NDJSON reader cannot recover from (`04-protocol.md` §1.1).
 *
 * Recursive because `link_envelope_wrap` takes it for `s_tx` and its callers take
 * it again for `s_emit`. */
static void emit_lock_take(void)
{
    if (s_emit_lock != NULL) xSemaphoreTakeRecursive(s_emit_lock, portMAX_DELAY);
}

static void emit_lock_give(void)
{
    if (s_emit_lock != NULL) xSemaphoreGiveRecursive(s_emit_lock);
}

/* `d-<counter>` is per boot and never reused (`04-protocol.md` §2). Both the link
 * task and the agent task allocate ids, so the counter is taken under the emit
 * lock, and the caller supplies the buffer: a shared one could be overwritten by
 * the other task between the allocation and `link_envelope_wrap` copying it. */
static uint32_t s_counter = 0;

/* The refusal message for a malformed line, which must name the byte offset
 * (`04-protocol.md` §1.2). Static because the link task stack is 4096 bytes and the
 * frame is built from it (`02-firmware.md` §3.3). */
static char s_parse_message[32];

static const link_sink_t *s_sink = NULL;

void link_reader_reset(link_reader_t *reader)
{
    reader->len = 0;
    reader->seen = 0;
    reader->discarding = false;
    reader->buf[0] = '\0';
}

bool link_reader_feed(link_reader_t *reader, char byte, size_t *line_len,
                      link_line_status_t *status)
{
    /* Rule 1: A line is complete only when `\n` arrives. */
    if (byte == '\n') {
        size_t n = reader->len;

        /* Rule 2: a trailing `\r` is stripped and not counted. */
        if (n > 0 && reader->buf[n - 1] == '\r') {
            n--;
        }

        /* Rule 3 / §1.2: the limit includes the terminator, so content
         * before `\n` may be at most LINK_RX_MAX - 1. One report per line. */
        if (reader->discarding || n > LINK_RX_MAX - 1) {
            /* `seen` counts the whole line, so the caller can report the real
             * length (§1.2). The prefix stays in `buf` on purpose: §1.2 answers an
             * oversize line with `command.result` when an `id` is recoverable, and
             * wiping the buffer made that branch unreachable — every oversize line
             * fell through to the `protocol.error` event instead. */
            *line_len = reader->seen;
            reader->len = 0;
            reader->seen = 0;
            reader->discarding = false;
            *status = LINK_LINE_OVERSIZE;
            return true;
        }

        *line_len = n;
        reader->buf[n] = '\0'; /* NUL-terminate for link_reader_line */
        reader->len = 0;
        reader->seen = 0;
        reader->discarding = false;
        /* An empty line is not a frame (§1.1 rule 1). */
        *status = (n == 0) ? LINK_LINE_BAD_JSON : LINK_LINE_OK;
        return true;
    }

    /* Counted for every content byte, including the ones past the cap, so an
     * oversize line can report its real length (§1.2) instead of a constant 0. */
    reader->seen++;

    if (reader->discarding) {
        return false; /* rule 3: drop bytes until the next newline */
    }

    /* The cap includes the terminator, so content bytes before `\n` cannot
     * exceed LINK_RX_MAX - 1. If we reach 4096, it is already oversize. */
    if (reader->len >= LINK_RX_MAX - 1) {
        /* Only set discarding if the 4096th byte is not a newline (handled above).
         * `len` is kept: the prefix is what `link_recover_id` scans for an `id`, and
         * the buffer is rewritten from offset 0 by the next line. */
        reader->discarding = true;
        return false;
    }

    reader->buf[reader->len++] = byte;
    return false;
}

const char *link_reader_line(const link_reader_t *reader)
{
    return reader->buf;
}

static bool id_valid(const char *id)
{
#define LINK_ID_MAX 32 /* `04-protocol.md` §2: 1-32 chars from [A-Za-z0-9._-] */
    size_t n = 0;
    if (id == NULL) return false;
    for (const char *p = id; *p != '\0'; p++, n++) {
        char c = *p;
        bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                  (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-';
        if (!ok || n >= LINK_ID_MAX) return false;
    }
    return n > 0; /* `04-protocol.md` §2: 1-32 characters */
}

/* The union of `04-protocol.md` §2.1: every type that exists on the wire. §2
 * requires an unknown type to be rejected rather than ignored, so this is the
 * parse-time check; direction is checked later, where a handler is chosen. */
static bool type_known(const char *type)
{
    static const char *const k_types[] = {
        "hello",  "hello_ack", "prompt",   "cancel",     "command",
        "secret.set", "ping",  "pong",     "ack",        "status",
        "log",    "agent.start", "agent.tool", "agent.tool_result",
        "agent.final", "agent.error", "task.fired", "gpio.edge",
        "mqtt.message", "event", "command.result",
    };
    for (size_t i = 0; i < sizeof(k_types) / sizeof(k_types[0]); i++) {
        if (strcmp(type, k_types[i]) == 0) return true;
    }
    return false;
}

/* The bridge -> device column of `04-protocol.md` §2.1. Direction is a dispatch
 * rule, not an envelope rule, so `link_frame_parse` does not apply it: a frame can
 * be well-formed and still be sent the wrong way, and §2.1 makes that
 * `E_PROTOCOL` at the point a handler is chosen. */
bool link_type_inbound(const char *type)
{
    static const char *const k_inbound[] = {
        "hello", "prompt", "cancel", "command", "secret.set", "ping", "ack",
    };
    for (size_t i = 0; i < sizeof(k_inbound) / sizeof(k_inbound[0]); i++) {
        if (strcmp(type, k_inbound[i]) == 0) return true;
    }
    return false;
}

esp_err_t link_envelope_wrap(json_doc_t *body, const char *type, const char *id,
                             int64_t ts, const char *reply_to, char *out,
                             size_t out_cap, size_t *written)
{
    if (type == NULL || type[0] == '\0' || id == NULL || !id_valid(id) ||
        out == NULL || written == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (reply_to != NULL && !id_valid(reply_to)) {
        return ESP_ERR_INVALID_ARG; /* `04-protocol.md` §2: reply_to is an id */
    }
    if (body != NULL && !json_is_object(json_root(body))) {
        return ESP_ERR_INVALID_ARG; /* `04-protocol.md` §2: frames are objects */
    }

    /* Assemble envelope prefix. Order follows §3 examples. */
    emit_lock_take();
    int n = snprintf(s_tx, sizeof(s_tx), "{\"v\":1,\"type\":\"%s\",\"id\":\"%s\",\"ts\":%" PRId64,
                     type, id, ts);
    if (n < 0 || (size_t)n >= sizeof(s_tx)) {
        emit_lock_give();
        return ESP_ERR_INVALID_SIZE;
    }
    size_t len = (size_t)n;

    if (reply_to != NULL) {
        n = snprintf(s_tx + len, sizeof(s_tx) - len, ",\"reply_to\":\"%s\"", reply_to);
        if (n < 0 || (size_t)n >= sizeof(s_tx) - len) {
            emit_lock_give();
            return ESP_ERR_INVALID_SIZE;
        }
        len += (size_t)n;
    }

    if (body == NULL) {
        if (len + 1 >= sizeof(s_tx)) {
            emit_lock_give();
            return ESP_ERR_INVALID_SIZE;
        }
        s_tx[len++] = '}';
        s_tx[len] = '\0';
    } else {
        /* Emit body JSON into scratch; splice members into our object. */
        size_t blen = 0;
        esp_err_t err = json_emit(body, s_tx + len + 1, sizeof(s_tx) - len - 1, &blen);
        if (err != ESP_OK) { /* `04` §1.2 TX limit */
            emit_lock_give();
            return ESP_ERR_INVALID_SIZE;
        }
        if (blen < 2) { /* json_emit failed for object */
            emit_lock_give();
            return ESP_ERR_INVALID_ARG;
        }

        if (blen == 2) {
            /* Body is empty {}, just close the envelope. */
            s_tx[len++] = '}';
            s_tx[len] = '\0';
        } else {
            /* Body non-empty: prefix with comma, splice interior (drop '{'). */
            s_tx[len++] = ',';
            memmove(s_tx + len, s_tx + len + 1, blen - 1);
            len += (blen - 1);
            s_tx[len] = '\0';
        }
    }

    /* result fits out_cap? NUL at index len is not counted toward out_cap,
     * mirroring `json_emit` semantics. */
    if (len >= out_cap) {
        emit_lock_give();
        return ESP_ERR_INVALID_SIZE;
    }
    memcpy(out, s_tx, len + 1);
    *written = len;
    emit_lock_give();
    return ESP_OK;
}

/* `04-protocol.md` §2, the inbound half. Unknown members are ignored (forward
 * compatibility) but a missing or wrong-typed required one is fatal, and an
 * unknown `type` is fatal rather than ignored. */
esp_err_t link_frame_parse(const char *line, size_t len, json_doc_t **out,
                           const char **bad_field)
{
    if (line == NULL || out == NULL || bad_field == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *out = NULL;
    *bad_field = NULL;

    if (len == 0 || len >= LINK_RX_MAX) {
        *bad_field = "line";
        return ESP_ERR_INVALID_SIZE;
    }

    json_doc_t *doc = NULL;
    size_t error_offset = 0;
    if (json_parse_at(line, len, &doc, &error_offset) != ESP_OK || doc == NULL) {
        /* §1.2: for a line that is not valid JSON the refusal `message` names the
         * parse offset. `bad_field` carries only the class, so the offset rides in
         * a static buffer — the link task is the sole caller and the string is
         * consumed before the next parse (`02-firmware.md` §3.3). */
        snprintf(s_parse_message, sizeof s_parse_message, "json at byte %u",
                 (unsigned)error_offset);
        *bad_field = s_parse_message;
        return ESP_ERR_INVALID_ARG;
    }

    json_node_t *root = json_root(doc);
    if (!json_is_object(root)) {
        *bad_field = "json";
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }

    /* `v`: exactly 1, integer (§2). */
    int64_t v = 0;
    if (!json_int(json_get(root, "v"), &v) || v != 1) {
        *bad_field = "v";
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }

    const char *type = json_string(json_get(root, "type"));
    if (type == NULL || type[0] == '\0' || !type_known(type)) {
        *bad_field = "type";
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }

    const char *id = json_string(json_get(root, "id"));
    if (id == NULL || !id_valid(id)) {
        *bad_field = "id";
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }

    if (!json_is_int(json_get(root, "ts"))) {
        *bad_field = "ts";
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }

    /* `reply_to` is optional, but if present it must be a legal id. */
    json_node_t *reply = json_get(root, "reply_to");
    if (reply != NULL && !json_is_null(reply)) {
        const char *reply_id = json_string(reply);
        if (reply_id == NULL || !id_valid(reply_id)) {
            *bad_field = "reply_to";
            json_free(doc);
            return ESP_ERR_INVALID_ARG;
        }
    }

    *out = doc;
    return ESP_OK;
}

void link_next_id(char *out, size_t cap)
{
    if (out == NULL || cap < 8) return;
    emit_lock_take();
    snprintf(out, cap, "d-%" PRIu32, ++s_counter);
    emit_lock_give();
}

esp_err_t link_set_sink(const link_sink_t *sink)
{
    if (sink == NULL) return ESP_ERR_INVALID_ARG;
    s_sink = sink;
    return ESP_OK;
}

const link_sink_t *link_sink_get(void)
{
    return s_sink;
}

/* --- Transmit port -------------------------------------------------------- */

static link_writer_t s_writer = NULL;

void link_set_writer(link_writer_t writer)
{
    /* Created here because this is the first call a booting device makes into the
     * module, so the lock exists before either emitting task starts. */
    if (s_emit_lock == NULL) s_emit_lock = xSemaphoreCreateRecursiveMutex();
    s_writer = writer;
}

esp_err_t link_emit(char *frame, size_t cap, size_t len)
{
    if (frame == NULL || len == 0) return ESP_ERR_INVALID_ARG;
    /* The terminator must fit in the caller's own buffer: `link` holds no buffer of
     * its own, and one writer call is the only way a frame cannot interleave with
     * another task's half-written line and desynchronise the stream. */
    if (len + 2 > cap || len + 2 > LINK_TX_MAX) return ESP_ERR_INVALID_SIZE;
    if (s_writer == NULL) return ESP_ERR_INVALID_STATE;

    frame[len] = '\r';
    frame[len + 1] = '\n';
    return s_writer(frame, len + 2);
}

/* --- Log hook ------------------------------------------------------------- */

/* A `log` frame is up to `LINK_TX_MAX`, but a hook that formats on the logging
 * task's stack has to be small: `configMINIMAL_STACK_SIZE` is 3 KiB on S3 and the
 * frame plus the rendered line would crowd it. 1 KiB covers the frame for a normal
 * message and truncates an absurd one, which is better than an overflow. */
#define LINK_LOG_BUF 1024

/* Set while the hook is formatting, so a log call from inside it (or from the
 * writer) cannot recurse. */
static volatile bool s_in_hook = false;

static const char *s_log_conf = "info";
static void *s_prev_vprintf = NULL;
static vprintf_like_t s_real_vprintf = NULL;

/* The hook runs on whatever task logged, and `main` is only 3584 bytes
 * (`CONFIG_ESP_MAIN_TASK_STACK_SIZE`). Its three buffers therefore live here, not
 * on the stack. `s_in_hook` serialises the hook, so a second task logging while a
 * frame is being built passes through instead of sharing them. */
static char s_log_line[LINK_LOG_BUF];
static char s_log_msg[LINK_LOG_BUF];
static char s_log_frame[LINK_LOG_BUF + 64];

static int log_hook(const char *fmt, va_list args)
{
    /* Re-entry guard: formatting, emitting or redacting may log. Passing straight
     * through to the previous handler keeps that output visible without a frame. */
    if (s_in_hook) {
        return (s_real_vprintf != NULL) ? s_real_vprintf(fmt, args) : 0;
    }
    s_in_hook = true;

    char *line = s_log_line;
    int n = vsnprintf(line, sizeof(s_log_line), fmt, args);
    if (n < 0) {
        s_in_hook = false;
        return 0;
    }

    link_log_line_t parts;
    if (!link_log_parse(line,
                        (n < (int)sizeof(s_log_line)) ? (size_t)n
                                                      : sizeof(s_log_line) - 1,
                        &parts)) {
        /* Not an ESP_LOGx line (`printf`, or a format that does not match LOG_FORMAT):
         * not a frame, so it is printed unchanged. `vprintf`, not the saved
         * `vprintf_like_t`: that one takes a `va_list` and cannot be called with
         * arguments. */
        s_in_hook = false;
        /* `fputs`, not `vprintf`: IDF shadows `vprintf` with a macro, and the line
         * is already rendered, so no format string is needed. */
        fputs(line, stdout);
        return n;
    }

    const char *level = link_log_filter(parts.level_letter, s_log_conf);
    if (level == NULL) {
        s_in_hook = false;
        return n;
    }

    /* `tag` and `msg` are borrowed slices of `line`; the frame builder copies. */
    char tag[64];
    size_t tag_copy = parts.tag_len < sizeof(tag) - 1 ? parts.tag_len : sizeof(tag) - 1;
    memcpy(tag, parts.tag, tag_copy);
    tag[tag_copy] = '\0';

    /* `parts.msg` is a slice of `line`, not a C string: copy it so the frame
     * builder sees exactly `msg_len` bytes and no trailing newline. */
    char *msg = s_log_msg;
    size_t msg_copy = parts.msg_len < sizeof(s_log_msg) - 1 ? parts.msg_len
                                                           : sizeof(s_log_msg) - 1;
    memcpy(msg, parts.msg, msg_copy);
    msg[msg_copy] = '\0';

    char *frame = s_log_frame;
    size_t written = 0;
    esp_err_t err = link_log_frame(tag, msg, level, 0, parts.t_ms, frame,
                                   sizeof(s_log_frame) - 2, &written);
    if (err == ESP_OK) {
        /* Timestamps are the hook's own: `ts` is wall time from NTP, which the
         * sys layer owns and this module cannot read. `ts:0` means unsynced, which
         * is exactly true before SNTP completes. */
        emit_lock_take();
        link_emit(frame, sizeof(s_log_frame), written);
        emit_lock_give();
    }

    s_in_hook = false;
    /* Return the rendered length so a caller that checks it sees success. */
    return n;
}

void *link_log_install(const char *configured)
{
    if (configured != NULL) s_log_conf = configured;
    if (s_prev_vprintf == NULL) {
        s_prev_vprintf = esp_log_set_vprintf(log_hook);
        s_real_vprintf = (vprintf_like_t)s_prev_vprintf;
    }
    return s_prev_vprintf;
}

/* --- Refusing a line (`04-protocol.md` §1.2) ------------------------------ */

const char *link_recover_id(const char *line, size_t len, char *out,
                            size_t out_cap)
{
    if (line == NULL || out == NULL || out_cap < 2) return NULL;

    /* An oversize line reports the bytes it *saw*, which can exceed the buffer: the
     * reader keeps only the first LINK_RX_MAX - 1. Clamping here keeps the scan in
     * bounds without the caller having to pass two lengths. */
    if (len > LINK_RX_MAX - 1) len = LINK_RX_MAX - 1;

    /* No `strstr`: the line is a byte range, not necessarily NUL-terminated. */
    for (size_t i = 0; i + 4 <= len; i++) {
        if (line[i] != '"' || memcmp(line + i, "\"id\"", 4) != 0) continue;

        size_t j = i + 4;
        while (j < len && (line[j] == ' ' || line[j] == '\t')) j++;
        if (j >= len || line[j] != ':') continue;
        j++;
        while (j < len && (line[j] == ' ' || line[j] == '\t')) j++;
        if (j >= len || line[j] != '"') continue;
        j++;

        size_t start = j;
        while (j < len && line[j] != '"') j++;
        if (j >= len) return NULL; /* unterminated string */
        size_t n = j - start;
        if (n == 0 || n > LINK_ID_MAX || n >= out_cap) return NULL;

        memcpy(out, line + start, n);
        out[n] = '\0';
        /* The charset check is `id_valid`'s, so a recovered id is exactly the one a
         * parsed frame would have accepted. */
        return id_valid(out) ? out : NULL;
    }
    return NULL;
}

esp_err_t link_refuse_line(const char *line, size_t len, const char *name,
                           const char *message, int64_t ts)
{
    if (message == NULL) return ESP_ERR_INVALID_ARG;
    /* `04` §3.20 makes `name` mandatory and strings it: an unknown command echoes
     * the empty name, which is what the reference implementation does. */
    if (name == NULL) name = "";

    char idbuf[LINK_ID_MAX + 1];
    const char *reply_to = link_recover_id(line, len, idbuf, sizeof(idbuf));

    char *frame = s_emit;
    size_t written = 0;
    esp_err_t err;

    if (reply_to != NULL) {
        json_doc_t *doc = json_new_object();
        if (doc == NULL) return ESP_ERR_NO_MEM;
        json_node_t *root = json_root(doc);
        json_node_t *err_node = json_add_object(root, "error");
        bool ok = err_node != NULL &&
                  json_add_string(err_node, "code", "E_PROTOCOL") &&
                  json_add_string(err_node, "message", message) &&
                  json_add_string(root, "name", name) &&
                  json_add_bool(root, "ok", false) &&
                  json_add_int(root, "duration_ms", 0);
        if (!ok) {
            json_free(doc);
            return ESP_ERR_NO_MEM;
        }
        emit_lock_take();
        char id[16];
        link_next_id(id, sizeof id);
        err = link_envelope_wrap(doc, "command.result", id, ts,
                                 reply_to, frame, LINK_TX_MAX, &written);
        if (err == ESP_OK) err = link_emit(frame, LINK_TX_MAX, written);
        emit_lock_give();
        json_free(doc);
        return err;
    }

    /* No recoverable id: the only answer §1.2 allows is the event. */
    json_doc_t *doc = json_new_object();
    if (doc == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(doc);
    json_node_t *data = json_add_object(root, "data");
    bool ok = data != NULL &&
              json_add_string(root, "kind", "protocol.error") &&
              json_add_string(data, "code", "E_PROTOCOL") &&
              json_add_string(data, "message", message) &&
              json_add_int(data, "dropped_bytes", (int64_t)len);
    if (!ok) {
        json_free(doc);
        return ESP_ERR_NO_MEM;
    }
    emit_lock_take();
    char id[16];
    link_next_id(id, sizeof id);
    err = link_envelope_wrap(doc, "event", id, ts, NULL, frame,
                             LINK_TX_MAX, &written);
    if (err == ESP_OK) err = link_emit(frame, LINK_TX_MAX, written);
    emit_lock_give();
    json_free(doc);
    return err;
}

esp_err_t link_emit_pong(const char *reply_to, int64_t ts)
{
    char *frame = s_emit;
    size_t written = 0;

    json_doc_t *doc = json_new_object();
    if (doc == NULL) return ESP_ERR_NO_MEM;

    emit_lock_take();
    char id[16];
    link_next_id(id, sizeof id);
    esp_err_t err = link_envelope_wrap(doc, "pong", id, ts, reply_to,
                                       frame, LINK_TX_MAX, &written);
    if (err == ESP_OK) err = link_emit(frame, LINK_TX_MAX, written);
    emit_lock_give();
    json_free(doc);
    return err;
}

/* --- Bodies `app` fills (`04-protocol.md` §3.2, §3.9) ---------------------- */

static esp_err_t emit_frame(json_doc_t *body, const char *type, const char *id,
                            int64_t ts, const char *reply_to)
{
    size_t written = 0;
    emit_lock_take();
    esp_err_t err = link_envelope_wrap(body, type, id, ts, reply_to, s_emit,
                                       LINK_TX_MAX, &written);
    if (err == ESP_OK) err = link_emit(s_emit, LINK_TX_MAX, written);
    emit_lock_give();
    return err;
}

esp_err_t link_emit_body(const char *type, const char *id, int64_t ts,
                         const char *reply_to, json_doc_t *body)
{
    /* The body is borrowed for the duration of the call only: `link` emits it and
     * never keeps the pointer, so the caller frees its document as usual. */
    return emit_frame(body, type, id, ts, reply_to);
}

esp_err_t link_build_hello_ack(json_node_t *obj, const link_hello_t *hello)
{
    if (obj == NULL || hello == NULL) return ESP_ERR_INVALID_ARG;

    json_node_t *limits = json_add_object(obj, "limits");
    json_node_t *caps = json_add_object(obj, "capabilities");
    json_node_t *tools = caps != NULL ? json_new_array(caps, "tools") : NULL;
    json_node_t *mcp = caps != NULL ? json_new_array(caps, "mcp") : NULL;

    /* The live set, copied from what `app` built, so `hello_ack` reports what is
     * callable now rather than a compile-time constant (§3.2). */
    if (hello->tools != NULL && tools != NULL) {
        size_t n = json_array_len(hello->tools);
        for (size_t i = 0; i < n; i++) {
            json_node_t *item = json_array_at(hello->tools, i);
            const char *name = json_string(item);
            if (name != NULL) json_array_push_string(tools, name);
        }
    }

    bool ok = json_add_string(obj, "device",
                              hello->device != NULL ? hello->device : "musteval") &&
              json_add_string(obj, "fw_version",
                              hello->fw_version != NULL ? hello->fw_version : "1.0.0") &&
              json_add_int(obj, "protocol", 1) &&
              json_add_string(obj, "boot_id",
                              hello->boot_id != NULL ? hello->boot_id : "") &&
              json_add_bool(obj, "time_synced", hello->time_synced) &&
              limits != NULL && json_add_int(limits, "rx_max", LINK_RX_MAX) &&
              json_add_int(limits, "tx_max", LINK_TX_MAX) &&
              caps != NULL && tools != NULL && mcp != NULL &&
              json_add_bool(caps, "dangerous_tools", false) &&
              json_add_bool(caps, "streaming", false);

    return ok ? ESP_OK : ESP_ERR_NO_MEM;
}

esp_err_t link_build_status(json_node_t *obj, const link_status_t *st)
{
    if (obj == NULL || st == NULL) return ESP_ERR_INVALID_ARG;

    json_node_t *wifi = json_add_object(obj, "wifi");
    json_node_t *time = json_add_object(obj, "time");
    json_node_t *llm = json_add_object(obj, "llm");
    json_node_t *agent = json_add_object(obj, "agent");
    json_node_t *storage = json_add_object(obj, "storage");
    json_node_t *ufs = storage != NULL ? json_add_object(storage, "userfs") : NULL;
    json_node_t *wfs = storage != NULL ? json_add_object(storage, "wsfs") : NULL;
    json_node_t *mem = json_add_object(obj, "mem");
    json_node_t *sysn = json_add_object(obj, "sys");
    json_node_t *mcp = json_new_array(obj, "mcp");
    json_node_t *mqtt = json_new_array(obj, "mqtt");

    bool ok = wifi != NULL && time != NULL && llm != NULL && agent != NULL &&
              storage != NULL && ufs != NULL && wfs != NULL && mem != NULL &&
              sysn != NULL && mcp != NULL && mqtt != NULL;

    /* A subsystem that does not exist yet reports its idle value, not a plausible
     * one: the bridge prints §3.9 verbatim, so a fabricated field would be a lie
     * the UI shows. */
    /* A NULL identity is reported as JSON null, not as an empty string: `04` §3.9
     * types these as nullable, and `""` would read as a network named nothing. */
#define ADD_NULLABLE(obj_, key_, value_)                                               \
    ((value_) != NULL ? json_add_string((obj_), (key_), (value_))                      \
                      : json_add_null((obj_), (key_)))

    ok = ok &&
         json_add_string(wifi, "state", st->wifi_state != NULL ? st->wifi_state : "off") &&
         ADD_NULLABLE(wifi, "ssid", st->wifi_ssid) &&
         (st->wifi_rssi != 0 ? json_add_int(wifi, "rssi", st->wifi_rssi)
                             : json_add_null(wifi, "rssi")) &&
         ADD_NULLABLE(wifi, "ip", st->wifi_ip) &&
         json_add_int(wifi, "failovers", st->wifi_failovers) &&
         json_add_bool(time, "synced", st->time_synced) &&
         json_add_int(time, "unix", st->time_unix) &&
         json_add_string(time, "tz", st->tz != NULL ? st->tz : "UTC0") &&
         ADD_NULLABLE(llm, "provider", st->llm_provider) &&
         ADD_NULLABLE(llm, "model", st->llm_model) &&
         json_add_bool(llm, "available", st->llm_available) &&
         ADD_NULLABLE(llm, "last_error", st->llm_last_error) &&
         json_add_int(llm, "calls_hour", st->llm_calls_hour) &&
         json_add_int(llm, "calls_day", st->llm_calls_day) &&
         (st->llm_limit_hour > 0
              ? json_add_int(llm, "limit_hour", st->llm_limit_hour)
              : json_add_null(llm, "limit_hour")) &&
         (st->llm_limit_day > 0 ? json_add_int(llm, "limit_day", st->llm_limit_day)
                                : json_add_null(llm, "limit_day")) &&
         json_add_null(obj, "geo") &&
         json_add_bool(agent, "busy", st->agent_busy) &&
         (st->agent_run_id != NULL ? json_add_string(agent, "run_id", st->agent_run_id)
                                   : json_add_null(agent, "run_id")) &&
         json_add_int(agent, "queue_depth", st->agent_queue_depth) &&
         json_add_int(agent, "max_queue", 2) &&
         json_add_int(ufs, "used_bytes", st->userfs_used) &&
         json_add_int(ufs, "total_bytes", st->userfs_total) &&
         json_add_int(wfs, "used_bytes", st->wsfs_used) &&
         json_add_int(wfs, "total_bytes", st->wsfs_total) &&
         json_add_int(storage, "workspace_quota_bytes", STORE_WS_TOTAL_BYTES) &&
         json_add_int(mem, "heap_free", st->heap_free) &&
         json_add_int(mem, "psram_free", st->psram_free) &&
         json_add_bool(sysn, "safe_mode", st->safe_mode) &&
         json_add_int(sysn, "boot_failures", st->boot_failures) &&
         json_add_int(sysn, "tool_calls", st->tool_calls) &&
         json_add_int(sysn, "tool_failures", st->tool_failures) &&
         json_add_int(obj, "uptime_s", st->uptime_s) &&
         json_add_string(obj, "boot_id",
                         st->boot_id != NULL ? st->boot_id : "");

    return ok ? ESP_OK : ESP_ERR_NO_MEM;
}
