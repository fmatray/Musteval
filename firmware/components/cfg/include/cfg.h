/* cfg — `config.json` parsing and `nvs:<key>` secret resolution.
 * Layering: L0 (`08-code-architecture.md` §1). Sole owner of the `secrets`
 * namespace and of redaction (`08-code-architecture.md` §3), so no other module
 * reads a secret directly.
 * Contracts: `02-firmware.md` §`cfg`, `03-config.md` §config.json,
 * `04-protocol.md` §4.8-§4.10. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "store.h"

/* `/config.json` is capped at 8 KiB (`03-config.md` §Storage limits), and the whole
 * document is held in RAM between loads, so this is a hard ceiling, not a hint. */
#define CFG_JSON_MAX 8192

/* Longest secret value the `secrets` namespace will accept, in bytes. Bound from
 * `03-config.md` §config.json rule 3 plus the NVS entry limit. */
#define CFG_SECRET_MAX 512

/* Load `/config.json` into the process-wide document. Returns ESP_ERR_NOT_FOUND
 * when the file is absent (a blank device is not a failure), ESP_ERR_INVALID_SIZE
 * when it exceeds `CFG_JSON_MAX`, ESP_ERR_INVALID_ARG when it does not parse or
 * fails validation. The previous document is kept on failure, so a bad `config.set`
 * cannot leave the device with no config at all.
 *
 * `degraded`, when non-NULL and of a non-zero `degraded_cap`, receives the reason a
 * loaded document is not fully usable — today only an unresolved `*_ref`, which is
 * `E_CONFIG` naming the key and not a refusal (`07-roadmap.md` M2 boots anyway).
 * `reload` relays it in its `warnings` array (`04` §4.2); empty means nothing to say. */
esp_err_t cfg_load_degraded(char *degraded, size_t degraded_cap);
esp_err_t cfg_load(void);

/* Takes ownership of a validated document, replacing the loaded one. Used by
 * `cfg_load`, which parses the file and hands the result over. */
void cfg_adopt(json_doc_t *doc);

/* Drop the loaded document. Safe to call when nothing is loaded. */
void cfg_unload(void);

/* True when a document is loaded. Commands answer `E_CONFIG`, never a crash, when
 * this is false (`04` §5). */
bool cfg_loaded(void);

/* Borrowed pointers into the loaded document; NULL when no document is loaded or
 * the path is absent. `cfg_get` never copies, so the caller must not keep the
 * pointer across a `cfg_load`. */
json_node_t *cfg_get(const char *path);

/* Deep copy of the whole loaded document into `out`, which the caller owns. The
 * copy is what `config.set` mutates, so a failed write never touches the live
 * config. NULL when nothing is loaded. */
json_doc_t *cfg_clone(void);

/* Replace the value at `path` in the *caller's* document copy. This is the only
 * mutation `config.set` performs. A missing *object* parent is created; a missing
 * array or array index is not (`04-protocol.md` §4.9). */
bool cfg_set(json_doc_t *doc, const char *path, const json_node_t *value);

/* Whether a write to `path` needs a reboot before it takes effect: true for the
 * boot-only prefixes `04-protocol.md` §4.2 names (`wifi.`, `serial.`, and
 * `ntp.servers`), false otherwise. Pure, so the rule is unit-tested. */
bool cfg_needs_reboot(const char *path);

/* True for a path whose value the running firmware holds a copy of, so a set to it
 * is not applied until the copy is re-read — today only `llm.*`, which `llm_init`
 * owns and `config.set` re-runs (`02-firmware.md` §Request body). */
bool cfg_is_live_path(const char *path);

/* Serialise `doc` canonically into `buf` (`03-config.md` §config.json, `AGENTS.md`
 * rule 4). `*written` excludes the terminator. */
esp_err_t cfg_emit(json_doc_t *doc, char *buf, size_t cap, size_t *written);

/* Validate a document against the frozen key tree and bounds: `03-config.md`
 * §Full schema, its five notes, and validation steps 3-8. Returns ESP_OK or
 * ESP_ERR_INVALID_ARG, and writes a short reason into `why` for the command's
 * `message` field. `why` may be NULL. Pure: no NVS, no filesystem. */
esp_err_t cfg_validate(json_doc_t *doc, char *why, size_t why_cap);

/* Step 9 of the same sequence, split out because it reads NVS and so cannot be
 * pure: every `*_ref` string in the document must resolve in namespace `secrets`.
 * ESP_ERR_NOT_FOUND names the first missing key in `why`; ESP_ERR_INVALID_ARG is a
 * ref that is not `nvs:` plus 1..15 `[A-Za-z0-9_]`. */
esp_err_t cfg_validate_refs(json_doc_t *doc, char *why, size_t why_cap);

/* Resolve `nvs:<key>` to plaintext (`02-firmware.md` §`cfg`). Returns
 * ESP_ERR_INVALID_ARG for a ref that is not `nvs:` plus 1..15 `[A-Za-z0-9_]`, and
 * ESP_ERR_NOT_FOUND when the key is unset: the empty-string substitution rule 3
 * forbids is deliberately not available here. */
esp_err_t cfg_resolve_secret(const char *ref, char *buf, size_t cap, size_t *len);

/* True when `ref` has the `^nvs:[A-Za-z0-9_]{1,15}$` shape. Pure. */
bool cfg_ref_is_valid(const char *ref);

/* Replace every secret *value* that appears in `line` in place. A line is redacted
 * by exact substring match against the secrets the caller names, so a caller with
 * no secrets passes n=0 and pays nothing. Returns true when anything was replaced.
 * `line` is modified; `cap` is its whole buffer including the terminator. */
bool cfg_redact(char *line, size_t cap, const char *const *secrets, size_t n);

/* Write a secret into NVS namespace `secrets` (`04` §4.10). An empty value is
 * ESP_ERR_INVALID_ARG, never a deletion. `*stored` is the byte length written. */
esp_err_t cfg_store_secret(const char *key, const char *value, size_t *stored);
