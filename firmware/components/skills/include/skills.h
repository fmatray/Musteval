/* skills — the `/skills/` `.md` index and the body loader behind `skill.read`.
 * Layering: L1 (`08-code-architecture.md` §1).
 *
 * Only frontmatter is read at scan time; the body is loaded on demand, because the
 * index goes into every prompt and a body can be 8 KiB (`03-config.md` §skills). */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

#include "store.h"

/* `03-config.md` §Storage limits: 16 skills total, 8 KiB per file. The index bound
 * is what fits `agent.max_input_bytes`; skills are dropped from the end in name order
 * until the system prompt plus index fits (`tools/skills.md`, `02-firmware.md` §2). */
#define SKILLS_MAX 16
#define SKILL_NAME_MAX 33       /* `[a-z0-9_-]{1,32}` plus NUL */
#define SKILL_DESC_MAX 201      /* 1..200 plus NUL */
#define SKILL_VERSION_MAX 33
#define SKILL_BODY_DEFAULT_MAX 4096
#define SKILL_FILE_MAX (8u * 1024u)

/* One skill's frontmatter. `tools` names tools the body expects; it grants nothing
 * (`03-config.md` §skills body rule 3), so it is kept for the index only when a
 * caller asks. */
typedef struct {
    char name[SKILL_NAME_MAX];
    char description[SKILL_DESC_MAX];
    char version[SKILL_VERSION_MAX];
    size_t max_body_bytes;
    bool has_max_body;
    bool valid;
} skill_meta_t;

/* Parse the frontmatter of one skill file. `stem` is the file name without `.md`.
 *
 * Returns ESP_OK when the frontmatter is well formed and its `name` equals the stem,
 * which is `03-config.md` §Validation rules rule 11: a mismatch is a drop with a
 * logged warning, not a tool error. Anything malformed is ESP_ERR_INVALID_ARG.
 *
 * `body_offset`, when non-NULL, receives the offset of the first body byte, so
 * `skill.read` can return everything after the block without re-finding it. Pure. */
esp_err_t skills_parse(const char *stem, const char *text, size_t len,
                       skill_meta_t *out, size_t *body_offset);

/* The index appended to the system prompt: one `- <name>: <description>` line per
 * skill, in the order `out` supplies, stopping when `budget` bytes are spent.
 *
 * `written` receives the byte count and `dropped` the number of skills left out —
 * the omission is logged by the caller (`tools/skills.md`). Returns ESP_OK when even
 * zero entries fit: a budget that fits nothing is a reported drop, not a failure, and
 * an error here would make the omission silent because the caller only logs on
 * ESP_OK. Pure. */
esp_err_t skills_index(const skill_meta_t *skills, size_t count, char *out,
                       size_t cap, size_t budget, size_t *written, size_t *dropped);

/* `skill.read`'s body load. Reads `/skills/<name>.md`, checks the name against
 * `[a-z0-9_-]{1,32}`, and returns everything after the frontmatter.
 *
 * Returns ESP_OK, or ESP_ERR_INVALID_ARG for a bad name or no such file (both
 * `E_TOOL`), or ESP_ERR_INVALID_SIZE when the body exceeds the skill's
 * `max_body_bytes` — refused rather than truncated, so the model never receives a
 * silently shortened skill (`tools/skills.md`). Board-only. */
esp_err_t skills_read_body(const char *name, char *out, size_t cap, size_t *len);

/* True when `name` matches `[a-z0-9_-]{1,32}`. Pure, shared by the loader and the
 * validator. */
bool skills_name_ok(const char *name);

/* Read `/skills/` and fill `out` with the parsed frontmatter of every valid file.
 * A malformed file — including one whose `name` does not equal its stem — is skipped
 * with a logged warning (`03-config.md` §Validation rules rule 11), never fatal.
 * Returns the count in `*count`. Board-only. */
esp_err_t skills_scan(skill_meta_t *out, size_t cap, size_t *count);

/* Sort `count` entries by name ascending, so the index order is deterministic and
 * "drop from the end" means the last name (`tools/skills.md`). Pure. */
void skills_sort(skill_meta_t *skills, size_t count);

/* The scanned set, cached for the current boot and refreshed by `reload`. Returns a
 * borrowed pointer; `*count` may be NULL. Board-only. */
const skill_meta_t *skills_current(size_t *count);
