/* skills_index — frontmatter parsing and the system-prompt index.
 *
 * Pure, including the drop-to-fit rule, because "skills were omitted" is a fact a
 * user can hit and the reply must already account for it (`tools/skills.md`). */

#include "skills.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"

static const char *TAG = "skills";

bool skills_name_ok(const char *name)
{
    if (name == NULL) return false;
    size_t n = strlen(name);
    /* agentskills.io: 1..64 characters, `[a-z0-9-]` only, no leading or trailing
     * hyphen, no consecutive hyphens. Checked here once; every caller delegates. */
    if (n == 0 || n > 64) return false;
    if (name[0] == '-' || name[n - 1] == '-') return false;
    bool previous_hyphen = false;
    for (size_t i = 0; i < n; i++) {
        char c = name[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-';
        if (!ok) return false;
        if (c == '-' && previous_hyphen) return false;
        previous_hyphen = c == '-';
    }
    return true;
}

/* `---\n` at the very start, or no frontmatter at all. */
static bool frontmatter_open(const char *text, size_t len, size_t *after)
{
    if (len < 3) return false;
    if (strncmp(text, "---", 3) != 0) return false;
    size_t i = 3;
    if (i < len && text[i] == '\r') i++;
    if (i < len && text[i] == '\n') i++;
    *after = i;
    return true;
}

/* Copy a trimmed `key: value` scalar. Quoted values lose their quotes, which is how
 * `description: "quoted"` parses without the quote marks. */
static void scalar(const char *line, size_t len, char *dst, size_t cap)
{
    size_t end = len;
    while (end > 0 && (line[end - 1] == '\r' || line[end - 1] == ' ')) end--;
    size_t start = 0;
    while (start < end && line[start] == ' ') start++;
    if (end - start >= 2 && (line[start] == '"' || line[start] == '\'') &&
        line[end - 1] == line[start]) {
        start++;
        end--;
    }
    size_t n = end - start;
    if (n >= cap) n = cap - 1;
    memcpy(dst, line + start, n);
    dst[n] = '\0';
}

esp_err_t skills_parse(const char *dir_name, const char *text, size_t len,
                       skill_meta_t *out, size_t *body_offset)
{
    if (dir_name == NULL || text == NULL || out == NULL) return ESP_ERR_INVALID_ARG;
    memset(out, 0, sizeof *out);

    size_t at = 0;
    if (!frontmatter_open(text, len, &at)) {
        /* A file without frontmatter is not a skill: the index is built from
         * frontmatter alone (`03-config.md` §skills). */
        return ESP_ERR_INVALID_ARG;
    }

    bool have_desc = false;
    for (;;) {
        if (at >= len) return ESP_ERR_INVALID_ARG;  /* no closing `---` */
        size_t line_start = at;
        size_t line_end = at;
        while (line_end < len && text[line_end] != '\n') line_end++;
        size_t next = line_end < len ? line_end + 1 : line_end;

        /* A `---` line closes the block. */
        if (line_end - line_start >= 3 &&
            strncmp(text + line_start, "---", 3) == 0) {
            size_t after = next;
            /* A blank line right after the block is separators, not body. */
            while (after < len && (text[after] == '\n' || text[after] == '\r')) after++;
            if (body_offset != NULL) *body_offset = after;
            break;
        }

        const char *line = text + line_start;
        size_t line_len = line_end - line_start;

        const char *colon = memchr(line, ':', line_len);
        if (colon != NULL) {
            size_t key_len = (size_t)(colon - line);
            const char *value = colon + 1;
            size_t value_len = line_len - key_len - 1;
            /* Trim the value's leading space once, so `description: x` and
             * `description:x` both work. */
            while (value_len > 0 && *value == ' ') {
                value++;
                value_len--;
            }

            /* Key matching is exact-length, which is also what keeps a member of a
             * nested map out: `  name:` inside `metadata:` has key_len 6, so it can
             * never be read as the skill's own name. */
            if (key_len == 4 && strncmp(line, "name", 4) == 0) {
                scalar(value, value_len, out->name, sizeof out->name);
            } else if (key_len == 11 && strncmp(line, "description", 11) == 0) {
                scalar(value, value_len, out->description, sizeof out->description);
                have_desc = out->description[0] != '\0';
            }
            /* `license`, `compatibility`, `metadata` and `allowed-tools` are the
             * spec's optional fields and are not parsed: the index needs neither,
             * and an unread value cannot weaken a gate (`03-config.md` §skills body
             * rule 3). */
        }

        at = next;
    }

    if (!skills_name_ok(out->name) || !have_desc) return ESP_ERR_INVALID_ARG;
    /* The spec requires `name` to match the parent directory name; a mismatch drops
     * the skill, and `skill.read` then has no such skill (`tools/skills.md`). */
    if (strcmp(out->name, dir_name) != 0) {
        ESP_LOGW(TAG, "%s/SKILL.md declares name %s; dropped", dir_name, out->name);
        return ESP_ERR_INVALID_ARG;
    }

    out->valid = true;
    return ESP_OK;
}

esp_err_t skills_index(const skill_meta_t *skills, size_t count, char *out,
                       size_t cap, size_t budget, size_t *written, size_t *dropped)
{
    if (out == NULL || cap == 0) return ESP_ERR_INVALID_ARG;
    if (written != NULL) *written = 0;
    if (dropped != NULL) *dropped = 0;

    size_t at = 0;
    size_t kept = 0;
    for (size_t i = 0; i < count; i++) {
        if (!skills[i].valid) continue;
        int n = snprintf(out + at, cap - at, "- %s: %s\n", skills[i].name,
                         skills[i].description);
        if (n < 0 || (size_t)n >= cap - at) break;
        /* The budget is a ceiling on what the index may add to the prompt, so a line
         * that would cross it is where dropping starts — and it starts at the end of
         * the name order, which is the incoming order. */
        if (at + (size_t)n > budget) break;
        at += (size_t)n;
        kept++;
    }
    out[at] = '\0';
    if (written != NULL) *written = at;
    if (dropped != NULL) {
        size_t valid = 0;
        for (size_t i = 0; i < count; i++) {
            if (skills[i].valid) valid++;
        }
        *dropped = valid - kept;
    }
    /* A budget that fits nothing is a reported drop, not a failure: the caller logs
     * it and carries on with no index. Returning an error here made that case silent,
     * because `agent.c` only logs when the call succeeds. */
    return ESP_OK;
}
