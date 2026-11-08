/* Host tests for the pure M4 rules: the nine `todo` ops and their normalization, the
 * close-out verdicts, schema validation and the tool gate, the history bound, the
 * provider-reply parse, and the skills frontmatter. Each asserts an observable
 * result of a rule a frozen document states (`tools/todo.md`, `02-firmware.md` §4.1,
 * `06-security.md` §6, `03-config.md` §skills), not that a function was called. */

#include <string.h>

#include "unity.h"

#include "agent.h"
#include "llm.h"
#include "net.h"
#include "skills.h"
#include "store.h"
#include "sys.h"
#include "tools.h"

/* The shipped tool array for the frozen 25-tool registry, measured from
 * `tools_spec.c`: every entry's name, description and schema serialised compactly.
 * Pinned here so the capacity assertion below fails if a schema grows past the
 * budget the request body was sized for. */
#define TOOL_ARRAY_BUDGET 5120

/* --- helpers -------------------------------------------------------------- */

/* Apply one op described by `json`, returning its result code. The text is parsed
 * with the same codec the wire uses, so a test writes the call the model would. */
static esp_err_t todo_op(todo_list_t *list, const char *json)
{
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(json, strlen(json), &doc));
    char why[96] = "";
    esp_err_t err = todo_apply(list, json_root(doc), why, sizeof why);
    json_free(doc);
    return err;
}

/* The status of the task whose content is `content`, or -1 when absent. */
static int status_of(const todo_list_t *list, const char *content)
{
    for (size_t i = 0; i < list->task_count; i++) {
        if (strcmp(list->tasks[i].content, content) == 0) {
            return (int)list->tasks[i].status;
        }
    }
    return -1;
}

static size_t active_count(const todo_list_t *list)
{
    size_t n = 0;
    for (size_t i = 0; i < list->task_count; i++) {
        if (list->tasks[i].status == TODO_IN_PROGRESS) n++;
    }
    return n;
}

/* --- todo: init and shape ------------------------------------------------- */

/* `tools/todo.md` §Operations: nested `list` form. */
TEST_CASE("todo init builds the nested phase tree", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_op(list,
        "{\"op\":\"init\",\"list\":["
        "{\"phase\":\"Read\",\"items\":[\"open the data sheet\"]},"
        "{\"phase\":\"Write\",\"items\":[\"draft the driver\",\"test it\"]}]}"));

    TEST_ASSERT_EQUAL_size_t(2, list->phase_count);
    TEST_ASSERT_EQUAL_STRING("Read", list->phases[0]);
    TEST_ASSERT_EQUAL_STRING("Write", list->phases[1]);
    TEST_ASSERT_EQUAL_size_t(3, todo_task_total(list));
    /* `init` is a mutation, so the first pending task is promoted by normalization. */
    TEST_ASSERT_EQUAL_INT(TODO_IN_PROGRESS, status_of(list, "open the data sheet"));
    TEST_ASSERT_EQUAL_INT(TODO_PENDING, status_of(list, "draft the driver"));
    TEST_ASSERT_EQUAL_INT(TODO_PENDING, status_of(list, "test it"));
}

/* The flat `items` form defaults to a phase called `Tasks`. */
TEST_CASE("todo init accepts flat items under a default phase", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"init\",\"items\":[\"one\",\"two\"]}"));
    TEST_ASSERT_EQUAL_size_t(1, list->phase_count);
    TEST_ASSERT_EQUAL_STRING("Tasks", list->phases[0]);
    TEST_ASSERT_EQUAL_size_t(2, todo_task_total(list));

    /* An explicit phase overrides the default. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_op(list,
        "{\"op\":\"init\",\"phase\":\"Later\",\"items\":[\"three\"]}"));
    TEST_ASSERT_EQUAL_STRING("Later", list->phases[0]);
}

/* Normalization: the first pending task is promoted, and only one is in progress. */
TEST_CASE("todo normalizes to exactly one in_progress task", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_op(list,
        "{\"op\":\"init\",\"items\":[\"a\",\"b\",\"c\"]}"));
    /* After init the first task is already active. */
    TEST_ASSERT_EQUAL_size_t(1, active_count(list));
    TEST_ASSERT_EQUAL_INT(TODO_IN_PROGRESS, status_of(list, "a"));

    /* Starting `c` demotes `a` rather than leaving two active. */
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"start\",\"task\":\"c\"}"));
    TEST_ASSERT_EQUAL_size_t(1, active_count(list));
    TEST_ASSERT_EQUAL_INT(TODO_IN_PROGRESS, status_of(list, "c"));
    TEST_ASSERT_EQUAL_INT(TODO_PENDING, status_of(list, "a"));
}

/* The status table in §State model: `completed` and `abandoned` are untouched by
 * `block`, and `blocked` returns to `pending` on `unblock`. */
TEST_CASE("todo block skips completed and abandoned tasks", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_op(list,
        "{\"op\":\"init\",\"items\":[\"a\",\"b\",\"c\"]}"));
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"done\",\"task\":\"a\"}"));
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"drop\",\"task\":\"b\"}"));

    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_op(list,
        "{\"op\":\"block\",\"phase\":\"Tasks\",\"reason\":\"no adapter\"}"));
    TEST_ASSERT_EQUAL_INT(TODO_COMPLETED, status_of(list, "a"));
    TEST_ASSERT_EQUAL_INT(TODO_ABANDONED, status_of(list, "b"));
    TEST_ASSERT_EQUAL_INT(TODO_BLOCKED, status_of(list, "c"));

    /* The blocker is reported with the task (`tools/todo.md` §Result). */
    bool found = false;
    for (size_t i = 0; i < list->task_count; i++) {
        if (strcmp(list->tasks[i].content, "c") == 0) {
            TEST_ASSERT_TRUE(list->tasks[i].has_blocker);
            TEST_ASSERT_EQUAL_STRING("no adapter", list->tasks[i].blocker);
            found = true;
        }
    }
    TEST_ASSERT_TRUE(found);

    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"unblock\",\"task\":\"c\"}"));
    /* `c` is now the only pending task, so normalization promotes it. */
    TEST_ASSERT_EQUAL_INT(TODO_IN_PROGRESS, status_of(list, "c"));
    TEST_ASSERT_EQUAL_size_t(1, active_count(list));
}

/* A blocked task is skipped by the promotion, so a list can have none active. */
TEST_CASE("todo promotion skips blocked tasks", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_op(list,
        "{\"op\":\"init\",\"items\":[\"a\",\"b\"]}"));
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"block\",\"task\":\"a\",\"reason\":\"x\"}"));
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"block\",\"task\":\"b\",\"reason\":\"y\"}"));

    TEST_ASSERT_EQUAL_size_t(0, active_count(list));
    TEST_ASSERT_EQUAL_INT(TODO_BLOCKED, status_of(list, "a"));
    TEST_ASSERT_EQUAL_INT(TODO_BLOCKED, status_of(list, "b"));
}

/* An empty target match is an error, never a silent no-op (§Targeting). */
TEST_CASE("todo refuses a target that matches nothing", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"init\",\"items\":[\"a\"]}"));
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
        todo_op(list, "{\"op\":\"done\",\"task\":\"not in the list\"}"));
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
        todo_op(list, "{\"op\":\"rm\",\"phase\":\"no such phase\"}"));
    /* `start` needs a task, not a phase. */
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
        todo_op(list, "{\"op\":\"start\",\"phase\":\"Tasks\"}"));
    TEST_ASSERT_EQUAL_size_t(1, todo_task_total(list));
}

/* Every mutating op is all-or-nothing: a bound breach leaves the list byte-identical
 * (§Bounds, "the returned list and the stored list are then both the pre-call list"). */
TEST_CASE("todo bounds refuse and leave the list unchanged", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_op(list,
        "{\"op\":\"init\",\"items\":[\"a\",\"b\"]}"));
    todo_list_t before = *list;

    /* 33 tasks in one append: refused, nothing partially inserted. */
    char big[2048];
    size_t used = (size_t)snprintf(big, sizeof big,
                                   "{\"op\":\"append\",\"phase\":\"Tasks\",\"items\":[");
    for (int i = 0; i < 33; i++) {
        used += (size_t)snprintf(big + used, sizeof big - used, "%s\"t%d\"",
                                 i == 0 ? "" : ",", i);
    }
    snprintf(big + used, sizeof big - used, "]}");
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_SIZE, todo_op(list, big));
    TEST_ASSERT_EQUAL_MEMORY(&before, list, sizeof before);

    /* A 121-character task content is refused, never truncated. */
    char long_content[AGENT_TODO_CONTENT_MAX + 3];
    memset(long_content, 'x', sizeof long_content - 1);
    long_content[sizeof long_content - 1] = '\0';
    char call[256];
    snprintf(call, sizeof call,
             "{\"op\":\"append\",\"phase\":\"Tasks\",\"items\":[\"%s\"]}",
             long_content);
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_SIZE, todo_op(list, call));
    TEST_ASSERT_EQUAL_MEMORY(&before, list, sizeof before);

    /* A ninth phase is refused. `init` already created one ("Tasks"), so there is
     * room for `AGENT_TODO_MAX_PHASES - 1` appends and the next one is the breach. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_op(list,
        "{\"op\":\"init\",\"items\":[\"keep\"]}"));
    for (int p = 0; p < AGENT_TODO_MAX_PHASES; p++) {
        char phase[128];
        snprintf(phase, sizeof phase,
                 "{\"op\":\"append\",\"phase\":\"p%d\",\"items\":[\"x%d\"]}", p, p);
        esp_err_t err = todo_op(list, phase);
        if (p < AGENT_TODO_MAX_PHASES - 1) {
            TEST_ASSERT_EQUAL_INT(ESP_OK, err);
        } else {
            TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_SIZE, err);
        }
    }
    TEST_ASSERT_EQUAL_size_t(AGENT_TODO_MAX_PHASES, list->phase_count);
}

/* A malformed or missing `op` is `E_TOOL`, never inferred (§Relationship table). */
TEST_CASE("todo requires an op and rejects an unknown one", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
        todo_op(list, "{\"items\":[\"a\"]}"));
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
        todo_op(list, "{\"op\":\"infer\",\"items\":[\"a\"]}"));
    TEST_ASSERT_EQUAL_size_t(0, todo_task_total(list));
}

/* `view` renders without writing, so a list whose active task was demoted by hand is
 * echoed as it is. */
TEST_CASE("todo view echoes without normalizing", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"init\",\"items\":[\"a\",\"b\"]}"));
    list->tasks[0].status = TODO_PENDING;
    list->tasks[1].status = TODO_PENDING;

    json_doc_t *out = json_new_object();
    TEST_ASSERT_NOT_NULL(out);
    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_render(list, json_root(out)));
    /* Rendering does not mutate. */
    TEST_ASSERT_EQUAL_INT(TODO_PENDING, list->tasks[0].status);
    TEST_ASSERT_EQUAL_INT(TODO_PENDING, list->tasks[1].status);
    json_free(out);
}

/* `rm` removes, and the render reflects it. */
TEST_CASE("todo rm drops the task from the render", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK, todo_op(list,
        "{\"op\":\"init\",\"items\":[\"a\",\"b\"]}"));
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"rm\",\"task\":\"a\"}"));
    TEST_ASSERT_EQUAL_INT(-1, status_of(list, "a"));
    TEST_ASSERT_EQUAL_size_t(1, todo_task_total(list));
}

/* --- close-out ------------------------------------------------------------ */

/* `02-firmware.md` §4.1 step 5: open work blocks finalizing. */
TEST_CASE("close-out fails while a task is open", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"init\",\"items\":[\"a\"]}"));
    TEST_ASSERT_EQUAL_INT(CLOSEOUT_OPEN_TASKS, agent_closeout_check(list, 1, true));

    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"done\",\"task\":\"a\"}"));
    TEST_ASSERT_EQUAL_INT(CLOSEOUT_OK, agent_closeout_check(list, 1, true));
}

/* Two or more rounds with no list ever opened is untracked; one round is compliant. */
TEST_CASE("close-out flags untracked work after two rounds", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);

    TEST_ASSERT_EQUAL_INT(CLOSEOUT_OK, agent_closeout_check(list, 1, false));
    TEST_ASSERT_EQUAL_INT(CLOSEOUT_UNTRACKED, agent_closeout_check(list, 2, false));
    /* A list emptied by `done` is tracked, not untracked. */
    TEST_ASSERT_EQUAL_INT(CLOSEOUT_OK, agent_closeout_check(list, 5, true));
}

/* The frozen prompts are distinct, and OK has none. */
TEST_CASE("close-out prompts exist only for a failed verdict", "[m4]")
{
    const char *open_tasks = agent_closeout_prompt(CLOSEOUT_OPEN_TASKS);
    const char *untracked = agent_closeout_prompt(CLOSEOUT_UNTRACKED);
    TEST_ASSERT_NOT_NULL(open_tasks);
    TEST_ASSERT_NOT_NULL(untracked);
    TEST_ASSERT_NOT_EQUAL(0, strcmp(open_tasks, untracked));
    TEST_ASSERT_NULL(agent_closeout_prompt(CLOSEOUT_OK));

    /* `todo_open` is true only when the check still failed as the budget ran out. */
    TEST_ASSERT_TRUE(agent_todo_open_flag(CLOSEOUT_OPEN_TASKS, true));
    TEST_ASSERT_FALSE(agent_todo_open_flag(CLOSEOUT_OPEN_TASKS, false));
    TEST_ASSERT_FALSE(agent_todo_open_flag(CLOSEOUT_OK, true));
}

/* --- tools_validate ------------------------------------------------------- */

static bool validates(const char *tool, const char *args_json)
{
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(args_json, strlen(args_json), &doc));
    char why[TOOLS_ERR_MAX] = "";
    esp_err_t err = tools_validate(tool, json_root(doc), why, sizeof why);
    json_free(doc);
    return err == ESP_OK;
}

/* A required argument that is absent is refused (`tools/gpio.md`). */
TEST_CASE("tools_validate refuses a missing required argument", "[m4]")
{
    TEST_ASSERT_TRUE(validates("gpio.read", "{\"pin\":4}"));
    TEST_ASSERT_FALSE(validates("gpio.read", "{}"));
    TEST_ASSERT_FALSE(validates("gpio.write", "{\"pin\":4}"));
    TEST_ASSERT_TRUE(validates("gpio.write", "{\"pin\":4,\"level\":1}"));
}

/* An argument the schema does not declare is refused, not ignored. */
TEST_CASE("tools_validate refuses an undeclared argument", "[m4]")
{
    TEST_ASSERT_FALSE(validates("gpio.read", "{\"pin\":4,\"bogus\":true}"));
    TEST_ASSERT_FALSE(validates("time.now", "{\"extra\":1}"));
    TEST_ASSERT_TRUE(validates("time.now", "{}"));
}

/* `adc.read` accepts only the ADC1 header pins (`tools/adc.md`). */
TEST_CASE("tools_validate enforces the ADC pin allowlist", "[m4]")
{
    TEST_ASSERT_TRUE(validates("adc.read", "{\"pin\":1}"));
    TEST_ASSERT_TRUE(validates("adc.read", "{\"pin\":2}"));
    TEST_ASSERT_TRUE(validates("adc.read", "{\"pin\":4}"));
    TEST_ASSERT_FALSE(validates("adc.read", "{\"pin\":3}"));
    TEST_ASSERT_FALSE(validates("adc.read", "{\"pin\":21}"));
}

/* i2c bounds: 7-bit usable address range and a 1..64 read (`tools/i2c.md`). */
TEST_CASE("tools_validate enforces the i2c bounds", "[m4]")
{
    TEST_ASSERT_TRUE(validates("i2c.read", "{\"addr\":60,\"len\":4}"));
    TEST_ASSERT_TRUE(validates("i2c.read", "{\"addr\":8,\"len\":64}"));
    TEST_ASSERT_FALSE(validates("i2c.read", "{\"addr\":7,\"len\":4}"));
    TEST_ASSERT_FALSE(validates("i2c.read", "{\"addr\":120,\"len\":4}"));
    TEST_ASSERT_FALSE(validates("i2c.read", "{\"addr\":60,\"len\":0}"));
    TEST_ASSERT_FALSE(validates("i2c.read", "{\"addr\":60,\"len\":65}"));
}

/* A wrong type is refused even when the value would fit as a string. */
TEST_CASE("tools_validate enforces argument types", "[m4]")
{
    TEST_ASSERT_FALSE(validates("gpio.write", "{\"pin\":\"4\",\"level\":1}"));
    TEST_ASSERT_FALSE(validates("gpio.write", "{\"pin\":4,\"level\":2}"));
    /* `boolean`: `fs.delete`'s `recursive` is a JSON bool, not a `0`/`1`. */
    TEST_ASSERT_TRUE(validates("fs.delete", "{\"path\":\"/workspace/a\",\"recursive\":true}"));
    TEST_ASSERT_FALSE(validates("fs.delete", "{\"path\":\"/workspace/a\",\"recursive\":1}"));
    /* `object`: `http.get`'s `headers` is a map, and a string is refused. */
    TEST_ASSERT_TRUE(validates("http.get", "{\"url\":\"https://a.test/\",\"headers\":{}}"));
    TEST_ASSERT_FALSE(validates("http.get", "{\"url\":\"https://a.test/\",\"headers\":\"x\"}"));
}

/* `todo` is validated against the same schema the model receives, so `op` must be one
 * of the nine names. */
TEST_CASE("tools_validate constrains todo op to the nine names", "[m4]")
{
    TEST_ASSERT_TRUE(validates("todo", "{\"op\":\"view\"}"));
    TEST_ASSERT_TRUE(validates("todo", "{\"op\":\"append\",\"items\":[\"a\"]}"));
    TEST_ASSERT_FALSE(validates("todo", "{\"op\":\"infer\"}"));
    TEST_ASSERT_FALSE(validates("todo", "{}"));
}

/* A name the registry does not hold and an argument that is not an object are refused
 * with their own words, so the run's error says which one went wrong
 * (`02-firmware.md` §7.1: validation runs before dispatch). */
TEST_CASE("tools_validate refuses an unknown tool and a non-object argument", "[m4]")
{
    json_doc_t *doc = NULL;
    char why[TOOLS_ERR_MAX] = "";

    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse("{}", 2, &doc));
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
                          tools_validate("nope.nothing", json_root(doc), why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "unknown tool"));
    json_free(doc);

    /* `{}` is valid for `time.now`, so an array and a bare string are what remains. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse("[]", 2, &doc));
    why[0] = '\0';
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
                          tools_validate("time.now", json_root(doc), why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "arguments must be an object"));
    json_free(doc);

    /* No arguments at all is the same refusal, not a crash. */
    why[0] = '\0';
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
                          tools_validate("time.now", NULL, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "arguments must be an object"));
}

/* `tools/memory.md` §Limits states the key alphabet, the 15-byte bound and the four
 * reserved prefixes. The board's `memory_*` handlers enforced a different alphabet
 * (`[a-z0-9_.-]{1,64}`) and did not refuse reserved prefixes at all, so those three
 * rules were documented and unenforced. */
TEST_CASE("a memory key obeys the documented alphabet and length", "[m4]")
{
    TEST_ASSERT_TRUE(tools_memory_key_ok("user_timezone"));
    TEST_ASSERT_TRUE(tools_memory_key_ok("User2"));
    TEST_ASSERT_TRUE(tools_memory_key_ok("a_"));

    /* 15 is the bound, 16 is over it: the same limit `secrets` uses. */
    TEST_ASSERT_TRUE(tools_memory_key_ok("abcdefghijklmno"));
    TEST_ASSERT_FALSE(tools_memory_key_ok("abcdefghijklmnop"));

    /* Empty, NULL and every character outside `[A-Za-z0-9_]`. */
    TEST_ASSERT_FALSE(tools_memory_key_ok(""));
    TEST_ASSERT_FALSE(tools_memory_key_ok(NULL));
    TEST_ASSERT_FALSE(tools_memory_key_ok("has space"));
    TEST_ASSERT_FALSE(tools_memory_key_ok("has.dot"));
    TEST_ASSERT_FALSE(tools_memory_key_ok("has-dash"));
    TEST_ASSERT_FALSE(tools_memory_key_ok("has/slash"));
}

TEST_CASE("a reserved memory prefix is refused and named", "[m4]")
{
    TEST_ASSERT_EQUAL_STRING("sys_", tools_memory_reserved_prefix("sys_uptime"));
    TEST_ASSERT_EQUAL_STRING("cfg_", tools_memory_reserved_prefix("cfg_x"));
    TEST_ASSERT_EQUAL_STRING("secret_", tools_memory_reserved_prefix("secret_a"));
    TEST_ASSERT_EQUAL_STRING("rt_", tools_memory_reserved_prefix("rt_counter"));

    /* A prefix must be a prefix: these are ordinary keys. */
    TEST_ASSERT_NULL(tools_memory_reserved_prefix("system"));
    TEST_ASSERT_NULL(tools_memory_reserved_prefix("user_timezone"));
    TEST_ASSERT_NULL(tools_memory_reserved_prefix(""));
    TEST_ASSERT_NULL(tools_memory_reserved_prefix(NULL));
}

/* `04-protocol.md` §5 and every tool doc's §Errors agree: a tool's own
 * refusal is `E_TOOL`, a workspace quota is `E_STORAGE`. The board answered
 * `E_STORAGE` for an absent path because `ESP_ERR_NOT_FOUND` fell through to the
 * storage arm, which tells the bridge the flash failed rather than the call. */
TEST_CASE("a tool failure carries the documented wire code", "[m4]")
{
    /* Refusals the model can fix, whatever the tool. */
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("fs.read", ESP_ERR_NOT_FOUND));
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("skill.read", ESP_ERR_NOT_FOUND));
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("memory.get", ESP_ERR_NOT_FOUND));
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("todo", ESP_ERR_INVALID_ARG));
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("gpio.write",
                                                       ESP_ERR_NOT_SUPPORTED));
    /* `ESP_ERR_INVALID_STATE` is what the three "not usable right now" refusals
     * return — an unpinned i2c bus, an unsynced clock, a read of `config.json` — and
     * each is a call the model can make differently, so none is storage. */
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("i2c.read",
                                                       ESP_ERR_INVALID_STATE));
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("time.now",
                                                       ESP_ERR_INVALID_STATE));
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("fs.read",
                                                       ESP_ERR_INVALID_STATE));

    /* The one code with two meanings: the workspace quota versus a call bound. */
    TEST_ASSERT_EQUAL_STRING("E_STORAGE", tools_wire_code("fs.write",
                                                          ESP_ERR_INVALID_SIZE));
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("memory.set",
                                                       ESP_ERR_INVALID_SIZE));
    TEST_ASSERT_EQUAL_STRING("E_TOOL", tools_wire_code("skill.read",
                                                       ESP_ERR_INVALID_SIZE));

    /* A network tool reports the link, never storage. */
    TEST_ASSERT_EQUAL_STRING("E_WIFI", tools_wire_code("http.get", ESP_ERR_NOT_FOUND));
    TEST_ASSERT_EQUAL_STRING("E_TIMEOUT", tools_wire_code("http.get", ESP_ERR_TIMEOUT));

    TEST_ASSERT_EQUAL_STRING("E_INTERNAL", tools_wire_code("fs.write", ESP_ERR_NO_MEM));
    TEST_ASSERT_EQUAL_STRING("E_TIMEOUT", tools_wire_code("time.now", ESP_ERR_TIMEOUT));
    TEST_ASSERT_NULL(tools_wire_code("time.now", ESP_OK));
}

/* --- gating (`06-security.md` §6) ----------------------------------------- */

static tools_policy_t policy_with(bool dangerous)
{
    tools_policy_t policy = {0};
    policy.allow_dangerous = dangerous;
    return policy;
}

/* An actuation tool is refused when `tools.allow_dangerous` is false, and the message
 * is the one `tools/gpio.md` quotes. */
TEST_CASE("the gate refuses actuation without allow_dangerous", "[m4]")
{
    tools_policy_t closed = policy_with(false);
    const tools_builtin_t *write_tool = tools_find("gpio.write");
    TEST_ASSERT_NOT_NULL(write_tool);

    char why[TOOLS_ERR_MAX] = "";
    TEST_ASSERT_EQUAL_INT(ESP_ERR_NOT_ALLOWED,
        tools_gate_check(write_tool, &closed, NULL, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "gpio.write refused"));
    TEST_ASSERT_NOT_NULL(strstr(why, "allow_dangerous"));

    /* A read tool needs no permission. */
    const tools_builtin_t *read_tool = tools_find("gpio.read");
    TEST_ASSERT_NOT_NULL(read_tool);
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        tools_gate_check(read_tool, &closed, NULL, why, sizeof why));

    tools_policy_t open = policy_with(true);
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        tools_gate_check(write_tool, &open, NULL, why, sizeof why));
}

/* `http.get` is refused unless its host is allowlisted, exactly. */
TEST_CASE("the gate allowlists http.get by exact host", "[m4]")
{
    json_doc_t *config = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        json_parse("[\"api.open-meteo.com\"]",
                   strlen("[\"api.open-meteo.com\"]"), &config));
    TEST_ASSERT_NOT_NULL(config);
    tools_policy_t policy = policy_with(false);
    policy.http_allow_hosts = json_root(config);

    const tools_builtin_t *http = tools_find("http.get");
    TEST_ASSERT_NOT_NULL(http);

    char why[TOOLS_ERR_MAX] = "";
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        tools_gate_check(http, &policy, "api.open-meteo.com", why, sizeof why));
    TEST_ASSERT_EQUAL_INT(ESP_ERR_NOT_ALLOWED,
        tools_gate_check(http, &policy, "example.com", why, sizeof why));
    /* A suffix is not a match: `open-meteo.com.evil.test` must not pass. */
    TEST_ASSERT_EQUAL_INT(ESP_ERR_NOT_ALLOWED,
        tools_gate_check(http, &policy, "api.open-meteo.com.evil.test", why,
                         sizeof why));
    /* No host at all is a refusal, not an assumption. */
    TEST_ASSERT_EQUAL_INT(ESP_ERR_NOT_ALLOWED,
        tools_gate_check(http, &policy, NULL, why, sizeof why));

    json_free(config);
}

/* A plain-`http://` URL yields no host, so it cannot be allowlisted into existence;
 * userinfo and port are dropped before the comparison. */
TEST_CASE("the gate extracts the host and refuses plain http", "[m4]")
{
    char host[128];
    json_doc_t *doc = NULL;

    const char *args = "{\"url\":\"https://user:pw@api.open-meteo.com:443/v1?x=1\"}";
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(args, strlen(args), &doc));
    const tools_builtin_t *http = tools_find("http.get");
    TEST_ASSERT_NOT_NULL(tools_gate_host_for(http, json_root(doc), host, sizeof host));
    TEST_ASSERT_EQUAL_STRING("api.open-meteo.com", host);
    json_free(doc);

    const char *plain = "{\"url\":\"http://api.open-meteo.com/v1\"}";
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(plain, strlen(plain), &doc));
    TEST_ASSERT_NULL(tools_gate_host_for(http, json_root(doc), host, sizeof host));
    json_free(doc);
}

/* `tools_host_allowed` is an exact match against the configured array. */
TEST_CASE("tools_host_allowed matches exactly", "[m4]")
{
    json_doc_t *config = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        json_parse("[\"a.test\",\"b.test\"]",
                   strlen("[\"a.test\",\"b.test\"]"), &config));
    TEST_ASSERT_NOT_NULL(config);
    tools_policy_t policy = policy_with(false);
    policy.http_allow_hosts = json_root(config);

    TEST_ASSERT_TRUE(tools_host_allowed(&policy, "a.test"));
    TEST_ASSERT_TRUE(tools_host_allowed(&policy, "b.test"));
    TEST_ASSERT_FALSE(tools_host_allowed(&policy, "c.test"));
    TEST_ASSERT_FALSE(tools_host_allowed(&policy, "A.TEST"));
    TEST_ASSERT_FALSE(tools_host_allowed(&policy, "a.test.evil"));
    TEST_ASSERT_FALSE(tools_host_allowed(&policy, NULL));
    json_free(config);
}

/* `07-roadmap.md`: a tool whose milestone has not shipped is refused rather than
 * answered, and the message names the milestone that owes it. One shared message
 * named the scheduler for `geo` and `mqtt` too, which sent a reader to the wrong
 * milestone; the wording now lives in the host-tested `tools_spec.c`. */
TEST_CASE("an unbuilt tool names the milestone that owes it", "[m4]")
{
    TEST_ASSERT_EQUAL_STRING("geo.locate is not implemented before M6",
                             tools_unbuilt_reason("geo.locate"));
    TEST_ASSERT_EQUAL_STRING("mqtt is not implemented before M6",
                             tools_unbuilt_reason("mqtt.list"));
    TEST_ASSERT_EQUAL_STRING("mqtt is not implemented before M6",
                             tools_unbuilt_reason("mqtt.publish"));
    TEST_ASSERT_EQUAL_STRING("the scheduler is not implemented before M5",
                             tools_unbuilt_reason("task.list"));
    TEST_ASSERT_EQUAL_STRING("the scheduler is not implemented before M5",
                             tools_unbuilt_reason("task.set"));
    TEST_ASSERT_EQUAL_STRING("the scheduler is not implemented before M5",
                             tools_unbuilt_reason("event.emit"));
    /* A built tool has no reason, and neither does an absent name: the caller's
     * fallback is what answers those. */
    TEST_ASSERT_NULL(tools_unbuilt_reason("time.now"));
    TEST_ASSERT_NULL(tools_unbuilt_reason("fs.read"));
    TEST_ASSERT_NULL(tools_unbuilt_reason("nope.nothing"));
    TEST_ASSERT_NULL(tools_unbuilt_reason(NULL));
}

/* `tools/i2c.md`: the three i2c tools appear together or not at all, and only when
 * the bus pins are configured. Everything else is always offered. */
TEST_CASE("i2c tools are offered only when the bus is pinned", "[m4]")
{
    tools_context_t ctx;
    memset(&ctx, 0, sizeof ctx);
    ctx.i2c_sda = -1;
    ctx.i2c_scl = -1;

    TEST_ASSERT_FALSE(tools_offered("i2c.read", &ctx));
    TEST_ASSERT_FALSE(tools_offered("i2c.write", &ctx));
    TEST_ASSERT_FALSE(tools_offered("i2c.scan", &ctx));
    TEST_ASSERT_TRUE(tools_offered("gpio.read", &ctx));
    TEST_ASSERT_TRUE(tools_offered("time.now", &ctx));
    /* Half a bus is not a bus: one pin configured leaves all three absent. */
    ctx.i2c_sda = 5;
    TEST_ASSERT_FALSE(tools_offered("i2c.read", &ctx));
    ctx.i2c_scl = 6;
    TEST_ASSERT_TRUE(tools_offered("i2c.read", &ctx));
    TEST_ASSERT_TRUE(tools_offered("i2c.write", &ctx));
    TEST_ASSERT_TRUE(tools_offered("i2c.scan", &ctx));
    TEST_ASSERT_FALSE(tools_offered(NULL, &ctx));
    /* A NULL context has no pins, so the bus family stays absent and nothing else
     * is affected. */
    TEST_ASSERT_FALSE(tools_offered("i2c.read", NULL));
    TEST_ASSERT_TRUE(tools_offered("gpio.read", NULL));
}

/* `04-protocol.md` §3.3 allows exactly `E_BUSY`, `E_CONFIG` and `E_PROTOCOL` for a
 * rejected prompt, and §4 makes a missing required argument `E_PROTOCOL`. Before
 * this table existed, `main.c` mapped every unrecognised `esp_err_t` to `E_INTERNAL`
 * with `retryable:true`, so a `prompt` with no `text` told the bridge the loop was
 * not started and asked it to retry a frame that can never succeed. */
TEST_CASE("a rejected prompt names one of the documented codes", "[m4]")
{
    const agent_rejection_t *r = agent_prompt_rejection(ESP_ERR_INVALID_ARG);
    TEST_ASSERT_EQUAL_STRING("E_PROTOCOL", r->code);
    TEST_ASSERT_FALSE(r->retryable);
    TEST_ASSERT_FALSE(r->queue_detail);

    r = agent_prompt_rejection(ESP_ERR_INVALID_STATE);
    TEST_ASSERT_EQUAL_STRING("E_BUSY", r->code);
    TEST_ASSERT_TRUE(r->retryable);
    /* `04` §3.15's example carries the depth, so the bridge can show why. */
    TEST_ASSERT_TRUE(r->queue_detail);

    r = agent_prompt_rejection(ESP_ERR_INVALID_SIZE);
    TEST_ASSERT_EQUAL_STRING("E_PROTOCOL", r->code);
    TEST_ASSERT_FALSE(r->retryable);

    /* `retryable` is the code's property, not the situation's: `04` §5 fixes it per
     * code, and `E_INTERNAL` is `yes` for both of these. */
    r = agent_prompt_rejection(ESP_ERR_NOT_FOUND);
    TEST_ASSERT_EQUAL_STRING("E_INTERNAL", r->code);
    TEST_ASSERT_TRUE(r->retryable);
    TEST_ASSERT_FALSE(r->queue_detail);

    r = agent_prompt_rejection(ESP_ERR_NO_MEM);
    TEST_ASSERT_EQUAL_STRING("E_INTERNAL", r->code);
    TEST_ASSERT_TRUE(r->retryable);

    /* An unnamed fault still answers rather than dropping the frame (§5). */
    r = agent_prompt_rejection(ESP_ERR_INVALID_RESPONSE);
    TEST_ASSERT_EQUAL_STRING("E_INTERNAL", r->code);
    TEST_ASSERT_NOT_NULL(r->message);
    TEST_ASSERT_TRUE(r->retryable);
}

/* --- declarative tool files ------------------------------------------------ */

static bool secret_yes(const char *key, void *ctx)
{
    (void)ctx;
    return key != NULL && strcmp(key, "brave_key") == 0;
}

/* `03-config.md` §tools and `tools/web.md` put every HTTP member under `http`. The
 * loader read them from the root, so the documented example below had no `url` and
 * was dropped as "description and url are required strings" — every declarative tool
 * would have failed to load. */
TEST_CASE("a declarative tool is read from its documented shape", "[m4]")
{
    static const char *file =
        "{\"description\":\"Search the web.\","
        "\"parameters\":{\"type\":\"object\",\"properties\":{}},"
        "\"http\":{\"method\":\"POST\","
        "\"url\":\"https://api.search.brave.com/res/v1/web/search\","
        "\"response_path\":\"web.results\","
        "\"headers\":{\"Accept\":\"application/json\","
        "\"X-Subscription-Token\":\"nvs:brave_key\"},"
        "\"timeout_ms\":15000}}";

    tools_declared_t tool;
    char why[128] = "";
    TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
        "web_search", file, strlen(file), secret_yes, NULL, &tool, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("web_search", tool.name);
    TEST_ASSERT_EQUAL_STRING("POST", tool.method);
    TEST_ASSERT_EQUAL_STRING("https://api.search.brave.com/res/v1/web/search",
                             tool.url);
    TEST_ASSERT_EQUAL_STRING("web.results", tool.response_path);
    TEST_ASSERT_EQUAL_INT(15000, tool.timeout_ms);
    /* `compile_headers` emitted an empty wrapper and then wrote over the buffer, so
     * a conforming headers object arrived as `{}`. Both members must be present. */
    TEST_ASSERT_NOT_NULL(strstr(tool.headers, "\"Accept\":\"application/json\""));
    TEST_ASSERT_NOT_NULL(strstr(tool.headers, "nvs:brave_key"));
}

/* `03-config.md` §tools: the bound is 1..60000, and a value outside it takes the
 * default rather than refusing a file that is otherwise valid. */
TEST_CASE("a declarative timeout outside its bound takes the default", "[m4]")
{
    static const char *file =
        "{\"description\":\"d\",\"url\":\"https://a.test/\",\"http\":{"
        "\"url\":\"https://a.test/\",\"timeout_ms\":%d}}";
    char buf[256];
    tools_declared_t tool;
    char why[128];
    int bad[] = {0, -1, 60001};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        snprintf(buf, sizeof buf, file, bad[i]);
        TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
            "t", buf, strlen(buf), NULL, NULL, &tool, why, sizeof why));
        TEST_ASSERT_EQUAL_INT(TOOLS_HTTP_TIMEOUT_MS, tool.timeout_ms);
    }
    snprintf(buf, sizeof buf, file, 60000);
    TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
        "t", buf, strlen(buf), NULL, NULL, &tool, why, sizeof why));
    TEST_ASSERT_EQUAL_INT(60000, tool.timeout_ms);
}

/* `tools/web.md`: a file with a built-in's stem is dropped; a non-https url, a
 * method outside GET/POST, and a `response_path` `json_path_get` cannot walk are
 * dropped too, each before the run rather than at the first call. */
TEST_CASE("a declarative file that breaks a rule is dropped", "[m4]")
{
    tools_declared_t tool;
    char why[128];
    const struct {
        const char *stem;
        const char *body;
    } bad[] = {
        /* built-in stem */
        {"time.now", "{\"description\":\"d\",\"http\":{\"url\":\"https://a.test/\"}}"},
        /* no http object at all */
        {"t", "{\"description\":\"d\",\"url\":\"https://a.test/\"}"},
        /* plain http */
        {"t", "{\"description\":\"d\",\"http\":{\"url\":\"http://a.test/\"}}"},
        /* method outside the contract */
        {"t", "{\"description\":\"d\",\"http\":{\"url\":\"https://a.test/\","
              "\"method\":\"PUT\"}}"},
        /* a response_path with a space or a bracket */
        {"t", "{\"description\":\"d\",\"http\":{\"url\":\"https://a.test/\","
              "\"response_path\":\"a[0]\"}}"},
        /* not JSON */
        {"t", "{"},
    };
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        why[0] = '\0';
        TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG, tools_declared_compile(
            bad[i].stem, bad[i].body, strlen(bad[i].body), secret_yes, NULL, &tool,
            why, sizeof why));
        /* Every drop names its reason: a silent drop is indistinguishable from a
         * file that was never read. */
        TEST_ASSERT_TRUE(why[0] != '\0');
    }

    /* An `nvs:` ref that does not resolve is its own answer, so the caller can log
     * it as the `E_CONFIG` drop `03-config.md` check 10 names (`tools.h`). */
    static const char *missing =
        "{\"description\":\"d\",\"http\":{\"url\":\"https://a.test/\","
        "\"headers\":\"nvs:missing\"}}";
    why[0] = '\0';
    TEST_ASSERT_EQUAL_INT(ESP_ERR_NOT_FOUND, tools_declared_compile(
        "t", missing, strlen(missing), secret_yes, NULL, &tool, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "does not resolve"));
}

/* --- history -------------------------------------------------------------- */

/* `03-config.md` §agent `max_history_messages`: the bound holds after every append,
 * because a provider charges for what is resent. */
TEST_CASE("history keeps at most max_messages messages", "[m4]")
{
    char history[8192];
    size_t used = 0;
    agent_history_append(history, sizeof history, &used, "{\"role\":\"user\",\"content\":\"0\"}", 2);
    TEST_ASSERT_EQUAL_size_t(1, agent_history_count(history));

    agent_history_append(history, sizeof history, &used, "{\"role\":\"user\",\"content\":\"1\"}", 2);
    agent_history_append(history, sizeof history, &used, "{\"role\":\"user\",\"content\":\"2\"}", 2);
    TEST_ASSERT_EQUAL_size_t(2, agent_history_count(history));
    /* The oldest was trimmed, not the newest. */
    TEST_ASSERT_NULL(strstr(history, "\"0\""));
    TEST_ASSERT_NOT_NULL(strstr(history, "\"2\""));

    /* Still one valid JSON array. */
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(history, strlen(history), &doc));
    TEST_ASSERT_TRUE(json_is_array(json_root(doc)));
    json_free(doc);
}

/* `max_messages == 0` is a successful no-op that still leaves a valid empty array. */
TEST_CASE("history with a zero bound stays an empty array", "[m4]")
{
    char history[256] = "[]";
    size_t used = 2;
    agent_history_append(history, sizeof history, &used, "{\"role\":\"user\"}", 0);
    TEST_ASSERT_EQUAL_STRING("[]", history);
    TEST_ASSERT_EQUAL_size_t(0, agent_history_count(history));

    used = 2;
    agent_history_trim(history, &used, 0);
    TEST_ASSERT_EQUAL_STRING("[]", history);
}

/* Escaped quotes and brackets inside a string do not confuse the element walk. */
TEST_CASE("history counts through escaped and bracketed strings", "[m4]")
{
    char history[512];
    size_t used = 0;
    agent_history_append(history, sizeof history, &used,
                         "{\"role\":\"user\",\"content\":\"a ] \\\" b [ c\"}", 8);
    TEST_ASSERT_EQUAL_size_t(1, agent_history_count(history));

    agent_history_append(history, sizeof history, &used,
                         "{\"role\":\"assistant\",\"content\":\"ok\"}", 8);
    TEST_ASSERT_EQUAL_size_t(2, agent_history_count(history));
}

/* `02-firmware.md` §4.2 "Buffer Full": the response is "Truncate oldest history
 * entry in PSRAM", not "drop the new one". An append that cannot fit must make room,
 * because the message it is being handed is the round just completed — losing it
 * leaves the provider with a tool call and no answer, or a question with no reply. */
TEST_CASE("history_append truncates the oldest entry when the buffer is full", "[m4]")
{
    /* Each message is 48 bytes, so two plus the brackets are 99 of the 128 available;
     * a third needs 150 and has to displace one. */
    char history[128];
    size_t used = 0;
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"user\",\"content\":\"aaaaaaaaaaaaaaaaaaaa\"}", 32));
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"assistant\",\"content\":\"bbbbbbbbbbbbbbbbbbbb\"}", 32));
    TEST_ASSERT_EQUAL_size_t(2, agent_history_count(history));

    /* This message does not fit beside both, so the oldest goes rather than it. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"user\",\"content\":\"cccccccccccccccccccc\"}", 32));
    TEST_ASSERT_EQUAL_size_t(2, agent_history_count(history));
    TEST_ASSERT_NULL(strstr(history, "aaaa"));
    TEST_ASSERT_NOT_NULL(strstr(history, "bbbb"));
    TEST_ASSERT_NOT_NULL(strstr(history, "cccc"));
    TEST_ASSERT_EQUAL_size_t(strlen(history), used);

    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(history, strlen(history), &doc));
    TEST_ASSERT_TRUE(json_is_array(json_root(doc)));
    json_free(doc);
}

/* A message larger than the whole buffer cannot be made room for. That one case is
 * still an error — but a named one, not a silent loss. */
TEST_CASE("history_append reports a message that cannot fit at all", "[m4]")
{
    char history[64];
    size_t used = 0;
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"user\",\"content\":\"a\"}", 32));
    char big[300];
    memset(big, 'x', sizeof big - 1);
    big[0] = '{'; big[1] = '"'; big[2] = 'r'; big[3] = '"'; big[4] = ':';
    big[sizeof big - 1] = '\0';
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_SIZE,
        agent_history_append(history, sizeof history, &used, big, 32));
    /* The history it could not extend is untouched, not corrupted. */
    TEST_ASSERT_EQUAL_size_t(1, agent_history_count(history));
    TEST_ASSERT_NOT_NULL(strstr(history, "user"));
}

/* --- provider reply ------------------------------------------------------- */

static const char *REPLY_TEXT =
    "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"hello\"},"
    "\"finish_reason\":\"stop\"}],"
    "\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":7,\"total_tokens\":18}}";

/* The common shape: one text answer, no tool calls. */
TEST_CASE("agent_parse_reply reads content and usage", "[m4]")
{
    agent_reply_t *reply = (agent_reply_t *)calloc(1, sizeof(agent_reply_t));
    TEST_ASSERT_NOT_NULL(reply);
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        agent_parse_reply(REPLY_TEXT, strlen(REPLY_TEXT), reply));
    TEST_ASSERT_EQUAL_STRING("hello", reply->content);
    TEST_ASSERT_EQUAL_size_t(5, reply->content_len);
    TEST_ASSERT_EQUAL_size_t(0, reply->call_count);
    TEST_ASSERT_EQUAL_INT32(11, reply->prompt_tokens);
    TEST_ASSERT_EQUAL_INT32(7, reply->completion_tokens);
    free(reply);
}

/* A tool call carries its id, name and the arguments as the provider sent them. */
TEST_CASE("agent_parse_reply reads a tool call", "[m4]")
{
    static const char *body =
        "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"\","
        "\"tool_calls\":[{\"id\":\"call_1\",\"type\":\"function\","
        "\"function\":{\"name\":\"gpio.read\",\"arguments\":\"{\\\"pin\\\":4}\"}}]},"
        "\"finish_reason\":\"tool_calls\"}]}";
    agent_reply_t *reply = (agent_reply_t *)calloc(1, sizeof(agent_reply_t));
    TEST_ASSERT_NOT_NULL(reply);
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_parse_reply(body, strlen(body), reply));
    TEST_ASSERT_EQUAL_size_t(1, reply->call_count);
    TEST_ASSERT_EQUAL_STRING("call_1", reply->calls[0].id);
    TEST_ASSERT_EQUAL_STRING("gpio.read", reply->calls[0].name);
    TEST_ASSERT_EQUAL_STRING("{\"pin\":4}", reply->calls[0].args);
    free(reply);
}

/* An `error`-only body is not a chat completion: the loop reports it as a provider
 * fault so a fallback can answer (`02-firmware.md` §4.2). */
TEST_CASE("agent_parse_reply refuses an error body", "[m4]")
{
    agent_reply_t *reply = (agent_reply_t *)calloc(1, sizeof(agent_reply_t));
    TEST_ASSERT_NOT_NULL(reply);
    static const char *body = "{\"error\":{\"message\":\"rate limited\"}}";
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
        agent_parse_reply(body, strlen(body), reply));
    free(reply);
}

/* The fourth call is refused rather than silently dropped, because a dropped tool call
 * leaves the model's plan half-executed. */
TEST_CASE("agent_parse_reply refuses more than four tool calls", "[m4]")
{
    static const char *body =
        "{\"choices\":[{\"message\":{\"content\":\"\",\"tool_calls\":["
        "{\"id\":\"1\",\"function\":{\"name\":\"time.now\",\"arguments\":\"{}\"}},"
        "{\"id\":\"2\",\"function\":{\"name\":\"time.now\",\"arguments\":\"{}\"}},"
        "{\"id\":\"3\",\"function\":{\"name\":\"time.now\",\"arguments\":\"{}\"}},"
        "{\"id\":\"4\",\"function\":{\"name\":\"time.now\",\"arguments\":\"{}\"}},"
        "{\"id\":\"5\",\"function\":{\"name\":\"time.now\",\"arguments\":\"{}\"}}]}}]}";
    agent_reply_t *reply = (agent_reply_t *)calloc(1, sizeof(agent_reply_t));
    TEST_ASSERT_NOT_NULL(reply);
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
        agent_parse_reply(body, strlen(body), reply));
    free(reply);
}

/* A call with no `arguments` member, and a call with no `function.name`, are the two
 * shapes a provider emits that the loop must still handle deliberately: the first is
 * a no-argument tool and gets `{}`, the second names nothing the dispatcher could
 * call and is refused rather than dispatched empty (`02-firmware.md` §4.1 step 5). */
TEST_CASE("agent_parse_reply fills arguments and refuses a nameless call", "[m4]")
{
    static const char *no_args =
        "{\"choices\":[{\"message\":{\"content\":\"\",\"tool_calls\":["
        "{\"id\":\"c1\",\"function\":{\"name\":\"time.now\"}}]}}]}";
    agent_reply_t *reply = (agent_reply_t *)calloc(1, sizeof(agent_reply_t));
    TEST_ASSERT_NOT_NULL(reply);
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_parse_reply(no_args, strlen(no_args), reply));
    TEST_ASSERT_EQUAL_size_t(1, reply->call_count);
    TEST_ASSERT_EQUAL_STRING("{}", reply->calls[0].args);

    static const char *no_name =
        "{\"choices\":[{\"message\":{\"content\":\"\",\"tool_calls\":["
        "{\"id\":\"c2\",\"function\":{\"arguments\":\"{}\"}}]}}]}";
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG,
        agent_parse_reply(no_name, strlen(no_name), reply));
    free(reply);
}

/* --- skills --------------------------------------------------------------- */

static const char *SKILL_TEXT =
    "---\n"
    "name: blink\n"
    "description: Blink the onboard LED\n"
    "version: 1.2\n"
    "max_body_bytes: 512\n"
    "---\n"
    "Step one: configure the pin.\n";

/* `03-config.md` §skills: frontmatter is parsed and the body offset points past it. */
TEST_CASE("skills_parse reads frontmatter and the body offset", "[m4]")
{
    skill_meta_t meta;
    size_t offset = 0;
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        skills_parse("blink", SKILL_TEXT, strlen(SKILL_TEXT), &meta, &offset));
    TEST_ASSERT_TRUE(meta.valid);
    TEST_ASSERT_EQUAL_STRING("blink", meta.name);
    TEST_ASSERT_EQUAL_STRING("Blink the onboard LED", meta.description);
    TEST_ASSERT_EQUAL_STRING("1.2", meta.version);
    TEST_ASSERT_TRUE(meta.has_max_body);
    TEST_ASSERT_EQUAL_size_t(512, meta.max_body_bytes);
    TEST_ASSERT_EQUAL_STRING("Step one: configure the pin.\n", SKILL_TEXT + offset);
}

/* Validation rule 11: `name` must equal the file stem. A mismatch drops the file. */
TEST_CASE("skills_parse drops a name that does not match the stem", "[m4]")
{
    skill_meta_t meta;
    size_t offset = 0;
    TEST_ASSERT_NOT_EQUAL(ESP_OK,
        skills_parse("other", SKILL_TEXT, strlen(SKILL_TEXT), &meta, &offset));
    TEST_ASSERT_FALSE(meta.valid);
}

/* The name charset is `[a-z0-9_-]{1,32}`. */
TEST_CASE("skills_name_ok enforces the charset and length", "[m4]")
{
    TEST_ASSERT_TRUE(skills_name_ok("blink"));
    TEST_ASSERT_TRUE(skills_name_ok("a-b_c9"));
    TEST_ASSERT_FALSE(skills_name_ok("Blink"));
    TEST_ASSERT_FALSE(skills_name_ok("blink.md"));
    TEST_ASSERT_FALSE(skills_name_ok("../etc"));
    TEST_ASSERT_FALSE(skills_name_ok(""));
    TEST_ASSERT_FALSE(skills_name_ok("aaaaaaaaaabbbbbbbbbbccccccccccddddd"));
}

/* The index stops at the budget and reports what it dropped. */
TEST_CASE("skills_index drops to fit the budget", "[m4]")
{
    skill_meta_t skills[3];
    for (int i = 0; i < 3; i++) {
        memset(&skills[i], 0, sizeof skills[i]);
        snprintf(skills[i].name, sizeof skills[i].name, "skill%d", i);
        snprintf(skills[i].description, sizeof skills[i].description,
                 "description number %d", i);
        skills[i].valid = true;
    }

    char out[256];
    size_t written = 0, dropped = 0;
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        skills_index(skills, 3, out, sizeof out, sizeof out, &written, &dropped));
    TEST_ASSERT_EQUAL_size_t(0, dropped);
    TEST_ASSERT_NOT_NULL(strstr(out, "skill0"));
    TEST_ASSERT_NOT_NULL(strstr(out, "skill2"));

    /* A line is 31 bytes ("- skill0: description number 0\n"), so a budget of 31
     * fits exactly one and drops the rest rather than truncating a line. */
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        skills_index(skills, 3, out, sizeof out, 31, &written, &dropped));
    TEST_ASSERT_EQUAL_size_t(2, dropped);
    TEST_ASSERT_NOT_NULL(strstr(out, "skill0"));
    TEST_ASSERT_NULL(strstr(out, "skill2"));

    /* A budget that fits no line is still a reported drop, not a failure: an error
     * here would be silent, because the caller only logs the omission on ESP_OK. */
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        skills_index(skills, 3, out, sizeof out, 0, &written, &dropped));
    TEST_ASSERT_EQUAL_size_t(0, written);
    TEST_ASSERT_EQUAL_STRING("", out);
    TEST_ASSERT_EQUAL_size_t(3, dropped);
}

/* --- the pin allowlist (`01-hardware.md` §6, §6.1) ------------------------ */

/* A pin a tool may touch is a rule, so it is proven here rather than by a board. */
TEST_CASE("the pin allowlist admits header pins and nothing else", "[m4]")
{
    /* Every header GPIO the board brings out is readable and writable. */
    const int header[] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 43, 44};
    for (size_t i = 0; i < sizeof header / sizeof header[0]; i++) {
        TEST_ASSERT_TRUE(sys_pin_readable(header[i]));
        TEST_ASSERT_TRUE(sys_pin_writable(header[i]));
    }

    /* GPIO0 is the download strapping pin and GPIO21 the onboard LED (§6.1); neither
     * may be driven by a tool, and no pin outside the header is in the allowlist. */
    TEST_ASSERT_FALSE(sys_pin_readable(0));
    TEST_ASSERT_FALSE(sys_pin_writable(0));
    TEST_ASSERT_FALSE(sys_pin_readable(21));
    TEST_ASSERT_FALSE(sys_pin_writable(21));
    TEST_ASSERT_FALSE(sys_pin_readable(-1));
    TEST_ASSERT_FALSE(sys_pin_readable(10));
    TEST_ASSERT_FALSE(sys_pin_readable(48));

    /* ADC1 only: D0/D1/D3 are 1/2/4, and ADC2 is unavailable while the radio is up. */
    TEST_ASSERT_TRUE(sys_pin_adc(1));
    TEST_ASSERT_TRUE(sys_pin_adc(2));
    TEST_ASSERT_TRUE(sys_pin_adc(4));
    TEST_ASSERT_FALSE(sys_pin_adc(3));
    TEST_ASSERT_FALSE(sys_pin_adc(5));
    TEST_ASSERT_FALSE(sys_pin_adc(0));
}

/* --- the audit's fixed defects -------------------------------------------- */

/* `02-firmware.md` §4.1 step 4: the assistant/tool pair is atomic, so a trim may not
 * start the kept suffix at a `role:"tool"` message whose call was dropped.
 *
 * The trim drops from the *front*: with a bound of N on a history of C messages it
 * drops C - N, keeping the newest N. So the orphan case is the bound landing exactly
 * on a result message, which this two-message history makes reachable at N = 1:
 * dropping one message would keep only the `tool` reply, so the trim drops through
 * it and keeps nothing. An empty history is the safe answer — the provider would
 * reject the orphan instead. */
TEST_CASE("history_trim never leaves an orphan tool message", "[m4]")
{
    char buf[512];
    strcpy(buf,
           "[{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c1\"}]},"
           "{\"role\":\"tool\",\"content\":\"r\",\"tool_call_id\":\"c1\"}]");
    size_t used = strlen(buf);

    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_trim(buf, &used, 1));
    TEST_ASSERT_EQUAL_size_t(0, agent_history_count(buf));
    TEST_ASSERT_EQUAL_STRING("[]", buf);
    TEST_ASSERT_EQUAL_size_t(strlen(buf), used);

    /* Dropping the user turn leaves the pair whole; the bound is honoured here
     * because dropping one message does not split a call from its result. */
    strcpy(buf,
           "[{\"role\":\"user\",\"content\":\"q\"},"
           "{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c1\"}]},"
           "{\"role\":\"tool\",\"content\":\"r\",\"tool_call_id\":\"c1\"}]");
    used = strlen(buf);
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_trim(buf, &used, 2));
    TEST_ASSERT_EQUAL_size_t(2, agent_history_count(buf));
    TEST_ASSERT_NULL(strstr(buf, "\"user\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"tool\""));
    TEST_ASSERT_EQUAL_size_t(strlen(buf), used);
}

/* The same rule for a conversation that is all pairs: dropping one pair keeps the
 * next pair intact rather than keeping a bare result.
 *
 * Four messages, bound 3 would drop `A1` and keep `[T1, A2, T2]`, whose first member
 * is an orphan; the trim therefore drops through `T1` as well and keeps `[A2, T2]`.
 * Two messages is the largest suffix that starts on a call, so the bound is a ceiling
 * that pairing may lower by one. */
TEST_CASE("history_trim keeps a whole pair rather than half of one", "[m4]")
{
    char buf[512];
    strcpy(buf,
           "[{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c1\"}]},"
           "{\"role\":\"tool\",\"content\":\"r1\",\"tool_call_id\":\"c1\"},"
           "{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c2\"}]},"
           "{\"role\":\"tool\",\"content\":\"r2\",\"tool_call_id\":\"c2\"}]");
    size_t used = strlen(buf);

    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_trim(buf, &used, 3));
    TEST_ASSERT_EQUAL_size_t(2, agent_history_count(buf));
    TEST_ASSERT_EQUAL_INT(0, strncmp(buf, "[{\"role\":\"assistant\"", 18));
    TEST_ASSERT_NOT_NULL(strstr(buf, "c2"));
    TEST_ASSERT_NULL(strstr(buf, "r1"));
    TEST_ASSERT_EQUAL_size_t(strlen(buf), used);

    /* A bound that already lands on a call is honoured exactly. */
    strcpy(buf,
           "[{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c1\"}]},"
           "{\"role\":\"tool\",\"content\":\"r1\",\"tool_call_id\":\"c1\"},"
           "{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c2\"}]},"
           "{\"role\":\"tool\",\"content\":\"r2\",\"tool_call_id\":\"c2\"}]");
    used = strlen(buf);
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_trim(buf, &used, 2));
    TEST_ASSERT_EQUAL_size_t(2, agent_history_count(buf));
    TEST_ASSERT_EQUAL_INT(0, strncmp(buf, "[{\"role\":\"assistant\"", 18));
    TEST_ASSERT_NULL(strstr(buf, "r1"));
}

/* An aborted run must leave the conversation as it found it, so the next run does not
 * replay a question that never got an answer. */
TEST_CASE("history_truncate restores the depth a run started from", "[m4]")
{
    char buf[512];
    strcpy(buf,
           "[{\"role\":\"user\",\"content\":\"q1\"},"
           "{\"role\":\"assistant\",\"content\":\"a1\"},"
           "{\"role\":\"user\",\"content\":\"q2\"},"
           "{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c1\"}]},"
           "{\"role\":\"tool\",\"content\":\"r\",\"tool_call_id\":\"c1\"}]");
    size_t used = strlen(buf);

    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_truncate(buf, &used, 2));
    TEST_ASSERT_EQUAL_size_t(2, agent_history_count(buf));
    TEST_ASSERT_NOT_NULL(strstr(buf, "a1"));
    TEST_ASSERT_NULL(strstr(buf, "q2"));
    TEST_ASSERT_NULL(strstr(buf, "\"tool\""));
    TEST_ASSERT_EQUAL_size_t(strlen(buf), used);

    /* Keeping more than exists is a no-op, not an error. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_truncate(buf, &used, 99));
    TEST_ASSERT_EQUAL_size_t(2, agent_history_count(buf));
}

/* The other end of the same rule: a truncate that stops just after a `tool_calls`
 * turn leaves a call with no result, which a provider refuses. Cutting one message
 * earlier drops the call and its result together. */
TEST_CASE("history_truncate never leaves a call without its result", "[m4]")
{
    char buf[512];
    strcpy(buf,
           "[{\"role\":\"user\",\"content\":\"q\"},"
           "{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c1\"}]},"
           "{\"role\":\"tool\",\"content\":\"r\",\"tool_call_id\":\"c1\"},"
           "{\"role\":\"assistant\",\"content\":\"a\"}]");
    size_t used = strlen(buf);

    /* Four would keep the call and drop its result. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_truncate(buf, &used, 4));
    TEST_ASSERT_EQUAL_size_t(4, agent_history_count(buf));

    strcpy(buf,
           "[{\"role\":\"user\",\"content\":\"q\"},"
           "{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c1\"}]},"
           "{\"role\":\"tool\",\"content\":\"r\",\"tool_call_id\":\"c1\"},"
           "{\"role\":\"assistant\",\"content\":\"a\"}]");
    used = strlen(buf);
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_truncate(buf, &used, 3));
    TEST_ASSERT_EQUAL_size_t(3, agent_history_count(buf));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"tool\""));
    TEST_ASSERT_EQUAL_size_t(strlen(buf), used);

    /* Keeping only the call's own turn is refused too: two drops the call, not the
     * result alone. */
    strcpy(buf,
           "[{\"role\":\"user\",\"content\":\"q\"},"
           "{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c1\"}]},"
           "{\"role\":\"tool\",\"content\":\"r\",\"tool_call_id\":\"c1\"}]");
    used = strlen(buf);
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_truncate(buf, &used, 2));
    TEST_ASSERT_EQUAL_size_t(1, agent_history_count(buf));
    TEST_ASSERT_NULL(strstr(buf, "\"tool\""));
    TEST_ASSERT_NULL(strstr(buf, "tool_calls"));
}

/* --- the loop's own contract, proven through the assembly it produces --------- */

/* `02-firmware.md` §4.1 step 4: the run's prompt is the last message of the first
 * request and is in the history from round 2 on. `agent.c` is board-only, so the
 * rule is proven here by driving the two pure pieces it uses — `agent_history_append`
 * and `llm_assemble_prompt` — in the order `run_once` does. Before the fix the prompt
 * never entered the history, so round 1 of a tool loop sent history alone and the
 * provider saw tool results with no question at all. */
TEST_CASE("a tool round keeps the question in the request body", "[m4]")
{
    char history[1024];
    size_t used = 0;
    int32_t bound = 16;

    /* Round 0: the prompt is pushed, then the request carries history only. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"user\",\"content\":\"what time is it\"}", bound));

    llm_request_t first = {
        .model = "m",
        .history_json = history,
        .user_prompt = NULL,
    };
    char body[1024];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&first, NULL, body, sizeof body));
    TEST_ASSERT_NOT_NULL(strstr(body, "what time is it"));

    /* Round 1: the assistant turn and its result join the same history, and the
     * question is still there. This is the assertion the old code failed. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"c1\"}]}", bound));
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"tool\",\"tool_call_id\":\"c1\",\"content\":\"12:00\"}", bound));

    llm_request_t later = {.model = "m", .history_json = history, .user_prompt = NULL};
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&later, NULL, body, sizeof body));
    TEST_ASSERT_NOT_NULL(strstr(body, "what time is it"));
    TEST_ASSERT_NOT_NULL(strstr(body, "12:00"));
    /* The question precedes the tool result, which is the order a provider requires. */
    TEST_ASSERT_TRUE(strstr(body, "what time is it") < strstr(body, "12:00"));
}

/* §4.1 step 6: a finished run leaves its answer in the history, so the next run sees
 * a conversation rather than a question with no reply. */
TEST_CASE("a finished run leaves the question and its answer for the next turn", "[m4]")
{
    char history[1024];
    size_t used = 0;
    int32_t bound = 16;

    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"user\",\"content\":\"first question\"}", bound));
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"assistant\",\"content\":\"first answer\"}", bound));

    /* Run two pushes its own prompt onto the same buffer, which is what `run_once`
     * does before its first round. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"user\",\"content\":\"second question\"}", bound));

    llm_request_t r = {.model = "m", .history_json = history, .user_prompt = NULL};
    char body[1024];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, body, sizeof body));
    TEST_ASSERT_NOT_NULL(strstr(body, "first question"));
    TEST_ASSERT_NOT_NULL(strstr(body, "first answer"));
    TEST_ASSERT_NOT_NULL(strstr(body, "second question"));
    /* A multiturn request is a conversation in order, not the newest question alone. */
    TEST_ASSERT_TRUE(strstr(body, "first answer") < strstr(body, "second question"));
}

/* The reason the push is required, stated as the failure it prevents: a round whose
 * history has no user turn and whose `user_prompt` is NULL assembles a body with no
 * `"role":"user"` message at all. That was the shape `run_once` sent from round 1 on
 * before the fix, and a provider answers a tool-result-only conversation with a 400
 * or with an answer to the wrong question. */
TEST_CASE("a history without the prompt assembles no user message", "[m4]")
{
    llm_request_t r = {
        .model = "m",
        .user_prompt = NULL,
        .history_json = "[{\"role\":\"assistant\",\"content\":\"\","
                        "\"tool_calls\":[{\"id\":\"c1\"}]},"
                        "{\"role\":\"tool\",\"tool_call_id\":\"c1\",\"content\":\"{}\"}]",
    };
    char body[512];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, body, sizeof body));
    TEST_ASSERT_NULL(strstr(body, "\"role\":\"user\""));
    /* And with the prompt pushed into that same history, the turn is restored. */
    char history[512];
    size_t used = 0;
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"user\",\"content\":\"q\"}", 16));
    llm_request_t fixed = {.model = "m", .history_json = history};
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&fixed, NULL, body, sizeof body));
    TEST_ASSERT_NOT_NULL(strstr(body, "\"role\":\"user\""));
}

/* Capacity is a property of the shipped constants, so it is asserted rather than
 * measured on a board: the body has to hold the tool array and an 8 KiB system prompt
 * at once, or every request is refused before any provider is reached. The board
 * showed the consequence — `request body too large` on every prompt, permanently,
 * because history is never reset. */
TEST_CASE("a request body can hold the tools and a full system prompt", "[m4]")
{
    TEST_ASSERT_EQUAL_INT(NET_BODY_MAX, LLM_BODY_MAX);
    /* `agent.c`'s history cap and `03-config.md`'s input bound; both are pinned here
     * because a body that cannot hold them is a permanently failing device. */
    TEST_ASSERT_GREATER_THAN(8192 + TOOL_ARRAY_BUDGET, LLM_BODY_MAX);
    /* The system prompt is the largest single part, and it is configurable up to the
     * `03-config.md` input bound. */
    llm_request_t r = {.model = "m"};
    static char prompt[8193];
    static char tools[TOOL_ARRAY_BUDGET + 1];
    memset(prompt, 's', 8192);
    prompt[8192] = '\0';
    memset(tools, ' ', TOOL_ARRAY_BUDGET);
    tools[0] = '[';
    tools[TOOL_ARRAY_BUDGET - 1] = ']';
    tools[TOOL_ARRAY_BUDGET] = '\0';

    r.system_prompt = prompt;
    r.tools_json = tools;
    static char body[LLM_BODY_MAX];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, body, sizeof body));
}

/* `tools/todo.md` §Bounds: a refusal leaves `list unchanged`. The nested `init`
 * validated phase count and total up front but then cleared the list before the
 * per-task insert, so an entry failing `task_add` — a duplicate content string, or
 * one over 120 characters — wiped a list the op was supposed to refuse whole. */
TEST_CASE("todo init refuses a bad nested entry without touching the list", "[m4]")
{
    todo_list_t *list = agent_todo_phases();
    todo_list_clear(list);
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        todo_op(list, "{\"op\":\"init\",\"items\":[\"keep me\"]}"));
    TEST_ASSERT_EQUAL_size_t(1, todo_task_total(list));

    /* A duplicate content string: `task_add` refuses the second copy, after `init`
     * has already cleared the list. */
    TEST_ASSERT_NOT_EQUAL(ESP_OK, todo_op(list,
        "{\"op\":\"init\",\"list\":[{\"phase\":\"P\",\"items\":[\"dup\",\"dup\"]}]}"));
    TEST_ASSERT_EQUAL_size_t(1, todo_task_total(list));
    TEST_ASSERT_EQUAL_STRING("keep me", list->tasks[0].content);
    TEST_ASSERT_EQUAL_STRING("Tasks", list->phases[0]);
}

/* Defect G, stated as the rule it broke. When `llm.max_history_messages` is already
 * reached, the append evicts the oldest entry and the count returns to the bound, so
 * comparing counts before and after said "the prompt is not in history" and the
 * request carried it a second time in `user_prompt`. The count-based predicate is
 * wrong on every full history; the append's own result is the answer. */
TEST_CASE("a full history does not send the prompt twice", "[m4]")
{
    char history[512];
    size_t used = 0;
    int32_t bound = 2;

    /* Fill to the bound, then push the run's prompt: the oldest is evicted, the count
     * stays 2, and the prompt is nonetheless the last message. */
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"user\",\"content\":\"older\"}", bound));
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"assistant\",\"content\":\"older answer\"}", bound));
    size_t floor = agent_history_count(history);
    TEST_ASSERT_EQUAL_INT(ESP_OK, agent_history_append(history, sizeof history, &used,
        "{\"role\":\"user\",\"content\":\"newest\"}", bound));

    /* The count is back at the bound, so a `count > floor` test says false here even
     * though the prompt is the last message — the predicate the fix replaced. */
    TEST_ASSERT_EQUAL_size_t(floor, agent_history_count(history));
    TEST_ASSERT_NOT_NULL(strstr(history, "newest"));

    /* And the request assembled from it carries the question once, from the history. */
    llm_request_t r = {.model = "m", .history_json = history, .user_prompt = NULL};
    char body[512];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, body, sizeof body));
    const char *hit = strstr(body, "newest");
    TEST_ASSERT_NOT_NULL(hit);
    TEST_ASSERT_NULL(strstr(hit + 1, "\"role\":\"user\""));
}

/* Negative cases the parser has to get right, because each one would otherwise reach
 * the model as a skill it cannot follow. */
TEST_CASE("skills_parse refuses every shape that is not a skill", "[m4]")
{
    skill_meta_t meta;
    size_t offset = 0;

    /* No frontmatter at all: a plain Markdown file is not a skill. */
    const char *plain = "# Just notes\n\nNothing declarative here.\n";
    TEST_ASSERT_NOT_EQUAL(ESP_OK,
        skills_parse("plain", plain, strlen(plain), &meta, &offset));

    /* Frontmatter never closed: the parse runs to the end and fails rather than
     * treating the whole file as the header. */
    const char *unclosed = "---\nname: unclosed\ndescription: no close\n";
    TEST_ASSERT_NOT_EQUAL(ESP_OK,
        skills_parse("unclosed", unclosed, strlen(unclosed), &meta, &offset));

    /* A description is required, and an empty one does not count. */
    const char *no_desc = "---\nname: nodesc\n---\nbody\n";
    TEST_ASSERT_NOT_EQUAL(ESP_OK,
        skills_parse("nodesc", no_desc, strlen(no_desc), &meta, &offset));

    /* The name charset is enforced on the parsed value too, not only on the stem. */
    const char *bad_name = "---\nname: Bad Name\ndescription: d\n---\nbody\n";
    TEST_ASSERT_NOT_EQUAL(ESP_OK,
        skills_parse("Bad Name", bad_name, strlen(bad_name), &meta, &offset));

    /* A zero or negative `max_body_bytes` is ignored, leaving the default, rather
     * than making every body of a real skill unreadable. */
    const char *zero = "---\nname: zero\ndescription: d\nmax_body_bytes: 0\n---\nbody\n";
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        skills_parse("zero", zero, strlen(zero), &meta, &offset));
    TEST_ASSERT_FALSE(meta.has_max_body);
    TEST_ASSERT_EQUAL_size_t(SKILL_BODY_DEFAULT_MAX, meta.max_body_bytes);

    const char *neg = "---\nname: neg\ndescription: d\nmax_body_bytes: -5\n---\nb\n";
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        skills_parse("neg", neg, strlen(neg), &meta, &offset));
    TEST_ASSERT_EQUAL_size_t(SKILL_BODY_DEFAULT_MAX, meta.max_body_bytes);
}

/* An invalid skill never reaches the index, so a dropped file cannot occupy a slot
 * and cannot make the model believe a skill exists. */
TEST_CASE("the index carries only valid skills", "[m4]")
{
    skill_meta_t skills[2];
    memset(skills, 0, sizeof skills);
    snprintf(skills[0].name, sizeof skills[0].name, "good");
    snprintf(skills[0].description, sizeof skills[0].description, "a real one");
    skills[0].valid = true;
    snprintf(skills[1].name, sizeof skills[1].name, "bad");
    snprintf(skills[1].description, sizeof skills[1].description, "dropped");
    skills[1].valid = false;

    char out[128];
    size_t written = 0, dropped = 0;
    TEST_ASSERT_EQUAL_INT(ESP_OK,
        skills_index(skills, 2, out, sizeof out, sizeof out, &written, &dropped));
    TEST_ASSERT_NOT_NULL(strstr(out, "good"));
    TEST_ASSERT_NULL(strstr(out, "bad"));
    /* The dropped count counts valid skills that did not fit, so a file the parser
     * already rejected is not reported as a budget drop. */
    TEST_ASSERT_EQUAL_size_t(0, dropped);
}

/* The registry's description is what the model reads, so it must not claim a
 * confinement the tool does not have. `06` §6.1 makes both mounts listable and
 * readable; only the write pair is confined to `/workspace/`. */
TEST_CASE("tool descriptions match the surface the tools have", "[m4]")
{
    TEST_ASSERT_NOT_NULL(strstr(tools_find("fs.list")->description, "read-only image"));
    TEST_ASSERT_NOT_NULL(strstr(tools_find("fs.read")->description, "read-only image"));
    TEST_ASSERT_NULL(strstr(tools_find("fs.write")->description, "read-only image"));
    TEST_ASSERT_NOT_NULL(strstr(tools_find("fs.write")->description, "/workspace/"));
}
