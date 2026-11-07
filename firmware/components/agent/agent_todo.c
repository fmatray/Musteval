/* agent_todo — the nine ops of `tools/todo.md`, their targeting, normalization and
 * bounds. Pure, so every rule the doc states is proven on the host.
 *
 * All-or-nothing: every mutating op runs against a scratch copy and is committed
 * only when it succeeded, so "list unchanged on error" is a structural fact rather
 * than a promise each op is trusted to keep. */

#include "agent.h"

#include <stdio.h>
#include <string.h>

/* The pre-image. One static, because a run is serialized and the ops are called
 * from the run task only; a second copy per nesting level would double the cost for
 * nothing. */
static todo_list_t s_scratch;

static bool copy_text(char *dst, size_t cap, const char *src)
{
    if (src == NULL) return false;
    size_t n = strlen(src);
    if (n >= cap) return false;
    memcpy(dst, src, n + 1);
    return true;
}

/* `tools/todo.md` §Arguments: an over-long value is refused, never truncated. */
static const char *arg_string(const json_node_t *args, const char *key)
{
    return json_string(json_get(args, key));
}

static bool arg_ok_len(const char *value, size_t max)
{
    return value != NULL && strlen(value) <= max;
}

size_t todo_task_total(const todo_list_t *list)
{
    return list != NULL ? list->task_count : 0;
}

void todo_list_clear(todo_list_t *list)
{
    if (list == NULL) return;
    memset(list, 0, sizeof *list);
}

const char *todo_status_name(todo_status_t status)
{
    switch (status) {
    case TODO_PENDING: return "pending";
    case TODO_IN_PROGRESS: return "in_progress";
    case TODO_COMPLETED: return "completed";
    case TODO_ABANDONED: return "abandoned";
    case TODO_BLOCKED: return "blocked";
    }
    return "pending";
}

/* Phase index by exact name, or -1. */
static int phase_find(const todo_list_t *list, const char *name)
{
    for (size_t i = 0; i < list->phase_count; i++) {
        if (strcmp(list->phases[i], name) == 0) return (int)i;
    }
    return -1;
}

static int task_find(const todo_list_t *list, const char *content)
{
    for (size_t i = 0; i < list->task_count; i++) {
        if (strcmp(list->tasks[i].content, content) == 0) return (int)i;
    }
    return -1;
}

/* `content` is unique across the whole list and `name` across phases: both are
 * checked before an insert so a duplicate cannot make targeting ambiguous. */
static bool phase_add(todo_list_t *list, const char *name, uint8_t *out)
{
    int found = phase_find(list, name);
    if (found >= 0) {
        if (out != NULL) *out = (uint8_t)found;
        return true;
    }
    if (list->phase_count >= AGENT_TODO_MAX_PHASES) return false;
    if (!copy_text(list->phases[list->phase_count], AGENT_TODO_PHASE_MAX + 1, name)) {
        return false;
    }
    if (out != NULL) *out = (uint8_t)list->phase_count;
    list->phase_count++;
    return true;
}

/* Insert one task, keeping tasks grouped by phase and in insertion order inside a
 * phase. The position is found first, so a refused insert cannot shift the list. */
static bool task_add(todo_list_t *list, uint8_t phase_index, const char *content)
{
    if (list->task_count >= AGENT_TODO_MAX_TASKS) return false;
    if (task_find(list, content) >= 0) return false;
    if (!arg_ok_len(content, AGENT_TODO_CONTENT_MAX)) return false;

    size_t at = list->task_count;
    for (size_t i = 0; i < list->task_count; i++) {
        if (list->tasks[i].phase_index > phase_index) {
            at = i;
            break;
        }
    }
    if (at < list->task_count) {
        memmove(&list->tasks[at + 1], &list->tasks[at],
                (list->task_count - at) * sizeof list->tasks[0]);
    }
    todo_task_t *t = &list->tasks[at];
    memset(t, 0, sizeof *t);
    if (!copy_text(t->content, sizeof t->content, content)) return false;
    t->status = TODO_PENDING;
    t->phase_index = phase_index;
    list->task_count++;
    return true;
}

/* `tools/todo.md` §Targeting: exact `task` content first, then every task of
 * `phase`, then every task. Returns the match count.
 *
 * `marks` is indexed by task and set for each target, so the ops that skip some
 * statuses can decide without a second walk. */
static size_t resolve_targets(const todo_list_t *list, const char *task,
                              const char *phase, bool *marks)
{
    memset(marks, 0, list->task_count * sizeof marks[0]);

    if (task != NULL) {
        int i = task_find(list, task);
        if (i >= 0) {
            marks[i] = true;
            return 1;
        }
        /* A `task` that names nothing is an error, never a fall-through to "every
         * task": `rm` with a typo would delete the whole list. The chain
         * `task → phase → all` only continues when the caller supplied no task. */
        if (phase == NULL) return 0;
    }
    if (phase != NULL) {
        int p = phase_find(list, phase);
        if (p >= 0) {
            size_t n = 0;
            for (size_t i = 0; i < list->task_count; i++) {
                if (list->tasks[i].phase_index == (uint8_t)p) {
                    marks[i] = true;
                    n++;
                }
            }
            return n;
        }
        /* A phase that matches nothing is a targeting failure, not a no-op: the doc
         * says an empty match is an error. */
        return 0;
    }
    for (size_t i = 0; i < list->task_count; i++) marks[i] = true;
    return list->task_count;
}

static int mark_to_task(todo_list_t *list, const bool *marks, size_t n)
{
    for (size_t i = 0; i < n && i < list->task_count; i++) {
        if (marks[i]) return (int)i;
    }
    return -1;
}

/* `tools/todo.md` §Normalization: at most one `in_progress`, and when none is
 * active the first `pending` task is promoted. Blocked tasks are skipped by the
 * promotion. Applied after every successful mutation, never after `view`. */
static void normalize(todo_list_t *list)
{
    int first_active = -1;
    for (size_t i = 0; i < list->task_count; i++) {
        if (list->tasks[i].status == TODO_IN_PROGRESS) {
            if (first_active < 0) {
                first_active = (int)i;
            } else {
                list->tasks[i].status = TODO_PENDING;
            }
        }
    }
    if (first_active >= 0) return;

    for (size_t i = 0; i < list->task_count; i++) {
        if (list->tasks[i].status == TODO_PENDING) {
            list->tasks[i].status = TODO_IN_PROGRESS;
            return;
        }
    }
}

/* `init` from the nested `list` form: `[{phase, items:[...]}]`. */
static esp_err_t op_init_nested(todo_list_t *list, const json_node_t *args)
{
    const json_node_t *phases = json_get(args, "list");
    size_t n = json_array_len(phases);
    if (n > AGENT_TODO_MAX_PHASES) return ESP_ERR_INVALID_SIZE;

    size_t total = 0;
    for (size_t i = 0; i < n; i++) {
        const json_node_t *entry = json_array_at(phases, i);
        const char *name = json_string(json_get(entry, "phase"));
        const json_node_t *items = json_get(entry, "items");
        size_t inner = json_array_len(items);
        if (!arg_ok_len(name, AGENT_TODO_PHASE_MAX)) return ESP_ERR_INVALID_ARG;
        if (inner == 0) return ESP_ERR_INVALID_ARG;
        total += inner;
        if (total > AGENT_TODO_MAX_TASKS) return ESP_ERR_INVALID_SIZE;
    }

    /* Validated in full before the first insert, so a bad entry in the last phase
     * cannot leave the first phases applied. */
    todo_list_clear(list);
    for (size_t i = 0; i < n; i++) {
        const json_node_t *entry = json_array_at(phases, i);
        const char *name = json_string(json_get(entry, "phase"));
        const json_node_t *items = json_get(entry, "items");
        uint8_t phase = 0;
        if (!phase_add(list, name, &phase)) return ESP_ERR_INVALID_SIZE;
        for (size_t j = 0; j < json_array_len(items); j++) {
            const char *content = json_string(json_array_at(items, j));
            if (!task_add(list, phase, content)) return ESP_ERR_INVALID_ARG;
        }
    }
    return ESP_OK;
}

/* `init` from flat `items` plus an optional `phase`, defaulting to "Tasks". */
static esp_err_t op_init_flat(todo_list_t *list, const json_node_t *args)
{
    const char *phase_name = arg_string(args, "phase");
    if (phase_name == NULL) phase_name = "Tasks";
    if (!arg_ok_len(phase_name, AGENT_TODO_PHASE_MAX)) return ESP_ERR_INVALID_ARG;

    const json_node_t *items = json_get(args, "items");
    size_t n = json_array_len(items);
    if (n > AGENT_TODO_MAX_TASKS) return ESP_ERR_INVALID_SIZE;
    for (size_t i = 0; i < n; i++) {
        const char *content = json_string(json_array_at(items, i));
        if (!arg_ok_len(content, AGENT_TODO_CONTENT_MAX)) return ESP_ERR_INVALID_ARG;
    }

    todo_list_clear(list);
    uint8_t phase = 0;
    if (!phase_add(list, phase_name, &phase)) return ESP_ERR_INVALID_SIZE;
    for (size_t i = 0; i < n; i++) {
        if (!task_add(list, phase, json_string(json_array_at(items, i)))) {
            return ESP_ERR_INVALID_ARG;
        }
    }
    return ESP_OK;
}

/* `append`: adds `pending` tasks to a phase, creating it when missing. */
static esp_err_t op_append(todo_list_t *list, const json_node_t *args)
{
    const char *phase_name = arg_string(args, "phase");
    if (!arg_ok_len(phase_name, AGENT_TODO_PHASE_MAX)) return ESP_ERR_INVALID_ARG;

    const json_node_t *items = json_get(args, "items");
    size_t n = json_array_len(items);
    if (n == 0) return ESP_ERR_INVALID_ARG;
    if (list->task_count + n > AGENT_TODO_MAX_TASKS) return ESP_ERR_INVALID_SIZE;
    for (size_t i = 0; i < n; i++) {
        const char *content = json_string(json_array_at(items, i));
        if (!arg_ok_len(content, AGENT_TODO_CONTENT_MAX)) return ESP_ERR_INVALID_SIZE;
        if (task_find(list, content) >= 0) return ESP_ERR_INVALID_ARG;
    }

    uint8_t phase = 0;
    bool existed = phase_find(list, phase_name) >= 0;
    if (!phase_add(list, phase_name, &phase)) return ESP_ERR_INVALID_SIZE;
    for (size_t i = 0; i < n; i++) {
        if (!task_add(list, phase, json_string(json_array_at(items, i)))) {
            if (!existed) {
                /* Roll the phase back: it was created by this op. */
                list->phase_count--;
            }
            return ESP_ERR_INVALID_ARG;
        }
    }
    return ESP_OK;
}

/* `start`, `done`, `drop`, `rm`, `block`, `unblock` — the targeted ops, whose only
 * differences are which statuses they skip and what they write. */
static esp_err_t op_targeted(todo_list_t *list, const json_node_t *args,
                             const char *op, char *why, size_t why_cap)
{
    const char *phase = arg_string(args, "phase");
    const char *task = arg_string(args, "task");
    if (phase != NULL && !arg_ok_len(phase, AGENT_TODO_PHASE_MAX)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (task != NULL && !arg_ok_len(task, AGENT_TODO_CONTENT_MAX)) {
        return ESP_ERR_INVALID_ARG;
    }

    /* `start` never takes a phase (`tools/todo.md` §Arguments). */
    if (strcmp(op, "start") == 0 && task == NULL) {
        if (why != NULL) snprintf(why, why_cap, "start needs a task");
        return ESP_ERR_INVALID_ARG;
    }
    /* `block`/`unblock` take a task or a phase, never neither. */
    if ((strcmp(op, "block") == 0 || strcmp(op, "unblock") == 0) &&
        task == NULL && phase == NULL) {
        if (why != NULL) snprintf(why, why_cap, "%s needs a task or a phase", op);
        return ESP_ERR_INVALID_ARG;
    }

    bool marks[AGENT_TODO_MAX_TASKS];
    size_t hits = resolve_targets(list, task, phase, marks);
    if (hits == 0) {
        if (why != NULL) snprintf(why, why_cap, "no task matches");
        return ESP_ERR_INVALID_ARG;
    }

    const char *reason = arg_string(args, "reason");

    if (strcmp(op, "start") == 0) {
        /* The scan length is the list, not the hit count: a single hit on task 2 of 3
         * sets `marks[2]` only, and scanning `hits == 1` entries found nothing, so
         * `start` silently did nothing for every task but the first. */
        int at = mark_to_task(list, marks, list->task_count);
        if (at < 0) return ESP_OK;
        /* §Operations: `start` demotes any other active task itself. Leaving it to
         * `normalize` would keep the earlier task, because normalization keeps the
         * first `in_progress` it finds — the opposite of what `start` means. */
        for (size_t i = 0; i < list->task_count; i++) {
            if (list->tasks[i].status == TODO_IN_PROGRESS) {
                list->tasks[i].status = TODO_PENDING;
            }
        }
        list->tasks[at].status = TODO_IN_PROGRESS;
        return ESP_OK;
    }
    if (strcmp(op, "rm") == 0) {
        size_t out = 0;
        for (size_t i = 0; i < list->task_count; i++) {
            if (!marks[i]) list->tasks[out++] = list->tasks[i];
        }
        list->task_count = out;
        return ESP_OK;
    }

    for (size_t i = 0; i < list->task_count; i++) {
        if (!marks[i]) continue;
        todo_task_t *t = &list->tasks[i];
        if (strcmp(op, "done") == 0) {
            t->status = TODO_COMPLETED;
        } else if (strcmp(op, "drop") == 0) {
            t->status = TODO_ABANDONED;
        } else if (strcmp(op, "block") == 0) {
            /* `completed` and `abandoned` are untouched by `block`. */
            if (t->status == TODO_COMPLETED || t->status == TODO_ABANDONED) continue;
            t->status = TODO_BLOCKED;
            if (reason != NULL) {
                if (!copy_text(t->blocker, sizeof t->blocker, reason)) {
                    return ESP_ERR_INVALID_ARG;
                }
            }
            t->has_blocker = t->blocker[0] != '\0';
        } else if (strcmp(op, "unblock") == 0) {
            if (t->status != TODO_BLOCKED) continue;
            t->status = TODO_PENDING;
            t->has_blocker = false;
            t->blocker[0] = '\0';
        }
    }
    return ESP_OK;
}

esp_err_t todo_apply(todo_list_t *list, const json_node_t *args, char *why,
                     size_t why_cap)
{
    if (why != NULL && why_cap > 0) why[0] = '\0';
    if (list == NULL || args == NULL) return ESP_ERR_INVALID_ARG;

    const char *op = arg_string(args, "op");
    if (op == NULL) {
        /* `tools/todo.md`: an omitted `op` is a refusal, never an inference. */
        if (why != NULL) snprintf(why, why_cap, "op is required");
        return ESP_ERR_INVALID_ARG;
    }

    /* `view` is read-only: it must not even normalize, and it cannot fail on state. */
    if (strcmp(op, "view") == 0) return ESP_OK;

    s_scratch = *list;

    esp_err_t err;
    if (strcmp(op, "init") == 0) {
        if (json_get(args, "list") != NULL) {
            err = op_init_nested(&s_scratch, args);
        } else if (json_get(args, "items") != NULL) {
            err = op_init_flat(&s_scratch, args);
        } else {
            /* `list: []` clears everything; so does no argument at all being an
             * explicit empty init. Both are the documented clear. */
            todo_list_clear(&s_scratch);
            err = ESP_OK;
        }
    } else if (strcmp(op, "append") == 0) {
        err = op_append(&s_scratch, args);
    } else if (strcmp(op, "start") == 0 || strcmp(op, "done") == 0 ||
               strcmp(op, "drop") == 0 || strcmp(op, "rm") == 0 ||
               strcmp(op, "block") == 0 || strcmp(op, "unblock") == 0) {
        err = op_targeted(&s_scratch, args, op, why, why_cap);
    } else {
        if (why != NULL) snprintf(why, why_cap, "unknown op");
        return ESP_ERR_INVALID_ARG;
    }

    if (err != ESP_OK) return err;

    normalize(&s_scratch);
    *list = s_scratch;
    return ESP_OK;
}

esp_err_t todo_render(const todo_list_t *list, json_node_t *out)
{
    if (list == NULL || out == NULL) return ESP_ERR_INVALID_ARG;

    json_node_t *phases = json_new_array(out, "phases");
    if (phases == NULL) return ESP_ERR_NO_MEM;

    for (size_t p = 0; p < list->phase_count; p++) {
        json_node_t *entry = json_array_push_object(phases);
        if (entry == NULL) return ESP_ERR_NO_MEM;
        if (!json_add_string(entry, "name", list->phases[p])) return ESP_ERR_NO_MEM;
        json_node_t *tasks = json_new_array(entry, "tasks");
        if (tasks == NULL) return ESP_ERR_NO_MEM;

        for (size_t t = 0; t < list->task_count; t++) {
            const todo_task_t *task = &list->tasks[t];
            if (task->phase_index != (uint8_t)p) continue;
            json_node_t *item = json_array_push_object(tasks);
            if (item == NULL) return ESP_ERR_NO_MEM;
            if (!json_add_string(item, "content", task->content)) return ESP_ERR_NO_MEM;
            if (!json_add_string(item, "status", todo_status_name(task->status))) {
                return ESP_ERR_NO_MEM;
            }
            if (task->has_blocker) {
                if (!json_add_string(item, "blocker", task->blocker)) {
                    return ESP_ERR_NO_MEM;
                }
            }
        }
    }
    return ESP_OK;
}

/* The live tree. Static and one, and in this file rather than in `agent.c` so the
 * rule half links on the host target: the close-out check reads it across runs, which
 * is what makes "this run's work" a property of the tree rather than of the run. */
static todo_list_t s_todo;

todo_list_t *agent_todo_phases(void)
{
    return &s_todo;
}

void agent_todo_reset(void)
{
    todo_list_clear(&s_todo);
}
