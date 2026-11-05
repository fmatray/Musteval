/* link — NDJSON framing over USB-Serial/JTAG CDC (`04-protocol.md` §1).
 * Layering: L2 (`08-code-architecture.md` §1). No module writes to CDC directly
 * and no module builds an envelope; both belong here (`08` §3). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "store.h"

/* Frozen by `04-protocol.md` §1. A line is complete only at `\n`, includes the
 * terminator in its limit, and overflow is discarded up to that newline so one
 * bad line cannot desynchronise the stream (§1.1 rules 1-3). */
#define LINK_RX_MAX 4096
#define LINK_TX_MAX 16384

/* Why a line was refused. The codec reports, the caller decides the wire shape:
 * `04-protocol.md` §1.2 makes the answer depend on whether an `id` is recoverable. */
typedef enum {
    LINK_LINE_OK = 0,
    LINK_LINE_OVERSIZE,     /* > LINK_RX_MAX bytes before the newline */
    LINK_LINE_BAD_JSON,     /* not one well-formed JSON object */
    LINK_LINE_BAD_ENVELOPE, /* parses, but fails `04` §2 */
} link_line_status_t;

/* Byte-at-a-time assembler. Feeding is O(1) per byte and allocation-free until a
 * line completes, which is what keeps the high-priority link task cheap. */
typedef struct {
    char buf[LINK_RX_MAX];
    size_t len;
    bool discarding; /* oversize line: drop bytes until the next newline */
} link_reader_t;

void link_reader_reset(link_reader_t *reader);

/* Feed one byte. Returns true when it completed a line, which is then in `buf`
 * with `line_len` bytes and `*status` set (the trailing `\r` of a CRLF stripped,
 * §1.1 rule 2). Returns false while the line is still open.
 *
 * A completed line that is over the cap is not delivered: `*status` says why and
 * the caller emits the protocol error. The reader never returns a partial line. */
bool link_reader_feed(link_reader_t *reader, char byte, size_t *line_len,
                      link_line_status_t *status);

const char *link_reader_line(const link_reader_t *reader);

/* Envelope (`04` §2): v=1, type, id, ts, optional reply_to. Validates the id
 * charset `[A-Za-z0-9._-]{1,32}` and rejects a `v` other than 1. */
esp_err_t link_envelope_wrap(json_doc_t *body, const char *type, const char *id,
                             int64_t ts, const char *reply_to, char *out,
                             size_t out_cap, size_t *written);

/* The inbound direction of the same rules. Parses one assembled line and, on
 * success, hands back an owned document the caller must `json_free`. `*bad_field`
 * names the member that failed so the error frame can say why (§4 `E_PROTOCOL`);
 * it is set to `NULL` on success.
 *
 * Splitting this from `link_reader_feed` keeps the byte loop free of JSON: the
 * reader answers "is there a line", this answers "is it a legal frame". */
esp_err_t link_frame_parse(const char *line, size_t len, json_doc_t **out,
                           const char **bad_field);

/* True when `type` belongs to the bridge -> device column of `04` §2.1. A valid
 * frame in the wrong direction is `E_PROTOCOL` at dispatch, not at parse. */
bool link_type_inbound(const char *type);

/* Allocate the next device id, `d-<counter>`, per boot and never reused (§2). */
const char *link_next_id(void);

/* Log bridging (`00-overview.md` D9, `04` §3.10). `esp_log_set_vprintf` is the
 * board half; these two are the half that decides what a log line becomes, so they
 * are provable on the host.
 *
 * `link_log_filter` maps an ESP-IDF level letter to the protocol's name and drops
 * anything the configured level hides. `device.log_level` uses
 * `none`/`error`/`warn`/`info`/`debug` (`03-config.md` §device); an unknown value
 * falls back to `info` rather than muting the device. */
const char *link_log_filter(char level_letter, const char *configured);

/* Build one `log` frame (§3.10) into `out`. `tag` is the module name and `msg` the
 * rendered line, already redacted by `cfg` (`08` §3: no module formats its own log
 * line around secrets). Returns ESP_ERR_INVALID_SIZE when the frame would exceed
 * `out_cap`, never a truncated frame. */
esp_err_t link_log_frame(const char *tag, const char *msg, const char *level,
                         int64_t ts, int64_t t_ms, char *out, size_t out_cap,
                         size_t *written);

/* One ESP-IDF V1 log line, split back into its parts. V1 bakes the prefix into the
 * format string (`log/include/esp_log_format.h`: `LOG_FORMAT`), so a composed line
 * is `I (1234) tag: message`, with colour codes only when `CONFIG_LOG_COLORS` is on.
 * Returns false on a line that does not have that shape, which is the honest answer
 * for a caller that only has `printf` output to work from. */
typedef struct {
    char level_letter; /* 'E', 'W', 'I', 'D', 'V' */
    int64_t t_ms;      /* the `(…)` figure: ms since boot under the RTOS source */
    const char *tag;   /* borrowed into `line`, NUL-terminated at the colon */
    size_t tag_len;
    const char *msg;   /* borrowed into `line`, newline removed */
    size_t msg_len;
} link_log_line_t;

bool link_log_parse(const char *line, size_t len, link_log_line_t *out);

/* --- Transmit port -------------------------------------------------------- */

/* `link` never touches the CDC driver itself (`08` §2: the read/write task owns the
 * port). Everything that emits registers a writer; `link_emit` appends the CRLF
 * framing for it. With no writer registered the emit is counted and dropped, which
 * is what happens between boot and the link task starting. */
typedef esp_err_t (*link_writer_t)(const char *buf, size_t len);
void link_set_writer(link_writer_t writer);

/* Writes one frame plus CRLF, in place. The caller's buffer must have room for the
 * two terminator bytes; `cap` is its size and `len` the frame's. A frame that would
 * exceed `LINK_TX_MAX` is refused rather than split (`04` §1.2). Returns the
 * writer's error, or `ESP_ERR_INVALID_STATE` when no writer is registered. The
 * single writer call is deliberate: two calls let another task interleave a frame
 * and desynchronise the stream. */
esp_err_t link_emit(char *frame, size_t cap, size_t len);

/* Build the envelope around `body` (`04` §2) and write one frame. This is the only
 * way a module above `link` sends one: `08` §3 forbids anyone else from building an
 * envelope, so callers hand over a body built with the `store` helpers and nothing
 * else. A NULL `body` emits a frame with no members beyond the envelope. */
esp_err_t link_emit_body(const char *type, const char *id, int64_t ts,
                         const char *reply_to, json_doc_t *body);

/* --- Body builders (`04` §3.2, §3.9) -------------------------------------- */

/* Facts `app` fills for `hello_ack`; `link` serialises them and owns nothing. */
typedef struct {
    const char *device;      /* device.name, default "musteval" */
    const char *fw_version;  /* "1.0.0" */
    const char *boot_id;     /* hex, stable until reset */
    bool time_synced;
} link_hello_t;

/* `hello_ack` BODY only (§3.2): `device`, `fw_version`, `protocol:1`, `boot_id`,
 * `time_synced`, `limits` and `capabilities`. `tools` is empty because no tool
 * exists at M1, which is what the field means (§3.2: the set callable *now*, not a
 * compile-time constant). Fills members of `obj`, which the caller owns: the
 * envelope builder needs the body in place, and no store API adopts a subtree. */
esp_err_t link_build_hello_ack(json_node_t *obj, const link_hello_t *hello);

/* The board facts `status` reports; everything else in §3.9 is either a constant
 * or a subsystem that does not exist yet. */
typedef struct {
    bool     safe_mode;
    int32_t  boot_failures;
    int64_t  uptime_s;
    const char *boot_id;
    int64_t  userfs_used, userfs_total;
    int64_t  wsfs_used, wsfs_total;
    int64_t  heap_free, psram_free;
} link_status_t;

/* `status` BODY only (§3.9), the single canonical status object. Subsystems absent
 * at M1 report the honest idle value rather than a fabricated one: `wifi` off,
 * `time` unsynced, `llm` unavailable, `mcp`/`mqtt` empty, `geo` null, `agent` idle.
 * Fills members of `obj`, which the caller owns. */
esp_err_t link_build_status(json_node_t *obj, const link_status_t *st);

/* --- Reader task (`02-firmware.md` §3.3) ---------------------------------- */

/* Install the USB-Serial/JTAG driver, register the CDC writer and start
 * `link_task`. Board-only: the host suite links the codec, not the port. Idempotent,
 * and a driver that will not install is reported rather than aborting boot, so
 * `app` decides what a device without a link should do. */
esp_err_t link_start(void);

/* Install the `esp_log_set_vprintf` hook that turns every ESP_LOGx into a `log`
 * frame (`00-overview.md` D9). `configured` is `device.log_level`. Returns the
 * previous handler so a caller could restore it. Not called by the host suite, which
 * needs its own stdout.
 *
 * The hook runs on whichever task logged, and `main` is only
 * `CONFIG_ESP_MAIN_TASK_STACK_SIZE` (3584 bytes) by default: its line, message and
 * frame buffers are static, not stack locals, because three 1 KiB locals overflowed
 * `main` on the board and panicked the boot. */
void *link_log_install(const char *configured);

/* --- Refusing a line (`04-protocol.md` §1.2) ------------------------------ */

/* Best-effort `id` from a line that did not parse: finds `"id"` followed by `:` and
 * a quoted 1..32-character `[A-Za-z0-9._-]` value. Writes into `out` and returns it,
 * or returns NULL. §1.2 makes the answer depend on exactly this being recoverable. */
const char *link_recover_id(const char *line, size_t len, char *out,
                            size_t out_cap);

/* Emit the §1.2 answer for a refused line, which is one of two frames and no third:
 * a `command.result` with `ok:false` and `E_PROTOCOL` when an `id` is recoverable,
 * otherwise a `protocol.error` event carrying `dropped_bytes`.
 *
 * `name` is the command this line claimed to be, echoed into the result. Pass NULL
 * when unknown and the frame reports `"name":""`: `04` §3.20 requires a string, and
 * an unparsable line has no name to echo, so the empty string is the honest value.
 * `message` is the human-readable reason and must not carry secret material. When a
 * line is dropped for length, `len` is the bytes actually seen, which is what
 * `dropped_bytes` reports (`04` §1.2). */
esp_err_t link_refuse_line(const char *line, size_t len, const char *name,
                           const char *message, int64_t ts);

/* --- Framing ------------------------------------------------------------- */

/* Answer `ping` with `pong` (§3.7). No other module is involved, and it must work
 * while a run is in flight, so the caller emits it without taking a lock. */
esp_err_t link_emit_pong(const char *reply_to, int64_t ts);

/* Route for every inbound frame. `link` does not know which module consumes it. */
typedef struct {
    void (*on_frame)(const json_node_t *frame);
} link_sink_t;

esp_err_t link_set_sink(const link_sink_t *sink);

/* The registered sink, or NULL before `link_set_sink`. `link_task` needs it to
 * route an inbound frame, and keeping the read private to `link.c` means a caller
 * cannot replace the sink by assigning to it. */
const link_sink_t *link_sink_get(void);
