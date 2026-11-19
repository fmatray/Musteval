/* skills — the `/skills/<name>/SKILL.md` index and the body loader behind `skill.read`.
 * Layering: L1 (`08-code-architecture.md` §1).
 *
 * Only frontmatter is read at scan time; the body is loaded on demand, because the
 * index goes into every prompt and a body can be 8 KiB (`03-config.md` §skills). */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

#include "store.h"

/* `03-config.md` §skills: one directory per skill, `SKILL.md` inside it, in the
 * agentskills.io layout. 16 skills total; the index bound is what fits
 * `agent.max_input_bytes`, and skills are dropped from the end in name order until
 * the system prompt plus index fits (`tools/skills.md`, `02-firmware.md` §2). */
#define SKILLS_MAX 16
#define SKILL_NAME_MAX 65       /* spec: 1..64 characters plus NUL */
/* The spec allows 1024 characters, but the whole index is `AGENT_SKILL_INDEX_MAX`
 * (1024 bytes, `agent.c`), so a longer line could never be shown: the index is a
 * pointer to the body, not the instructions. Keeping 1025 bytes per skill spent
 * 13 KiB of static RAM for text no prompt can hold. A description over this bound
 * is accepted and clipped, never refused (`03-config.md` §skills). */
#define SKILL_DESC_MAX 257
#define SKILL_BODY_DEFAULT_MAX 4096
#define SKILL_FILE_MAX (8u * 1024u)
/* The file every skill directory must hold. The spec fixes this name; a directory
 * without it is not a skill. */
#define SKILL_FILE_NAME "SKILL.md"

/* One skill's frontmatter. The spec defines `name` and `description` as required and
 * permits `license`, `compatibility`, `metadata` and `allowed-tools`; none of the
 * optional fields is read here, because the index needs neither and an unread field
 * cannot grant anything (`03-config.md` §skills body rule 3). */
typedef struct {
    char name[SKILL_NAME_MAX];
    char description[SKILL_DESC_MAX];
    bool valid;
} skill_meta_t;

/* Parse the frontmatter of one `SKILL.md`. `dir_name` is the skill's directory name.
 *
 * Returns ESP_OK when the frontmatter is well formed and its `name` equals
 * `dir_name`, which the agentskills.io spec requires ("Must match the parent
 * directory name"). A mismatch is a drop with a logged warning, not a tool error.
 * Anything malformed is ESP_ERR_INVALID_ARG.
 *
 * `body_offset`, when non-NULL, receives the offset of the first body byte, so
 * `skill.read` can return everything after the block without re-finding it. Pure. */
esp_err_t skills_parse(const char *dir_name, const char *text, size_t len,
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

/* `skill.read`'s body load. Reads `/skills/<name>/SKILL.md`, checks the name against
 * the spec's `[a-z0-9-]` rule, and returns everything after the frontmatter.
 *
 * Returns ESP_OK, or ESP_ERR_INVALID_ARG for a bad name or no such file (both
 * `E_TOOL`), or ESP_ERR_INVALID_SIZE when the body does not fit `cap` — refused
 * rather than truncated, so the model never receives a silently shortened skill
 * (`tools/skills.md`). Board-only. */
esp_err_t skills_read_body(const char *name, char *out, size_t cap, size_t *len);

/* True when `name` is a legal skill directory name: 1..64 characters of `[a-z0-9-]`,
 * no leading or trailing hyphen, no consecutive hyphens. The agentskills.io spec's
 * rule, and the one place it is written. Pure, shared by the loader and the scan. */
bool skills_name_ok(const char *name);

/* Read `/skills/` and fill `out` with the parsed frontmatter of every valid skill
 * directory. A directory without `SKILL.md`, or one whose frontmatter is malformed
 * or whose `name` does not equal the directory name, is skipped with a logged
 * warning, never fatal. Returns the count in `*count`. Board-only. */
esp_err_t skills_scan(skill_meta_t *out, size_t cap, size_t *count);

/* Sort `count` entries by name ascending, so the index order is deterministic and
 * "drop from the end" means the last name (`tools/skills.md`). Pure. */
void skills_sort(skill_meta_t *skills, size_t count);

/* The scanned set, cached for the current boot and refreshed by `reload`. Returns a
 * borrowed pointer; `*count` may be NULL. Board-only. */
const skill_meta_t *skills_current(size_t *count);
