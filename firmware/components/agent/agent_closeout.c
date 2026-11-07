/* agent_closeout — the two decisions of `02-firmware.md` §4.1 step 5.
 * Pure: the loop control is a rule, and a rule that decides whether a run may end
 * belongs on the host where it can be failed, not on a board. */

#include "agent.h"

/* Frozen prose, one owner. The loop appends this string and the doc quotes it, so
 * the two cannot drift. */
static const char CLOSEOUT_OPEN_TEXT[] =
    "Close out your todo list before answering. Every task must end completed, or "
    "abandoned via drop if you are deliberately not doing it. No task may be left "
    "pending, in_progress or blocked.";

static const char CLOSEOUT_UNTRACKED_TEXT[] =
    "This run took several steps but opened no todo list. Open one with init and "
    "record what you did and what remains, then close it out.";

closeout_verdict_t agent_closeout_check(const todo_list_t *list, int tool_rounds,
                                        bool ever_initialised)
{
    for (size_t i = 0; list != NULL && i < list->task_count; i++) {
        todo_status_t s = list->tasks[i].status;
        if (s == TODO_PENDING || s == TODO_IN_PROGRESS || s == TODO_BLOCKED) {
            return CLOSEOUT_OPEN_TASKS;
        }
    }

    /* Empty tree plus two or more rounds of real work: the model acted without
     * tracking. A one-round question is compliant (`02-firmware.md` §4.1). */
    if ((list == NULL || list->task_count == 0) && !ever_initialised &&
        tool_rounds >= 2) {
        return CLOSEOUT_UNTRACKED;
    }
    return CLOSEOUT_OK;
}

const char *agent_closeout_prompt(closeout_verdict_t verdict)
{
    switch (verdict) {
    case CLOSEOUT_OPEN_TASKS: return CLOSEOUT_OPEN_TEXT;
    case CLOSEOUT_UNTRACKED:  return CLOSEOUT_UNTRACKED_TEXT;
    case CLOSEOUT_OK:         break;
    }
    return NULL;
}

bool agent_todo_open_flag(closeout_verdict_t verdict, bool budget_spent)
{
    /* `04` §3.14: true only when the check still failed as the budget ran out. A
     * verdict that never failed cannot make the flag true, however the run ended. */
    return budget_spent && verdict != CLOSEOUT_OK;
}
