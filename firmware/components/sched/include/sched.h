/* sched — the trigger engine and the `/tasks.json` document.
 * Layering: L2 (`08-code-architecture.md` §1). Imports `store` and `clock`, and
 * reaches `sys` only to configure a pin whose ISR it installs itself, which keeps
 * that edge L2 -> L0 (`02-firmware.md` §5.1). It never calls `agent`: the submit
 * path is a port installed by `main`, so every rule below is provable without a
 * model.
 * Contracts: `02-firmware.md` §5, `03-config.md` §tasks, `04-protocol.md` §3.16,
 * §3.19, §4.4.
 *
 * Split like `store`/`agent`: the rules in `sched_parse.c` are pure and host-tested;
 * `sched.c` owns the file, NVS, the timer, the ISR and the port call, and is built
 * only for the board.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "store.h"

/* `03-config.md` §Storage limits: `/tasks.json` is 8 KiB, 16 entries. Both are
 * checked before parsing so an oversized file cannot exhaust RAM. */
#define SCHED_JSON_MAX  8192
#define SCHED_MAX_TASKS 16

/* `03-config.md` §Task schema: `[a-z0-9_-]{1,15}`, and the id doubles as the NVS
 * runtime key, which is why the ceiling exists (`§Open questions 1`). */
#define SCHED_ID_MAX 15

/* `repeat.count`, `trigger.interval_s`, `payload_contains`, `cooldown_s`. */
#define SCHED_REPEAT_COUNT_MIN 1
#define SCHED_REPEAT_COUNT_MAX 1000
#define SCHED_INTERVAL_MIN_S   60
#define SCHED_INTERVAL_MAX_S   604800
#define SCHED_PAYLOAD_MAX      64
#define SCHED_COOLDOWN_MAX_S   3600
#define SCHED_COOLDOWN_DEF_S   5

/* `trigger.name` and `event.emit`'s `type` share one charset and ceiling
 * (`02-firmware.md` §5.5, `tools/events.md`). */
#define SCHED_EVENT_NAME_MAX 32

/* `action.vars`: one `{{name}}` placeholder per entry. The declared *names* come
 * from the template's own frontmatter (`03-config.md` §Template schema); this is the
 * value map the task supplies. */
#define SCHED_MAX_VARS 8
#define SCHED_VAR_NAME_MAX  32
#define SCHED_VAR_VALUE_MAX 64

/* `action.prompt` is 1..`agent.max_input_bytes`, and that key is itself bounded by
 * `serial.max_line_bytes` (`03-config.md` §Storage limits), so 4096 is the largest
 * legal prompt and nothing this refuses could have been accepted. */
#define SCHED_PROMPT_MAX 4096

typedef struct {
    char name[SCHED_VAR_NAME_MAX + 1];
    char value[SCHED_VAR_VALUE_MAX + 1];
} sched_var_t;

/* The nine triggers of `00-overview.md` §5, in the frozen order. */
typedef enum {
    SCHED_TRIGGER_BOOT = 0,
    SCHED_TRIGGER_WIFI_CONNECTED,
    SCHED_TRIGGER_NTP_SYNCED,
    SCHED_TRIGGER_SERIAL_CONNECTED,
    SCHED_TRIGGER_GPIO,
    SCHED_TRIGGER_MQTT_MESSAGE,
    SCHED_TRIGGER_EVENT,
    SCHED_TRIGGER_SCHEDULE,
    SCHED_TRIGGER_MANUAL,
    SCHED_TRIGGER_COUNT,
} sched_trigger_t;

typedef enum {
    SCHED_REPEAT_ONCE = 0,
    SCHED_REPEAT_COUNT_MODE,
    SCHED_REPEAT_FOREVER,
} sched_repeat_t;

typedef enum {
    SCHED_MISSED_SKIP = 0,
    SCHED_MISSED_RUN_ONCE,
} sched_missed_t;

typedef enum {
    SCHED_ACTION_PROMPT = 0,
    SCHED_ACTION_TEMPLATE,
} sched_action_t;

typedef enum {
    SCHED_AT = 0,
    SCHED_EVERY,
} sched_schedule_kind_t;

/* One task, with its fixed-size text inline so the whole document is a single heap
 * block and a reload never fragments the ring. Every ceiling comes from the frozen
 * schema; a value that would exceed one is refused, never truncated. */
typedef struct {
    char id[SCHED_ID_MAX + 1];
    char description[128];
    bool enabled;

    sched_trigger_t type;

    /* gpio */
    int pin;
    int level;

    /* mqtt_message: exact topic, or a trailing `*` for exactly one final level. */
    char    server_id[33];
    char    topic[129];
    char    payload_contains[SCHED_PAYLOAD_MAX + 1]; /* "" when absent */
    int32_t cooldown_s;

    /* event */
    char name[SCHED_EVENT_NAME_MAX + 1];

    /* schedule */
    sched_schedule_kind_t kind;
    char    at[33];          /* "HH:MM:SS" or ISO-8601; "" for `every` */
    int32_t interval_s;      /* 0 for `at` */
    int64_t first_at;        /* epoch seconds; 0 with `has_first_at` false = boot */
    bool    has_first_at;

    sched_action_t action;
    char prompt[SCHED_PROMPT_MAX];      /* `prompt` action text; "" for a template */
    char template_name[33];
    sched_var_t vars[SCHED_MAX_VARS];
    size_t      vars_count;

    sched_repeat_t repeat_mode;
    int32_t        repeat_count;        /* meaningful only for COUNT */
    sched_missed_t missed;

    /* Runtime: NVS only, never written back into the file (`03-config.md` §tasks). */
    int32_t fired_count;
    int64_t last_fired;
} sched_task_t;

typedef struct {
    sched_task_t tasks[SCHED_MAX_TASKS];
    size_t       count;
} sched_tasks_t;

/* --- Pure: document and schema -------------------------------------------- */

/* The name a trigger is written as, which is also what `task.fired` reports. Frozen
 * spelling, not a diagnostic. NULL past the end. */
const char *sched_trigger_name(sched_trigger_t type);

/* `type` from its frozen spelling, or -1 when it is not one of the nine. */
int sched_trigger_from_name(const char *name);

/* Validate one task object against the closed schema, filling `out`. This is the
 * only path that admits a task, used by both `tasks.set` and the file parse, so
 * there is one opinion of what a task is. Returns ESP_OK, or ESP_ERR_INVALID_ARG
 * with `why` naming the offending key (`03-config.md` §Validation step 4). */
esp_err_t sched_task_validate(const json_node_t *obj, sched_task_t *out,
                              char *why, size_t why_cap);

/* Parse one `/tasks.json` document. A file that fails steps 1-8 of
 * `03-config.md` §Validation schedules nothing (`ESP_ERR_INVALID_ARG`); a single task
 * whose object is invalid is dropped and the rest load, which is the documented
 * per-item degradation. `dropped`, when non-NULL, receives how many were. */
esp_err_t sched_parse(const char *json, size_t len, sched_tasks_t *out,
                      char *why, size_t why_cap, size_t *dropped);

/* Add a task's `trigger` object under `parent` in the frozen shape `03-config.md`
 * §Trigger schema defines, which is also what `task.fired` reports (`04` §3.16).
 * Public because `main` builds that frame and owns the link; the shape has one
 * owner here so the file, the list and the frame cannot drift. */
void sched_trigger_json(json_node_t *parent, const sched_task_t *task);

/* Emit `tasks.list`'s `result` (`04-protocol.md` §4.4): every task with its trigger,
 * repeat, `fired_count` and, when one exists, `next_fire_at`. */
esp_err_t sched_list_json(const sched_tasks_t *tasks, int64_t now, int64_t boot_at,
                          int32_t tz_offset_s, json_doc_t **out);

/* Insert or replace by id. `*created` is false when an existing entry was updated,
 * which is what `tasks.set` reports. Returns ESP_ERR_INVALID_SIZE past the cap. */
esp_err_t sched_tasks_upsert(sched_tasks_t *tasks, const sched_task_t *task,
                             bool *created);

/* Remove by id. `*deleted` is false when the id was not there, which `tasks.delete`
 * reports as `E_CONFIG` (`04-protocol.md` §4.4). */
esp_err_t sched_tasks_remove(sched_tasks_t *tasks, const char *id, bool *deleted);

/* Emit `/tasks.json` with `tasks` as its content, for the whole-file rewrite. */
esp_err_t sched_emit_json(const sched_tasks_t *tasks, json_doc_t **out);

/* --- Pure: matching ------------------------------------------------------- */

/* `02-firmware.md` §5.4: `pattern` matches `topic` exactly, or, when it ends in `*`,
 * matches one final level of arbitrary length: the pattern `home/sensor/` plus a
 * star matches `home/sensor/a` but not `home/sensor/a/b`. `+` and `#` are not
 * special here. */
bool sched_topic_matches(const char *pattern, const char *topic);

/* `[a-z0-9._-]{1,32}`. Shared by `trigger.name` and `event.emit`. */
bool sched_event_name_ok(const char *name);

/* A hit: the task's index in `tasks`, so the caller can fire it without a lookup. */
typedef struct {
    size_t index;
    int    level;      /* gpio only */
} sched_hit_t;

/* Enabled tasks whose trigger is `type`, for the six bare transitions (boot,
 * wifi_connected, ntp_synced, serial_connected, manual, and `schedule` is excluded
 * because it is due-time driven). At most `cap` hits are written. */
size_t sched_match_bare(const sched_tasks_t *tasks, sched_trigger_t type,
                        sched_hit_t *hits, size_t cap);

/* `event` triggers: exact and case-sensitive on `name` (`02-firmware.md` §5.5). */
size_t sched_match_event(const sched_tasks_t *tasks, const char *name,
                         sched_hit_t *hits, size_t cap);

/* `gpio` triggers: pin and level both agree. */
size_t sched_match_gpio(const sched_tasks_t *tasks, int pin, int level,
                        sched_hit_t *hits, size_t cap);

/* `mqtt_message` triggers: server, topic filter and optional payload substring,
 * minus those still inside their cooldown. Exact-topic tasks are emitted before
 * wildcard ones, which is the order §5.4 states. */
size_t sched_match_mqtt(const sched_tasks_t *tasks, const char *server_id,
                        const char *topic, const char *payload, int64_t now,
                        sched_hit_t *hits, size_t cap);

/* --- Pure: schedule arithmetic -------------------------------------------- */

/* May this task fire again? `once` after one fire, `count` below its max, `forever`
 * always. */
bool sched_repeat_allows(const sched_task_t *task);

/* Next due instant in unix seconds, or 0 when none can be computed: an `at` task
 * before the first clock sync, or an `every` task with no clock and no `first_at`.
 * `boot_at` is the unix time of boot, which an `every` task without `first_at`
 * counts from (`02-firmware.md` §5.2). `tz_offset_s` is what
 * `clock_utc_offset_s()` answers; a daily `at` is a local wall-clock time so the
 * offset decides which instants it names. */
int64_t sched_next_fire(const sched_task_t *task, int64_t now, int64_t boot_at,
                        int32_t tz_offset_s);

/* The most recent instant this task was due, in unix seconds, or 0 when it has never
 * been due. This is the firing test, not `sched_next_fire`: a tick asks "has a due
 * time passed since the last fire", and the *next* due time is always in the future,
 * so comparing against it would never fire. A daily `at` answers the occurrence
 * earlier today (or yesterday); an `every` answers its last interval boundary. */
int64_t sched_last_due(const sched_task_t *task, int64_t now, int64_t boot_at,
                       int32_t tz_offset_s);

/* Whether a fire that came due at `due_at` still runs now. `skip` drops it once it
 * is more than `grace_s` late; `run_once` runs it whenever it is still due. */
bool sched_missed_should_fire(const sched_task_t *task, int64_t due_at, int64_t now,
                              int64_t grace_s);

/* Render `03-config.md` §Template schema's three rules: the frontmatter block is
 * dropped, a `{{x}}` whose `x` the frontmatter declares is replaced by the task's
 * value (empty when the task does not supply one), and every other `{{x}}` is left
 * exactly as written. Substitution is literal: no expressions, no loops. `out` is
 * NUL-terminated and `out_len` excludes it; a result that does not fit is
 * ESP_ERR_INVALID_SIZE rather than a silently cut prompt. */
esp_err_t sched_render_template(const char *text, size_t len, const sched_task_t *task,
                                char *out, size_t cap, size_t *out_len);

/* `"HH:MM:SS"` to seconds since local midnight, or -1 when it is not that shape.
 * Pure: it knows nothing of a timezone, which is what makes it testable. */
int32_t sched_parse_hhmmss(const char *text);

/* True when `text` is a `YYYY-MM-DDTHH:MM:SS[Z]` instant, which is the other shape
 * `trigger.at` accepts. Separate from its numeric value, since `1970-01-01T00:00:00Z`
 * is a valid instant whose unix second is zero. */
bool sched_is_iso(const char *text);

/* A task's runtime state as one NVS string under key `<id>`: `"<fired_count>:<last>"`
 * (`03-config.md` §tasks names one key per task, and the 15-byte NVS key limit is why
 * `tasks[].id` is capped at 15 in the first place). Packing is what fits both fields
 * in that key. Pure so the round trip is a host test. */
esp_err_t sched_state_encode(int32_t fired_count, int64_t last_fired,
                             char *out, size_t cap);
bool sched_state_decode(const char *text, int32_t *fired_count, int64_t *last_fired);

/* --- Board half (`sched.c`) ----------------------------------------------- */

/* Where a fired task's prompt goes. Installed by `main`, the only module allowed to
 * start a run (`08-code-architecture.md` §3). `source` is the `task.fired` origin. */
typedef esp_err_t (*sched_submit_fn)(const char *prompt, size_t len, const char *source,
                                     char *run_id_out, size_t run_id_cap);
void sched_set_submit(sched_submit_fn fn);

/* Allocate the state, the lock, the GPIO queue and the drain task. It reads nothing
 * and arms nothing, so safe mode (`02-firmware.md` §3.2: `/tasks.json` is not loaded
 * or fired) still answers `tasks.list` and `tasks.delete` — the two commands the
 * recovery path needs. */
esp_err_t sched_init(void);

/* Load `/tasks.json` and start the tick and the `boot` trigger is the caller's. A
 * missing or rejected file leaves zero tasks, which the degradation policy calls a
 * working agent with nothing scheduled. Called once at boot step 9, and never in
 * safe mode. */
esp_err_t sched_start(void);

/* Re-read `/tasks.json` and re-arm the GPIO pins. A file this build cannot read
 * leaves the armed schedule in place, so a bad edit does not unschedule the device. */
esp_err_t sched_reload(void);

/* The read-only view the commands and `tasks.list` render from. NULL before
 * `sched_init`. */
const sched_tasks_t *sched_state(void);

/* Forget one task's runtime state, so a later `tasks.set` of the same id starts from
 * `fired_count: 0` (`04-protocol.md` §4.4 says a delete does not leave a counter
 * behind for the next task that claims the id). */
void sched_clear_runtime(const char *id);

/* Fire one task by index. Shared by every trigger so the path is identical whichever
 * matched, and serialized through `agent`'s queue rather than this call. Returns true
 * when a run was accepted. */
bool sched_fire(size_t index, int64_t now);

/* `/tasks.json` as it is stored, for `task.list` (`tools/tasks.md` §`task.list`: the
 * tool returns the stored task objects, not `tasks.list`'s enriched view). Takes the
 * lock, so a concurrent `tasks.set` cannot be observed half-written. */
esp_err_t sched_stored_json(json_doc_t **out);

/* One `tasks.*` command (`04-protocol.md` §4.4). `name` is the command name and
 * `args` its argument object. On success `*out` is the `result` object the caller
 * emits and frees; on failure it is NULL and `why` names the offending key, which
 * `main` puts in the `E_CONFIG` detail. Every write goes through the whole-file
 * rewrite, so a refused `tasks.set` leaves the previous document armed. */
esp_err_t sched_command(const char *name, const json_node_t *args, json_doc_t **out,
                        char *why, size_t why_cap);

/* A transition the firmware observed, forwarded to the matcher and fired. Each
 * returns how many tasks fired, which `event.emit` reports as `task_hits`. */
size_t sched_trigger_bare(sched_trigger_t type);

/* Fire every `event` task named `name`, then emit the `agent.event` frame with the
 * hit count — even at 0, which `tools/events.md` §Delivery requires. `data` is the
 * emission's optional string, or NULL. Returns the hits, which the tool reports. */
size_t sched_trigger_event(const char *name, const char *data);
size_t sched_trigger_mqtt(const char *server_id, const char *topic, const char *payload);
void   sched_trigger_gpio_isr(int pin);   /* ISR-safe: queues only */

/* Read an action into `out`: literal for `prompt`, `/prompts/<name>.md` for a
 * template. Returns ESP_ERR_NOT_FOUND for a template file that is not there, which
 * `task.fired` reports as a skipped run. */
esp_err_t sched_render_action(const sched_task_t *task, char *out, size_t cap,
                              size_t *out_len);
