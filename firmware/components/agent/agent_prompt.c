/* agent_prompt — the rejection a `prompt` frame gets when the loop will not take it.
 *
 * `04-protocol.md` §3.3 fixes the three codes a rejected prompt may carry and §4
 * fixes "missing required arguments are `E_PROTOCOL`". Keeping the table here rather
 * than in `main.c` is what makes both host-testable: the frame handler mapped every
 * unrecognised `esp_err_t` to `E_INTERNAL`, so a prompt with no `text` answered
 * "the agent loop is not started" with `retryable:true` — a malformed frame told the
 * bridge to retry it. Pure. */

#include "agent.h"

const agent_rejection_t *agent_prompt_rejection(esp_err_t err)
{
    static const agent_rejection_t rejected[] = {
        /* `04` §4: a required argument that is absent is a malformed frame. */
        {ESP_ERR_INVALID_ARG, "E_PROTOCOL", "prompt.text is required", false, false},
        /* §3.3: the third concurrent prompt. `retryable` because a run ends. */
        {ESP_ERR_INVALID_STATE, "E_BUSY",
         "agent run in flight; queue full (2), prompt rejected", true, true},
        /* §3.3: longer than `agent.max_input_bytes`, and not a tool failure. */
        {ESP_ERR_INVALID_SIZE, "E_PROTOCOL",
         "prompt exceeds agent.max_input_bytes", false, false},
        /* The queue or the task could not be created. `retryable` follows the code,
         * not the situation: `04` §5 fixes it per code, and `E_INTERNAL` is `yes`. */
        {ESP_ERR_NO_MEM, "E_INTERNAL", "the agent loop cannot allocate", true, false},
        /* `agent_submit`'s "no loop" code; see `agent.h`. */
        {ESP_ERR_NOT_FOUND, "E_INTERNAL", "the agent loop is not started", true, false},
    };

    for (size_t i = 0; i < sizeof rejected / sizeof rejected[0]; i++) {
        if (rejected[i].err == err) return &rejected[i];
    }
    /* A code the table does not name is a fault here, not a client error. §5 lets
     * `E_INTERNAL` answer any type, so it is a valid answer rather than a drop. */
    static const agent_rejection_t unknown = {0, "E_INTERNAL", "the prompt was refused",
                                              true, false};
    return &unknown;
}
