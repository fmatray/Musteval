/* Host tests for the pure M4 rules: the nine `todo` ops and their normalization, the
 * close-out verdicts, schema validation and the tool gate, the history bound, the
 * provider-reply parse, and the skills frontmatter. Each asserts an observable
 * result of a rule a frozen document states (`tools/todo.md`, `02-firmware.md` §4.1,
 * `06-security.md` §6, `03-config.md` §skills), not that a function was called. */

#include <string.h>

#include "unity.h"

#include "agent.h"
#include "skills.h"
#include "store.h"
#include "sys.h"
#include "tools.h"

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
