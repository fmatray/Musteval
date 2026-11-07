/* store — the only JSON entry points and the only path resolver in the firmware.
 * Layering: L0 (`08-code-architecture.md` §1). No module builds a path from a
 * model string and no module calls cJSON (`08` §3). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <stdio.h>

#include "esp_err.h"

/* --- JSON (`03-config.md` §canonical, `08` §3) ----------------------------- */

typedef struct json_doc json_doc_t;
typedef struct json_node json_node_t;

/* Parse `len` bytes of `buf`. Fails with ESP_ERR_INVALID_ARG on anything that is
 * not exactly one JSON value; the caller maps that to a wire error.
 *
 * On failure `*error_offset` (when non-NULL) is the 0-based byte offset of the
 * parse error, or `len` when the bytes parse but trailing non-whitespace follows.
 * `04-protocol.md` §1.2 requires the refusal `message` to name that offset, so the
 * caller cannot reconstruct it after the fact. */
esp_err_t json_parse_at(const char *buf, size_t len, json_doc_t **out,
                        size_t *error_offset);

/* `json_parse_at` without the offset. */
esp_err_t json_parse(const char *buf, size_t len, json_doc_t **out);

/* Canonical emit: object keys sorted bytewise, no trailing commas, no whitespace
 * outside strings (`AGENTS.md` canonical JSON). Writes a NUL terminator after
 * `*written`, which does not count toward `cap`. */
esp_err_t json_emit(json_doc_t *doc, char *buf, size_t cap, size_t *written);

void json_free(json_doc_t *doc);

/* A deep copy of a whole document, or NULL. The copy owns its nodes, so freeing the
 * original first is safe. */
json_doc_t   *json_duplicate(const json_doc_t *doc);

/* Builders: an empty document and typed members added to an object node. Without
 * these no module could ever construct a frame or a result, only parse one.
 * `json_add_*` return false when the parent is not an object or the value is
 * refused; a failed add leaves the document unchanged. Keys are inserted in
 * whatever order they are added — `json_emit` is what imposes canonical order. */
json_doc_t   *json_new_object(void);
json_node_t  *json_new_array(json_node_t *parent, const char *key);
bool          json_add_string(json_node_t *obj, const char *key, const char *value);
bool          json_add_int(json_node_t *obj, const char *key, int64_t value);
bool          json_add_bool(json_node_t *obj, const char *key, bool value);
bool          json_add_null(json_node_t *obj, const char *key);
json_node_t  *json_add_object(json_node_t *obj, const char *key);
/* Append to an existing array node. */
json_node_t  *json_array_push_string(json_node_t *array, const char *value);
json_node_t  *json_array_push_int(json_node_t *array, int64_t value);

/* Accessors. A missing key or a wrong type is reported, never asserted: config
 * files are user input. All return NULL/false rather than failing the doc. */
json_node_t *json_root(json_doc_t *doc);
json_node_t *json_get(const json_node_t *obj, const char *key);
bool         json_is_object(const json_node_t *node);
bool         json_is_array(const json_node_t *node);
bool         json_is_string(const json_node_t *node);
bool         json_is_int(const json_node_t *node);
bool         json_is_bool(const json_node_t *node);
bool         json_is_null(const json_node_t *node);

/* Borrowed pointer into the document, valid until `json_free`. NULL when the node
 * is not a string. Copy it before freeing the doc. */
const char *json_string(const json_node_t *node);
bool        json_int(const json_node_t *node, int64_t *out);
bool        json_bool(const json_node_t *node, bool *out);

size_t       json_array_len(const json_node_t *node);
json_node_t *json_array_at(const json_node_t *node, size_t index);
/* Append a fresh empty object to an array node; NULL when it is not an array. */
json_node_t *json_array_push_object(json_node_t *array);

/* Deep copy `node` in under `key`. Returns the copy, or NULL when `obj` is not an
 * object or memory ran out. The copy is owned by `obj` and freed with it. */
json_node_t *json_add_node(json_node_t *obj, const char *key, const json_node_t *node);

/* Iterate an object's members in document order: the index is 0-based and
 * `json_object_key` returns "" for a member with no key. A non-object has length 0.
 * `json_object_value` is the member itself, so a validator can walk an arbitrary
 * tree without knowing any key in advance. */
size_t       json_object_len(const json_node_t *obj);
const char  *json_object_key(const json_node_t *obj, size_t index);
json_node_t *json_object_value(const json_node_t *obj, size_t index);

/* Resolve a dotted path against `doc` (`04` §4.8): `llm.providers.0.model`. A
 * numeric segment indexes an array, any other segment is an object key. NULL when
 * a segment is missing or the node at that point has the wrong shape. */
json_node_t *json_path_get(json_doc_t *doc, const char *path);

/* Replace the value at `path` with a deep copy of `value`. Every intermediate key
 * must already exist and be an object (or an array at a numeric segment); a set
 * never creates a parent, so a typo in a path cannot invent config. Returns false
 * and leaves the document untouched on any failure. */
bool json_path_set(json_doc_t *doc, const char *path, const json_node_t *value);

/* --- Paths (`02-firmware.md` §store, `06-security.md` §6.1) ---------------- */

/* Mount points. `esp_vfs_register` rejects a `base_path` ending in `/`
 * (`is_path_prefix_valid` in `vfs.c`), so these carry none. */
#define STORE_VFS_WORKSPACE "/workspace"
#define STORE_VFS_USERFS    "/fs"

/* Agent-path prefixes for the same trees: the mount point plus the separator a
 * segment match needs. Derived, so the two cannot drift apart. */
#define STORE_ROOT_WORKSPACE STORE_VFS_WORKSPACE "/"
#define STORE_ROOT_USERFS    STORE_VFS_USERFS "/"

/* `esp_vfs_register` refuses a `base_path` ending in `/` (`is_path_prefix_valid`,
 * `vfs.c`), and that refusal only surfaces as a dead device at boot. A trailing
 * slash is a size change, so pin the sizes. These live in the header, not in
 * `store.c`, because `store.c` is excluded from the `linux` target and these must
 * compile in the host suite too. `02-firmware.md` §store. */
_Static_assert(sizeof(STORE_VFS_USERFS) == 4, "STORE_VFS_USERFS must be \"/fs\"");
_Static_assert(sizeof(STORE_VFS_WORKSPACE) == 11,
               "STORE_VFS_WORKSPACE must be \"/workspace\"");

/* Agent path -> VFS path. Pure prefix substitution, no filesystem access:
 * `/workspace/...` stays, anything else is rooted at `/fs/`. */
esp_err_t store_map_path(const char *agent_path, char *out, size_t out_cap);

/* Accept `in` only if it canonicalises to strictly under `/workspace/` with at
 * least one further segment. Rejects `..` that escapes, relative paths, NUL
 * bytes and over-long paths with E_TOOL. */
esp_err_t store_resolve_write_path(const char *in, char *out, size_t out_cap);

/* True when `path` is `dir` itself or lies below it. Whole segments are compared,
 * so `/workspace/a` is not under `/workspace/ab`. Pure. */
bool store_path_is_under(const char *dir, const char *path);

/* Canonicalise a read-only path: reject relative, `.`/`..` escapes, NUL bytes and
 * over-long input, then return the VFS path. Unlike `store_resolve_write_path` this
 * accepts anywhere (`/config.json`, `/prompts/brief.md`), because the host commands
 * read what the image ships; it is never used for a model-supplied path. Pure. */
esp_err_t store_resolve_image_path(const char *in, char *out, size_t out_cap);

/* Largest VFS path any `store_*` call produces; sized for a full agent path plus
 * the `/fs/` prefix. A caller's buffer may be smaller, which the calls reject. */
#define STORE_PATH_MAX 512

/* --- Filesystem (`02-firmware.md` §store) ---------------------------------- */

/* Register `userfs` at `/fs` and `wsfs` at `/workspace`. `userfs` failure is fatal
 * and never formats; `wsfs` failure formats once and retries. Idempotent. */
esp_err_t store_mount(void);

/* Bytes used and total for one mounted filesystem. `store` owns the littlefs labels
 * (`store.c`), so this is the only way a caller reads them without reaching past the
 * layering law (`08-code-architecture.md` §3). Returns ESP_ERR_NOT_FOUND when the
 * label is not mounted. */
typedef enum {
    STORE_FS_USERFS = 0, /* `/fs`      */
    STORE_FS_WSFS,       /* `/workspace` */
} store_fs_t;

esp_err_t store_fs_usage(store_fs_t fs, size_t *used, size_t *total);

/* --- NVS `runtime` namespace (`03-config.md` §runtime state) --------------- */

/* Initialize NVS and create the three namespaces `07-roadmap.md` M0 requires to
 * exist after boot: `runtime`, `secrets` and `memory`. Opening a namespace
 * read-write is what creates it, so an unused `secrets` still exists for `cfg`.
 * Must run before any `store_nvs_*` call: `nvs_open` returns
 * `ESP_ERR_NVS_NOT_INITIALIZED` otherwise, which would silently disable the boot
 * guard (`02-firmware.md` §3.2). A fresh or version-migrated partition is erased
 * and retried once — the documented idiom, since the alternative is a device that
 * never boots. Erasing costs the counters; it is the case that has no other exit. */
esp_err_t store_init(void);

esp_err_t store_nvs_set_int(const char *key, int32_t value);
esp_err_t store_nvs_get_int(const char *key, int32_t *out);

/* Namespace `secrets` (`03-config.md` §config.json rule 3). The value is written
 * verbatim and never read back by any caller that emits it; `store_nvs_get_str`
 * exists for the modules that resolve a `nvs:<key>` reference internally, and its
 * out-buffer is the caller's. `E_CONFIG`-shaped refusals are the caller's map:
 * an empty key or value is ESP_ERR_INVALID_ARG. */
esp_err_t store_nvs_set_str(const char *namespace, const char *key,
                            const char *value, size_t *out_len);
esp_err_t store_nvs_get_str(const char *namespace, const char *key,
                            char *out, size_t cap, size_t *out_len);

/* --- Workspace quota (`06-security.md` §6.2) ------------------------------ */

#define STORE_WS_TOTAL_BYTES (64u * 1024u)
#define STORE_WS_FILE_COUNT  64u
#define STORE_WS_FILE_BYTES  (16u * 1024u)

/* The directory as it stands. `replacing_bytes`/`replacing` say the target already
 * exists, so an overwrite counts the new size, never both (§6.2). */
typedef struct {
    size_t files;           /* entries under `/workspace/`, recursively */
    size_t bytes;           /* sum of their sizes */
    bool   replacing;       /* the target path already exists */
    size_t replacing_bytes; /* its current size, ignored unless `replacing` */
} store_quota_t;

/* True when writing `new_size` at `target` would cross a limit. Checked before
 * any truncation, so a refused write cannot destroy what is already there (§6.2).
 * A quota that would already be exceeded is refused too: the file count is only
 * allowed to fall. */
bool store_quota_exceeded(const store_quota_t *q, size_t new_size);

/* --- Chunked uploads (`04-protocol.md` §4.16, §4.18) ---------------------- */

#define STORE_CHUNK_BYTES 2048u

/* Per-file caps the assembled upload is held to, keyed by target directory.
 * `04` §4.16: an import gets no larger budget than the file it could have
 * replaced. Returns 0 for a path with no directory cap (the sandbox), which is
 * then bounded by the workspace quota alone. */
size_t store_import_cap(const char *agent_path);

/* Staging sequence: chunk `offset` continues a transfer only when it equals the
 * bytes already staged, so two interleaved uploads fail the check instead of
 * splicing (`04` §4.16). `offset == 0` always starts a new transfer. */
bool store_chunk_fits(size_t offset, size_t chunk_raw, size_t staged);

/* --- Sandbox (`04-protocol.md` §4.17-§4.20) ------------------------------- */

/* One directory entry. `name` is the entry name alone, never a path. */
#define STORE_ENTRY_NAME_MAX 64

typedef struct {
    char   name[STORE_ENTRY_NAME_MAX];
    bool   dir;
    size_t bytes;
} store_entry_t;

/* List one directory under `/workspace/`. Entries are directories first, then
 * name, both ascending (§4.17). More than `cap` entries is ESP_ERR_INVALID_SIZE
 * rather than a truncated list. `*count` is set only on ESP_OK. The sandbox root
 * is legal here even though it is not a legal file target. */
/* The same listing, but admitted through the image rule rather than the sandbox
 * rule, so `templates.list` can see `/prompts` (`04` §4.17). */
esp_err_t store_list_image_dir(const char *agent_path, store_entry_t *out, size_t cap,
                               size_t *count);

esp_err_t store_list_dir(const char *agent_path, store_entry_t *out, size_t cap,
                         size_t *count);

/* True when `entry_name` is the staging file a live transfer is writing under
 * `vfs_dir`. `store_list_dir` skips it because §4.16 forbids exposing partial
 * state; the match is exact, so a real `<name>.new` is still listed. */
bool store_is_staging_entry(const char *vfs_dir, const char *entry_name,
                            const char *stage_path);

/* Read up to `cap` bytes of text. `*truncated` is true when the file was longer,
 * `*len` is what was read and `*total` is the file's whole length, so the wire can
 * report what the cap left behind (§4.18). Content that is not valid UTF-8 is
 * ESP_ERR_INVALID_SIZE, which the link maps to `E_STORAGE`. */
esp_err_t store_read_text(const char *agent_path, char *buf, size_t cap,
                          size_t *len, size_t *total, bool *truncated);

/* Read one whole file from anywhere the image ships (`/config.json`,
 * `/prompts/brief.md`) into `buf`, NUL-terminated. Not confined to the sandbox and
 * never truncated: a file that does not fit `cap` is ESP_ERR_INVALID_SIZE, so a
 * caller never parses half a config. `*len` excludes the terminator. */
esp_err_t store_read_file(const char *agent_path, char *buf, size_t cap, size_t *len);

/* Append one chunk of a staged write (§4.20). `offset == 0` restarts the
 * transfer; any other offset that is not the staged size is
 * ESP_ERR_INVALID_STATE. `final` commits by renaming the staged file over the
 * target after the quota check, so a refused or abandoned transfer leaves the
 * previous file intact. `*out_bytes` is the assembled size, `*out_created` says
 * the target did not exist before. */
esp_err_t store_write_chunk(const char *agent_path, const uint8_t *data,
                            size_t len, size_t offset, bool final,
                            size_t *out_bytes, bool *out_created);

/* Same protocol as `store_write_chunk`, but for the host-only import path: the
 * target may be anywhere the image ships (`/config.json`, `/prompts/x.md`), bounded
 * by `store_import_cap` instead of the workspace quota. The caller refuses a path
 * naming a built-in file (`04` §4.16). */
esp_err_t store_write_image_chunk(const char *agent_path, const uint8_t *data,
                                  size_t len, size_t offset, bool final,
                                  size_t *out_bytes, bool *out_created);

/* Delete one file, one empty directory, or — with `recursive` — a whole subtree
 * (§4.19). `*entries` is the immediate child count when a populated directory is
 * refused for want of `recursive`, and the number of descendants removed on
 * success. */
esp_err_t store_delete_file(const char *agent_path, bool recursive, size_t *entries);

/* Create one directory; intermediates are never created (§4.21). `*created` is
 * false when the directory was already there. A missing parent is
 * ESP_ERR_INVALID_ARG, which `main` reports as E_CONFIG with `detail`. */
esp_err_t store_mkdir(const char *agent_path, bool *created);

/* Rename one file or directory inside `/workspace/` (§4.22). Both paths are
 * canonicalised, an existing `to` is never overwritten, and a directory carries
 * its subtree. A missing source is ESP_ERR_NOT_FOUND; a taken target or a move
 * into the source itself is ESP_ERR_INVALID_STATE (`main` maps both to the wire
 * codes §4.22 names). */
esp_err_t store_move(const char *from, const char *to);

/* `true` when `agent_path` names a directory. */
esp_err_t store_is_dir(const char *agent_path, bool *out);

/* --- §4.19 / §4.22 decisions, pure so the host suite can fail them --------- */

typedef enum {
    STORE_DELETE_FILE,            /* not a directory: unlink */
    STORE_DELETE_EMPTY,           /* empty directory: rmdir */
    STORE_DELETE_TREE,            /* populated and `recursive`: remove post-order */
    STORE_DELETE_NEEDS_RECURSIVE, /* populated without `recursive`: refuse */
} store_delete_action_t;

store_delete_action_t store_delete_action(bool is_dir, size_t entries, bool recursive);

/* Fold one `readdir` name into a child count. `.`, `..` and any name a listing
 * would refuse are not children (§4.17). Pure. */
size_t store_count_entry(size_t count, const char *name);

typedef enum {
    STORE_MOVE_OK = 0,
    STORE_MOVE_SOURCE_MISSING,
    STORE_MOVE_TARGET_TAKEN, /* §4.22: an existing `to` is never overwritten */
    STORE_MOVE_INTO_ITSELF,  /* `to` is `from` or below it */
} store_move_verdict_t;

/* Decide a move before the syscall, so the refusal order is fixed in one place.
 * `from`/`to` are canonical VFS paths. Pure. */
store_move_verdict_t store_move_verdict(bool source_exists, bool target_exists,
                                        const char *from, const char *to);

/* Directories first, then name, both ascending (§4.17). Pure. */
int  store_entry_cmp(const store_entry_t *a, const store_entry_t *b);

/* True when `name` is one entry name: non-empty, no `/`, not `.`/`..`. Pure. */
bool store_entry_name_ok(const char *name);

/* Decode one wire chunk. `encoding` NULL or "utf8" copies; "base64" decodes.
 * ESP_ERR_INVALID_ARG for an unknown encoding, malformed base64, or a chunk that
 * does not fit `cap` or `STORE_CHUNK_BYTES` (§4.16, §4.20). Pure. */
esp_err_t store_decode_chunk(const char *data, const char *encoding,
                             uint8_t *out, size_t cap, size_t *len);

/* True when a `cap`-byte read of a `total`-byte file left bytes behind. `cap` is a
 * ceiling on what one `sandbox.read` may carry (§4.18), so a file of exactly `cap`
 * bytes is complete, not truncated. Pure. */
bool store_read_truncated(size_t read_n, size_t cap, size_t total);

/* Length of the longest valid UTF-8 prefix of `buf`, or 0 when the bytes are not
 * UTF-8 at all (a lone continuation, an overlong form, a surrogate, a code point
 * above U+10FFFF). A prefix shorter than `n` that stops only because a multi-byte
 * sequence is incomplete is a truncation, not an error: the caller cut the text.
 * Pure. */
size_t store_utf8_prefix(const uint8_t *buf, size_t n);


/* Enumerate one namespace's entry names, oldest first. NVS has no iterator that
 * survives a close, so this is a cursor the caller advances: start with `index` 0,
 * then pass back the same `*index` until it returns ESP_ERR_NOT_FOUND. `out` receives
 * one key per call. The same shape `memory.list` needs and no more.
 *
 * The iteration is over a snapshot taken at the first call, because `nvs_entry_find`
 * invalidates on any write and a `memory.list` that interleaved with a write would
 * otherwise skip entries. */
esp_err_t store_nvs_key_at(const char *namespace, size_t index, char *out,
                           size_t cap);

/* Delete one key. ESP_ERR_NVS_NOT_FOUND when it was not there. */
esp_err_t store_nvs_erase(const char *namespace, const char *key);
