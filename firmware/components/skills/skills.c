/* skills.c — the board half: scan `/skills/`, and load one body for `skill.read`.
 *
 * The parse and the index are pure and live in `skills_index.c`; this file only
 * touches `userfs`. */

#include "skills.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"

static const char *TAG = "skills";

/* Bodies are read whole, never truncated, so one buffer sized for the `userfs` cap
 * serves every skill (`03-config.md` §Storage limits). Static: a run's stack is
 * 4096 B. */
static char s_file[SKILL_FILE_MAX + 1];
static skill_meta_t s_meta[SKILLS_MAX];
/* The scan's directory listing, for the same reason `s_file` is static: as a local,
 * `store_entry_t entries[SKILLS_MAX]` made the `skills_scan` frame 1312 B, and
 * `skills_scan` is reached from `agent_start` on `main`'s 3584 B stack with only
 * ~1080 B left — a boot crash. File scope takes the frame to 160 B.
 * `skills_scan` is already non-reentrant through `s_meta`. */
static store_entry_t s_entries[SKILLS_MAX];

esp_err_t skills_read_body(const char *name, char *out, size_t cap, size_t *len)
{
    if (len != NULL) *len = 0;
    if (name == NULL || out == NULL) return ESP_ERR_INVALID_ARG;
    /* A name outside the charset is refused rather than clamped, so the tool cannot
     * be used to enumerate `/skills/` through error text (`tools/skills.md`). */
    if (!skills_name_ok(name)) return ESP_ERR_INVALID_ARG;

    /* agentskills.io: the skill is a directory holding `SKILL.md`. */
    char path[96];
    int n = snprintf(path, sizeof path, "/skills/%s/" SKILL_FILE_NAME, name);
    if (n < 0 || (size_t)n >= sizeof path) return ESP_ERR_INVALID_ARG;

    size_t file_len = 0;
    esp_err_t err = store_read_file(path, s_file, SKILL_FILE_MAX, &file_len);
    if (err != ESP_OK) return err;  /* a missing skill and a read failure are both E_TOOL */

    skill_meta_t meta;
    size_t body_at = 0;
    err = skills_parse(name, s_file, file_len, &meta, &body_at);
    if (err != ESP_OK) return ESP_ERR_INVALID_ARG;

    size_t body_len = file_len - body_at;
    /* Over the bound is a refusal, not a truncation: the model must not act on half
     * an instruction set. The bound is the reader's cap, not a per-file frontmatter
     * field: the spec has no such field (`tools/skills.md`). */
    if (body_len >= cap) {
        return ESP_ERR_INVALID_SIZE;
    }
    memcpy(out, s_file + body_at, body_len);
    out[body_len] = '\0';
    if (len != NULL) *len = body_len;
    return ESP_OK;
}

/* Read `/skills/` and return the parsed frontmatter, sorted by name so the index
 * order is deterministic (the drop-to-fit rule drops "from the end in name order",
 * `tools/skills.md`). Returns the count; a directory without a readable `SKILL.md`,
 * or with malformed frontmatter, is skipped with a warning, never fatal. */
esp_err_t skills_scan(skill_meta_t *out, size_t cap, size_t *count)
{
    if (out == NULL || count == NULL) return ESP_ERR_INVALID_ARG;
    *count = 0;

    size_t found = 0;
    /* `SKILLS_MAX` directories, because a `/skills/` holding more entries than that
     * is refused by name rather than silently truncated to the first sixteen. */
    esp_err_t err = store_list_dir("/skills/", s_entries, SKILLS_MAX, &found);
    if (err == ESP_ERR_NOT_FOUND) return ESP_OK;  /* no tree yet is not a failure */
    if (err != ESP_OK) return err;

    size_t at = 0;
    for (size_t i = 0; i < found && at < cap; i++) {
        /* agentskills.io: each skill is a directory, and the file inside it is what
         * carries the instructions. A loose `.md` in `/skills/` is not a skill. */
        const char *name = s_entries[i].name;
        if (!s_entries[i].dir || !skills_name_ok(name)) continue;

        char path[96];
        int n = snprintf(path, sizeof path, "/skills/%s/" SKILL_FILE_NAME, name);
        if (n < 0 || (size_t)n >= sizeof path) continue;

        size_t file_len = 0;
        if (store_read_file(path, s_file, SKILL_FILE_MAX, &file_len) != ESP_OK) {
            ESP_LOGW(TAG, "%s: no readable " SKILL_FILE_NAME ", dropped", name);
            continue;
        }
        if (skills_parse(name, s_file, file_len, &out[at], NULL) != ESP_OK) {
            continue;  /* the parser already logged why */
        }
        at++;
    }
    *count = at;
    return ESP_OK;
}

/* Ascending by name, so `skills_index`'s "drop from the end" means the last name.
 * A simple insertion sort: at most sixteen entries. */
void skills_sort(skill_meta_t *skills, size_t count)
{
    for (size_t i = 1; i < count; i++) {
        skill_meta_t key = skills[i];
        size_t j = i;
        while (j > 0 && strcmp(skills[j - 1].name, key.name) > 0) {
            skills[j] = skills[j - 1];
            j--;
        }
        skills[j] = key;
    }
}

const skill_meta_t *skills_current(size_t *count)
{
    if (count != NULL) *count = 0;
    size_t found = 0;
    if (skills_scan(s_meta, SKILLS_MAX, &found) != ESP_OK) return s_meta;
    skills_sort(s_meta, found);
    if (count != NULL) *count = found;
    return s_meta;
}
