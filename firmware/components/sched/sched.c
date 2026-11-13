/* sched.c — the board half of the scheduler: the file on flash, the runtime state
 * in NVS, the `esp_timer` that makes a schedule task due, the GPIO ISR, and the one
 * port `main` installs (`sched_submit_fn`).
 *
 * Every decision here is a call to `sched_parse.c`, which is where the schema, the
 * matchers and the arithmetic live and where they are tested. This file only moves
 * bytes and time.
 */

#include "sched.h"

#include <stdio.h>
#include <string.h>

#include "clock.h"
#include "link.h"
#include "driver/gpio.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "store.h"
#include "sys.h"

static const char *TAG = "sched";

/* `03-config.md` §Storage: `/tasks.json` lives where the image ships it, next to
 * `/config.json`, and is read with `store_read_file` because it is not in the
 * workspace sandbox. */
#define SCHED_PATH "/tasks.json"

/* `03-config.md` §tasks: one NVS key per task, named by its id, holding
 * `"<fired_count>:<last>"` (`sched_state_encode`). The namespace is the one `store`
 * created at boot for runtime counters. */
#define SCHED_NS "runtime"

/* The GPIO ISR queues an edge; the drain task matches and fires it. The depth is the
 * number of edges a human can produce before the drain runs, not a stream rate. */
#define SCHED_GPIO_QUEUE_LEN 8

/* The `esp_timer` period. It bounds how late a `schedule` task can fire; a minute is
 * the resolution `03-config.md` §Schedule offers (a task is not a real-time
 * deadline). */
#define SCHED_TICK_US (60LL * 1000 * 1000)

/* `02-firmware.md` §5.2: a fire more than this late counts as missed, which is when
 * `missed` decides whether it still runs. One tick plus a margin, so a normal tick
 * is never mistaken for a missed fire. */
#define SCHED_MISSED_GRACE_S 90

/* The largest thing this module ever reads or writes: `/tasks.json` is capped at
 * 8 KiB (`03-config.md` §Storage limits) and a template at 4 KiB. They live in PSRAM
 * beside the state, not on a task stack: `CONFIG_ESP_TIMER_TASK_STACK_SIZE` is 3584
 * bytes, so an 8 KiB local would overflow the timer task on the first tick. */
#define SCHED_TEXT_MAX SCHED_JSON_MAX

typedef struct {
    int pin;
} sched_gpio_edge_t;

/* PSRAM first, internal as the fallback: `08-code-architecture.md` §5 puts the bulk
 * buffers there, and `agent` allocates its history the same way. The state is 87 KiB
 * of `sched_task_t`, which does not fit the ~52 KiB of free DIRAM. */
static void *sched_alloc(size_t bytes)
{
    void *p = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (p == NULL) p = heap_caps_malloc(bytes, MALLOC_CAP_8BIT);
    return p;
}

static sched_tasks_t *s_state;
static sched_tasks_t *s_scratch; /* a parse target, so a bad file cannot wipe the armed set */
static char *s_text;          /* scratch for one rendered template */
static char *s_doc;           /* the emitted `/tasks.json` image */
static char *s_prompt;        /* the action text handed to the submit port */
static SemaphoreHandle_t s_lock;
static QueueHandle_t s_gpio_queue;
static esp_timer_handle_t s_timer;
static TaskHandle_t s_gpio_task;
static bool s_started;

static sched_submit_fn s_submit;

/* `04` §3.16: one frame per fire, `run_id` null when the run was refused. `sched`
 * emits it itself because `08` §1.1 makes `sched` → `link` the permitted edge and
 * names `task.fired` as one of the two frames it exists for. */
static void emit_fired(const sched_task_t *task, const char *run_id)
{
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_string(root, "task_id", task->id);
    json_add_string(root, "description", task->description);
    sched_trigger_json(root, task);
    json_add_int(root, "fire_count", task->fired_count);
    if (run_id != NULL) {
        json_add_string(root, "run_id", run_id);
    } else {
        json_add_null(root, "run_id");
    }
    char id[16];
    link_next_id(id, sizeof id);
    link_emit_body("task.fired", id, clock_now(), NULL, body);
    json_free(body);
}

/* The one place a fire becomes a run and a `task.fired`. `run_id_out` is NULL for a
 * trigger, where nobody is waiting for the id. */
static bool deliver(sched_task_t *task, int64_t now, char *run_id_out, size_t run_id_cap)
{
    char run_id[24] = "";
    size_t len = 0;

    esp_err_t err = sched_render_action(task, s_prompt, SCHED_PROMPT_MAX + 1, &len);
    if (err != ESP_OK) {
        /* A template file that is gone is a task that cannot run. The fire still
         * counts, so a `count` task cannot spin forever retrying a missing file. */
        ESP_LOGW(TAG, "task %s: action unavailable (%s)", task->id, esp_err_to_name(err));
    } else if (s_submit != NULL) {
        const char *source = task->action == SCHED_ACTION_TEMPLATE ? "template" : "task";
        /* A refusal is `agent`'s to log. The trigger matched either way, so the
         * counter moves and `task.fired` reports the attempt with no `run_id`. */
        (void)s_submit(s_prompt, len, source, run_id, sizeof run_id);
    }

    task->fired_count++;
    task->last_fired = now;
    char value[40];
    if (sched_state_encode(task->fired_count, task->last_fired, value, sizeof value) ==
        ESP_OK) {
        store_nvs_set_str(SCHED_NS, task->id, value, NULL);
    }
    emit_fired(task, run_id[0] != '\0' ? run_id : NULL);
    if (run_id_out != NULL && run_id_cap > 0) snprintf(run_id_out, run_id_cap, "%s", run_id);
    return true;
}

/* Lock-free: every caller either holds `s_lock` or is the tick. Splitting it this way
 * is what keeps `tasks.run` — which holds the lock across its whole reply — from
 * taking a non-recursive mutex twice. */
static bool fire_locked(size_t index, int64_t now, char *run_id_out, size_t run_id_cap)
{
    if (s_state == NULL || index >= s_state->count) return false;
    sched_task_t *task = &s_state->tasks[index];
    if (!task->enabled || !sched_repeat_allows(task)) return false;
    return deliver(task, now, run_id_out, run_id_cap);
}

bool sched_fire(size_t index, int64_t now)
{
    if (s_lock == NULL) return false;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) != pdTRUE) return false;
    bool fired = fire_locked(index, now, NULL, 0);
    xSemaphoreGive(s_lock);
    return fired;
}

static size_t fire_hits(const sched_hit_t *hits, size_t found)
{
    int64_t now = clock_now();
    size_t fired = 0;
    for (size_t i = 0; i < found; i++) {
        if (sched_fire(hits[i].index, now)) fired++;
    }
    return fired;
}

size_t sched_trigger_bare(sched_trigger_t type)
{
    if (s_state == NULL) return 0;
    sched_hit_t hits[SCHED_MAX_TASKS];
    size_t found = sched_match_bare(s_state, type, hits, SCHED_MAX_TASKS);
    return fire_hits(hits, found);
}

/* The handler `gpio_isr_handler_add` registers. It does one thing and returns, which
 * is what keeps the ISR shorter than the time to the next edge. The pin arrives as
 * the handler argument, so no global is read here. */
static void gpio_isr(void *arg)
{
    sched_trigger_gpio_isr((int)(intptr_t)arg);
}

/* `02-firmware.md` §5.1: `sched` owns the handler. A pin that a task watches is armed
 * here, once per load, because the edge it needs is the one that reaches the task's
 * `level` — a rising edge for `level: 1`, a falling edge for `level: 0`. The handler
 * is this module's ISR, added per pin (`gpio_isr_handler_add` in `sched.c`'s GPIO
 * section); `sys` only configured the pin. */
/* Which pins this module armed, so a `reload` that drops a task can disarm its pin.
 * Without it a deleted GPIO task keeps raising `gpio.edge` forever, on a schedule that
 * no longer contains a task watching that pin. */
static uint32_t s_armed_pins;

static void arm_gpio(const sched_tasks_t *tasks)
{
    uint32_t wanted = 0;
    for (size_t i = 0; i < tasks->count; i++) {
        const sched_task_t *task = &tasks->tasks[i];
        if (!task->enabled || task->type != SCHED_TRIGGER_GPIO) continue;
        if (!sys_pin_readable(task->pin)) continue;
        wanted |= 1u << task->pin;
        int edge = task->level != 0 ? SYS_EDGE_RISING : SYS_EDGE_FALLING;
        if (sys_gpio_edge_config(task->pin, edge) != ESP_OK) {
            ESP_LOGW(TAG, "task %s: GPIO%d cannot report an edge", task->id, task->pin);
            continue;
        }
        /* `02-firmware.md` §5.1: the handler is this module's, and it queues. `ESP_OK`
         * and `ESP_ERR_INVALID_STATE` both mean the pin is armed: the second is the
         * reload case, where the handler this build added is already registered. */
        esp_err_t err = gpio_isr_handler_add(task->pin, gpio_isr,
                                             (void *)(intptr_t)task->pin);
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "task %s: GPIO%d handler: %s", task->id, task->pin,
                     esp_err_to_name(err));
        }
    }

    /* Disarm what this reload no longer wants. `gpio_isr_handler_remove` leaves the
     * pin configured, so the interrupt is also disabled outright. */
    uint32_t stale = s_armed_pins & ~wanted;
    for (int pin = 0; pin < 32; pin++) {
        if ((stale & (1u << pin)) == 0) continue;
        gpio_isr_handler_remove(pin);
        (void)gpio_intr_disable(pin);
    }
    s_armed_pins = wanted;
}

/* `tools/events.md` §Delivery: the frame carries the emitted type, the optional data
 * string and the hit count, and is emitted even at zero hits so the bridge sees every
 * emission the model makes. Emitted after the fires, because `task_hits` is their
 * count. */
static void emit_agent_event(const char *name, const char *data, size_t hits)
{
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_string(root, "kind", "agent.event");
    json_node_t *out = json_add_object(root, "data");
    if (out != NULL) {
        json_add_string(out, "type", name);
        if (data != NULL) json_add_string(out, "data", data);
        json_add_int(out, "task_hits", (int64_t)hits);
    }
    char id[16];
    link_next_id(id, sizeof id);
    link_emit_body("event", id, clock_now(), NULL, body);
    json_free(body);
}

size_t sched_trigger_event(const char *name, const char *data)
{
    size_t fired = 0;
    if (s_state != NULL && name != NULL) {
        sched_hit_t hits[SCHED_MAX_TASKS];
        size_t found = sched_match_event(s_state, name, hits, SCHED_MAX_TASKS);
        fired = fire_hits(hits, found);
    }
    /* Even with no state the emission happened and the bridge is told so: a silent
     * `event.emit` would read as a dropped tool call. */
    if (name != NULL) emit_agent_event(name, data, fired);
    return fired;
}

size_t sched_trigger_mqtt(const char *server_id, const char *topic, const char *payload)
{
    if (s_state == NULL) return 0;
    sched_hit_t hits[SCHED_MAX_TASKS];
    size_t found = sched_match_mqtt(s_state, server_id, topic, payload, clock_now(), hits,
                                    SCHED_MAX_TASKS);
    return fire_hits(hits, found);
}

/* ISR: the queue is created at init and never freed, so `xQueueSendFromISR` is the
 * only thing between the edge and the drain task. Nothing here allocates, logs, reads
 * a pin or takes the mutex: `xSemaphoreTake` is not callable from an ISR at all, and
 * `sys_gpio_read` reconfigures the pin, which would disable this interrupt mid-edge. */
void sched_trigger_gpio_isr(int pin)
{
    if (s_gpio_queue == NULL) return;
    sched_gpio_edge_t edge = {.pin = pin};
    BaseType_t higher = pdFALSE;
    xQueueSendFromISR(s_gpio_queue, &edge, &higher);
    if (higher) portYIELD_FROM_ISR();
}

/* `04` §3.17: every edge is reported whether or not a task matched, so the bridge
 * sees the pin move. `edge` is derived from the level the edge delivered: a pin
 * reaching 1 rose, one reaching 0 fell. `mode` is what `sys_gpio_edge_config`
 * configured, which is `input_pullup` for every pin it arms. */
static void emit_gpio_edge(int pin, int level)
{
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_int(root, "pin", pin);
    json_add_int(root, "level", level);
    json_add_string(root, "edge", level != 0 ? "rising" : "falling");
    json_add_string(root, "mode", "input_pullup");
    char id[16];
    link_next_id(id, sizeof id);
    link_emit_body("gpio.edge", id, clock_now(), NULL, body);
    json_free(body);
}

/* The drain task. GPIO matching and firing happen here, on a task with a stack this
 * module owns, not in interrupt context (`02-firmware.md` §5.1). */
static void gpio_task(void *arg)
{
    (void)arg;
    sched_gpio_edge_t edge;
    sched_hit_t hits[SCHED_MAX_TASKS];
    for (;;) {
        if (xQueueReceive(s_gpio_queue, &edge, portMAX_DELAY) != pdTRUE) continue;
        if (s_state == NULL) continue;
        /* The level is read here, on a task, where `sys_gpio_read` is legal. */
        int level = 0;
        if (sys_gpio_read(edge.pin, &level) != ESP_OK) continue;
        emit_gpio_edge(edge.pin, level);
        size_t found = sched_match_gpio(s_state, edge.pin, level, hits, SCHED_MAX_TASKS);
        (void)fire_hits(hits, found);
    }
}

static void tick(void *arg)
{
    (void)arg;
    if (s_state == NULL || s_lock == NULL) return;
    int64_t now = clock_now();
    bool synced = clock_synced();
    /* `02-firmware.md` §5.2: an `every` task without `first_at` counts from boot. */
    int64_t boot_at = synced ? now - (int64_t)(clock_uptime_ms() / 1000) : 0;
    int32_t offset = clock_utc_offset_s();

    if (xSemaphoreTake(s_lock, 0) != pdTRUE) return; /* a command is mid-write */
    for (size_t i = 0; i < s_state->count; i++) {
        sched_task_t *task = &s_state->tasks[i];
        if (!task->enabled || task->type != SCHED_TRIGGER_SCHEDULE) continue;
        if (!sched_repeat_allows(task)) continue;
        if (!synced && task->kind == SCHED_EVERY && !task->has_first_at) continue;
        /* A daily time before the first sync is inert (`03-config.md` §Schedule):
         * firing on the boot epoch would be a wrong instant presented as a right
         * one, and it would count as a missed fire when it is not. */
        if (!synced && task->kind == SCHED_AT && sched_parse_hhmmss(task->at) >= 0) {
            continue;
        }
        int64_t due = sched_last_due(task, now, boot_at, offset);
        /* `last_fired` is what makes a tick idempotent: without it every tick inside
         * the same interval would fire the task again. */
        if (due > 0 && due > task->last_fired &&
            sched_missed_should_fire(task, due, now, SCHED_MISSED_GRACE_S)) {
            (void)fire_locked(i, now, NULL, 0);
        }
    }
    xSemaphoreGive(s_lock);
}

/* --- Ports and state ------------------------------------------------------ */

void sched_set_submit(sched_submit_fn fn)
{
    s_submit = fn;
}

const sched_tasks_t *sched_state(void)
{
    return s_state;
}

esp_err_t sched_render_action(const sched_task_t *task, char *out, size_t cap,
                              size_t *out_len)
{
    if (task == NULL || out == NULL || cap == 0) return ESP_ERR_INVALID_ARG;
    if (task->action == SCHED_ACTION_PROMPT) {
        size_t len = strlen(task->prompt);
        if (len >= cap) return ESP_ERR_INVALID_SIZE;
        memcpy(out, task->prompt, len + 1);
        if (out_len != NULL) *out_len = len;
        return ESP_OK;
    }

    /* A template lives beside the other prompts and is read whole: one parsed from
     * half a file would substitute into a truncated instruction. */
    char path[64];
    snprintf(path, sizeof path, "/prompts/%s.md", task->template_name);
    size_t len = 0;
    esp_err_t err = store_read_file(path, s_text, SCHED_TEXT_MAX, &len);
    if (err != ESP_OK) return err;
    return sched_render_template(s_text, len, task, out, cap, out_len);
}

static void load_runtime(sched_tasks_t *tasks)
{
    for (size_t i = 0; i < tasks->count; i++) {
        sched_task_t *task = &tasks->tasks[i];
        char value[40];
        if (store_nvs_get_str(SCHED_NS, task->id, value, sizeof value, NULL) != ESP_OK) {
            continue;
        }
        int32_t count = 0;
        int64_t last = 0;
        if (sched_state_decode(value, &count, &last)) {
            task->fired_count = count;
            task->last_fired = last;
        }
    }
}

esp_err_t sched_reload(void)
{
    if (s_state == NULL || s_text == NULL) return ESP_ERR_INVALID_STATE;

    size_t len = 0;
    esp_err_t err = store_read_file(SCHED_PATH, s_text, SCHED_TEXT_MAX, &len);
    if (err == ESP_ERR_NOT_FOUND) {
        /* No file is not an error: a device with nothing scheduled is a working
         * device (`02-firmware.md` §degradation). */
        if (xSemaphoreTake(s_lock, portMAX_DELAY) != pdTRUE) return ESP_ERR_INVALID_STATE;
        memset(s_state, 0, sizeof *s_state);
        xSemaphoreGive(s_lock);
        /* No file means no task, so every pin this module armed is now stale. */
        arm_gpio(s_state);
        return ESP_OK;
    }
    if (err != ESP_OK) return err;

    /* Parsed into a second block, not into `s_state`: `sched_parse` zeroes its output
     * before it validates, and `sched_tasks_t` is 87 KiB, so neither a local nor a
     * direct parse is possible. On refusal the armed schedule survives, which is what
     * keeps a file this build cannot read from unscheduling the device. */
    char why[96] = "";
    size_t dropped = 0;
    err = sched_parse(s_text, len, s_scratch, why, sizeof why, &dropped);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "/tasks.json refused: %s", why);
        return err;
    }
    load_runtime(s_scratch);

    if (xSemaphoreTake(s_lock, portMAX_DELAY) != pdTRUE) return ESP_ERR_INVALID_STATE;
    memcpy(s_state, s_scratch, sizeof *s_state);
    xSemaphoreGive(s_lock);
    arm_gpio(s_state);

    if (dropped > 0) {
        ESP_LOGW(TAG, "/tasks.json: %u task(s) dropped, first reason: %s",
                 (unsigned)dropped, why);
    }
    return ESP_OK;
}

esp_err_t sched_init(void)
{
    if (s_state == NULL) {
        /* Every trigger path returns early on a NULL state, so an allocation failure
         * degrades to "nothing scheduled" rather than a crash on the next boot. */
        s_state = sched_alloc(sizeof *s_state);
        s_scratch = sched_alloc(sizeof *s_scratch);
        s_text = sched_alloc(SCHED_TEXT_MAX);
        s_doc = sched_alloc(SCHED_TEXT_MAX);
        s_prompt = sched_alloc(SCHED_PROMPT_MAX + 1);
        if (s_state == NULL || s_scratch == NULL || s_text == NULL || s_doc == NULL ||
            s_prompt == NULL) {
            ESP_LOGE(TAG, "no memory for task state");
            return ESP_ERR_NO_MEM;
        }
        memset(s_state, 0, sizeof *s_state);
    }
    if (s_lock == NULL) s_lock = xSemaphoreCreateMutex();
    if (s_lock == NULL) return ESP_ERR_NO_MEM;
    if (s_gpio_queue == NULL) {
        s_gpio_queue = xQueueCreate(SCHED_GPIO_QUEUE_LEN, sizeof(sched_gpio_edge_t));
    }
    if (s_gpio_queue == NULL) return ESP_ERR_NO_MEM;

    if (s_gpio_task == NULL) {
        /* 4096: `drain` takes the lock and renders an action, nothing larger. */
        if (xTaskCreate(gpio_task, "sched_gpio", 4096, NULL, 5, &s_gpio_task) != pdPASS) {
            return ESP_ERR_NO_MEM;
        }
    }

    return ESP_OK;
}

esp_err_t sched_start(void)
{
    if (s_state == NULL || s_lock == NULL) return ESP_ERR_INVALID_STATE;

    esp_err_t err = sched_reload();
    if (err != ESP_OK) ESP_LOGW(TAG, "no tasks loaded: %s", esp_err_to_name(err));

    if (!s_started) {
        const esp_timer_create_args_t args = {
            .callback = tick,
            .name = "sched",
        };
        err = esp_timer_create(&args, &s_timer);
        if (err == ESP_OK) err = esp_timer_start_periodic(s_timer, SCHED_TICK_US);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "timer: %s", esp_err_to_name(err));
            return err;
        }
        s_started = true;
    }
    /* `02-firmware.md` §3.1 step 9: loading the file and firing `boot` are one step,
     * so a `boot` task cannot exist without having been given the chance to run. */
    (void)sched_trigger_bare(SCHED_TRIGGER_BOOT);
    return ESP_OK;
}

void sched_clear_runtime(const char *id)
{
    if (id == NULL || *id == '\0') return;
    store_nvs_erase(SCHED_NS, id);
}

/* The whole-file rewrite in two halves, because the two need different locks. The
 * emit reads `s_state` and must hold `s_lock`; the flush touches only flash and must
 * not, or `sched_reload` on failure would deadlock against the caller's own take.
 * `store_write_image_chunk` with `final` renames over the target after the size check,
 * so a refused write leaves the previous document armed and a power loss mid-write
 * leaves the staged file rather than a half-written `/tasks.json`. */
static esp_err_t emit_document_locked(size_t *len)
{
    json_doc_t *doc = NULL;
    esp_err_t err = sched_emit_json(s_state, &doc);
    if (err != ESP_OK) return err;
    err = json_emit(doc, s_doc, SCHED_TEXT_MAX + 1, len);
    json_free(doc);
    return err;
}

static esp_err_t flush_document(size_t len)
{
    return store_write_image_chunk(SCHED_PATH, (const uint8_t *)s_doc, len, 0, true, NULL,
                                   NULL);
}

static const char *arg_id(const json_node_t *args)
{
    if (args == NULL) return NULL;
    const char *id = json_string(json_get(args, "id"));
    return (id != NULL && *id != '\0') ? id : NULL;
}

static esp_err_t result_with_id(json_doc_t **out, const char *id, int64_t count)
{
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_add_string(root, "id", id);
    json_add_int(root, "count", count);
    return ESP_OK;
}

/* The stored shape — what `/tasks.json` holds, without the runtime counters
 * `tasks.list` enriches with (`tools/tasks.md` §`task.list` says the tool returns the
 * stored object). One owner for the shape is `sched_emit_json`, which is also what the
 * whole-file rewrite writes, so the tool cannot report a task the file does not hold. */
esp_err_t sched_stored_json(json_doc_t **out)
{
    if (out == NULL) return ESP_ERR_INVALID_ARG;
    *out = NULL;
    if (s_state == NULL) return ESP_ERR_INVALID_STATE;
    if (xSemaphoreTake(s_lock, portMAX_DELAY) != pdTRUE) return ESP_ERR_INVALID_STATE;
    esp_err_t err = sched_emit_json(s_state, out);
    xSemaphoreGive(s_lock);
    return err;
}

esp_err_t sched_command(const char *name, const json_node_t *args, json_doc_t **out,
                        char *why, size_t why_cap)
{
    if (name == NULL || out == NULL) return ESP_ERR_INVALID_ARG;
    *out = NULL;
    if (why != NULL && why_cap > 0) why[0] = '\0';
    if (s_state == NULL) return ESP_ERR_INVALID_STATE;
    size_t len = 0;

    if (strcmp(name, "tasks.list") == 0) {
        int64_t now = clock_now();
        int64_t boot_at = clock_synced() ? now - (int64_t)(clock_uptime_ms() / 1000) : 0;
        return sched_list_json(s_state, now, boot_at, clock_utc_offset_s(), out);
    }

    if (strcmp(name, "tasks.set") == 0) {
        /* `04` §4.6 puts the object under `args.task`; a bare task object is accepted
         * too so a caller sending the schema's own shape is not punished for it. */
        const json_node_t *task_obj = args != NULL ? json_get(args, "task") : NULL;
        if (task_obj == NULL) task_obj = args;
        sched_task_t task;
        memset(&task, 0, sizeof task);
        esp_err_t err = sched_task_validate(task_obj, &task, why, why_cap);
        if (err != ESP_OK) return err;

        bool created = false;
        if (xSemaphoreTake(s_lock, portMAX_DELAY) != pdTRUE) return ESP_ERR_INVALID_STATE;
        err = sched_tasks_upsert(s_state, &task, &created);
        int64_t count = (int64_t)s_state->count;
        if (err == ESP_OK) {
            err = emit_document_locked(&len);
            /* The flush is inside the lock too: a concurrent `tasks.set` must not
             * interleave two whole-file rewrites. */
            if (err == ESP_OK) err = flush_document(len);
        }
        xSemaphoreGive(s_lock);
        if (err == ESP_ERR_INVALID_SIZE) {
            if (why != NULL && why_cap > 0) {
                snprintf(why, why_cap, "tasks: %d entries is the maximum", SCHED_MAX_TASKS);
            }
            return err;
        }
        if (err != ESP_OK) {
            /* The document did not land, so memory is ahead of flash. Reload outside
             * the lock (it takes it) so a task that will vanish on reboot is not
             * served as armed. */
            (void)sched_reload();
            return err;
        }

        err = result_with_id(out, task.id, count);
        if (err != ESP_OK) return err;
        json_add_bool(json_root(*out), "created", created);
        return ESP_OK;
    }

    if (strcmp(name, "tasks.delete") == 0) {
        const char *id = arg_id(args);
        if (id == NULL) {
            if (why != NULL && why_cap > 0) snprintf(why, why_cap, "id: required string");
            return ESP_ERR_INVALID_ARG;
        }
        if (xSemaphoreTake(s_lock, portMAX_DELAY) != pdTRUE) return ESP_ERR_INVALID_STATE;
        bool deleted = false;
        esp_err_t err = sched_tasks_remove(s_state, id, &deleted);
        int64_t count = (int64_t)s_state->count;
        if (err == ESP_OK && deleted) {
            err = emit_document_locked(&len);
            if (err == ESP_OK) err = flush_document(len);
        }
        xSemaphoreGive(s_lock);
        if (err != ESP_OK) {
            (void)sched_reload();
            return err;
        }
        if (!deleted) {
            if (why != NULL && why_cap > 0) snprintf(why, why_cap, "id: %s is not a task", id);
            return ESP_ERR_NOT_FOUND;
        }
        /* `04` §4.7: the runtime state goes in the same operation, so re-adding the id
         * starts at zero. */
        sched_clear_runtime(id);
        err = result_with_id(out, id, count);
        if (err != ESP_OK) return err;
        json_add_bool(json_root(*out), "deleted", true);
        return ESP_OK;
    }

    if (strcmp(name, "tasks.run") == 0) {
        const char *id = arg_id(args);
        if (id == NULL) {
            if (why != NULL && why_cap > 0) snprintf(why, why_cap, "id: required string");
            return ESP_ERR_INVALID_ARG;
        }
        if (xSemaphoreTake(s_lock, portMAX_DELAY) != pdTRUE) return ESP_ERR_INVALID_STATE;
        size_t index = s_state->count;
        for (size_t i = 0; i < s_state->count; i++) {
            if (strcmp(s_state->tasks[i].id, id) == 0) index = i;
        }
        char run_id[24] = "";
        /* `tasks.run` is what `manual` triggers (`04` §4.5): it fires whatever the
         * task's own `trigger` says, and `repeat` still applies, so a spent `once`
         * task refuses here rather than running twice. */
        bool fired = index < s_state->count &&
                     fire_locked(index, clock_now(), run_id, sizeof run_id);
        xSemaphoreGive(s_lock);
        if (index == s_state->count) {
            if (why != NULL && why_cap > 0) snprintf(why, why_cap, "id: %s is not a task", id);
            return ESP_ERR_NOT_FOUND;
        }
        if (!fired) {
            if (why != NULL && why_cap > 0) {
                snprintf(why, why_cap, "%s did not fire: disabled or out of repeats", id);
            }
            return ESP_ERR_INVALID_STATE;
        }
        *out = json_new_object();
        if (*out == NULL) return ESP_ERR_NO_MEM;
        json_node_t *root = json_root(*out);
        json_add_string(root, "task_id", id);
        if (run_id[0] != '\0') json_add_string(root, "run_id", run_id);
        return ESP_OK;
    }

    if (why != NULL && why_cap > 0) snprintf(why, why_cap, "%s is not a tasks command", name);
    return ESP_ERR_NOT_FOUND;
}
