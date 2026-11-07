/* Log bridging: the decision half of `00-overview.md` D9. Rendering a log line as
 * an NDJSON `log` frame (`04-protocol.md` §3.10) is pure, so it is tested on the
 * host; only the `esp_log_set_vprintf` install is board-bound and lives in
 * `link.c`. */

#include <stdio.h>
#include <string.h>

#include "link.h"

/* Ranked so a configured level can compare. A higher rank is more verbose; a message
 * is emitted when its rank is at or below the configured one. `none` is rank 0, so
 * it hides everything including errors, which is what `device.log_level` says. */
typedef struct {
    const char *name;
    int rank;
} level_t;

static const level_t k_levels[] = {
    {"none", 0}, {"error", 1}, {"warn", 2}, {"info", 3}, {"debug", 4},
};

/* ESP-IDF's letters (`ESP_LOG_ERROR` is 'E' and so on, plus 'V' for verbose). The
 * protocol has no `verbose`, so it is reported as `debug`: the finer grained of the
 * two is the honest answer, and §3.10's `verbose` is not in `device.log_level`. */
typedef struct {
    char letter;
    const char *name;
    int rank;
} level_letter_t;

static const level_letter_t k_letters[] = {
    {'E', "error", 1}, {'W', "warn", 2}, {'I', "info", 3},
    {'D', "debug", 4}, {'V', "debug", 4},
};

const char *link_log_filter(char level_letter, const char *configured)
{
    int want = 3; /* `03-config.md` §device: default `info` */
    if (configured != NULL) {
        for (size_t i = 0; i < sizeof(k_levels) / sizeof(k_levels[0]); i++) {
            if (strcmp(configured, k_levels[i].name) == 0) {
                want = k_levels[i].rank;
                break;
            }
        }
    }

    for (size_t i = 0; i < sizeof(k_letters) / sizeof(k_letters[0]); i++) {
        if (k_letters[i].letter == level_letter) {
            /* Rank compared: a `debug` line (4) is hidden at `info` (3). */
            return (k_letters[i].rank <= want) ? k_letters[i].name : NULL;
        }
    }
    return NULL; /* an unknown letter is not a level we can name */
}

/* ESP-IDF V1 composes `I (1234) tag: message` (`esp_log_format.h` LOG_FORMAT),
 * optionally wrapped in an ANSI colour sequence. Colour is off in this build
 * (`CONFIG_LOG_COLORS` unset) but the parser tolerates it, because turning it on is
 * a menuconfig switch a user may flip. */
static const char *skip_ansi(const char *p, const char *end)
{
    /* CSI: ESC '[' … final byte in 0x40..0x7E. */
    while (p + 1 < end && p[0] == '\033' && p[1] == '[') {
        const char *q = p + 2;
        while (q < end && (unsigned char)*q >= 0x20 && (unsigned char)*q <= 0x3F) q++;
        if (q >= end) break;
        if ((unsigned char)*q >= 0x40 && (unsigned char)*q <= 0x7E) {
            p = q + 1;
            continue;
        }
        break;
    }
    return p;
}

bool link_log_parse(const char *line, size_t len, link_log_line_t *out)
{
    if (line == NULL || out == NULL || len == 0) return false;
    const char *end = line + len;

    const char *p = skip_ansi(line, end);
    if (p >= end) return false;

    /* The level letter stands alone before a space. */
    char letter = *p++;
    if (letter != 'E' && letter != 'W' && letter != 'I' && letter != 'D' &&
        letter != 'V') {
        return false;
    }
    if (p >= end || *p != ' ') return false;
    p++;

    /* `(1234)`, or `(HH:MM:SS.mmm)` under a system-clock source. Only a bare
     * number is taken as `t_ms`; a clock time reports 0, because the frame carries
     * `ts` from NTP and conflating the two would put a time of day in a millisecond
     * field. */
    if (p >= end || *p != '(') return false;
    p++;
    int64_t t_ms = 0;
    const char *digits = p;
    while (p < end && *p >= '0' && *p <= '9') {
        /* Saturate rather than wrap: an absurd value must not become a negative. */
        if (t_ms < (INT32_MAX / 10)) t_ms = t_ms * 10 + (*p - '0');
        p++;
    }
    if (p != digits && p < end && *p == ')') {
        p++;
    } else {
        t_ms = 0;
        while (p < end && *p != ')') p++;
        if (p < end) p++;
    }
    if (p >= end || *p != ' ') return false;
    p++;

    /* `tag: message`. The tag ends at the first `: `, which is what LOG_FORMAT
     * writes; a tag containing a colon would be ambiguous, and IDF tags do not. */
    const char *tag = p;
    const char *sep = NULL;
    for (const char *q = p; q + 1 < end; q++) {
        if (q[0] == ':' && q[1] == ' ') {
            sep = q;
            break;
        }
    }
    if (sep == NULL || sep == tag) return false;
    p = sep + 2;

    /* One trailing newline belongs to the log call, not the message. */
    size_t msg_len = (size_t)(end - p);
    while (msg_len > 0 && (p[msg_len - 1] == '\n' || p[msg_len - 1] == '\r')) {
        msg_len--;
    }

    /* `LOG_FORMAT` ends with `LOG_RESET_COLOR "\n"`, so with colour on the reset
     * sequence sits between the text and the newline. Stripping it here keeps it out
     * of `msg`, where it would be a junk escape in the bridge's scrollback. */
    for (;;) {
        /* Walk back over one CSI sequence: `ESC '[' … final byte`. */
        if (msg_len < 2) break;
        size_t i = msg_len - 1;
        if ((unsigned char)p[i] < 0x40 || (unsigned char)p[i] > 0x7E) break;
        size_t j = i;
        while (j > 0 && (unsigned char)p[j - 1] >= 0x20 &&
               (unsigned char)p[j - 1] <= 0x3F) {
            j--;
        }
        if (j < 2 || p[j - 1] != '[' || p[j - 2] != '\033') break;
        msg_len = j - 2;
    }

    out->level_letter = letter;
    out->t_ms = t_ms;
    out->tag = tag;
    out->tag_len = (size_t)(sep - tag);
    out->msg = p;
    out->msg_len = msg_len;
    return true;
}

esp_err_t link_log_frame(const char *tag, const char *msg, const char *level,
                         int64_t ts, int64_t t_ms, char *out, size_t out_cap,
                         size_t *written)
{
    if (tag == NULL || msg == NULL || level == NULL || out == NULL ||
        written == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *written = 0;

    /* Built through the document builder rather than snprintf so a quote or a
     * newline in `msg` is escaped: §1.1 rule 1 forbids a literal newline in a
     * frame, and an unescaped one would desynchronise the stream. */
    json_doc_t *body = json_new_object();
    if (body == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(body);
    if (root == NULL || !json_add_string(root, "level", level) ||
        !json_add_string(root, "tag", tag) || !json_add_string(root, "msg", msg) ||
        !json_add_int(root, "t_ms", t_ms)) {
        json_free(body);
        return ESP_ERR_NO_MEM;
    }

    char id[16];
    link_next_id(id, sizeof id);
    esp_err_t err = link_envelope_wrap(body, "log", id, ts, NULL, out,
                                       out_cap, written);
    json_free(body);
    return err;
}
