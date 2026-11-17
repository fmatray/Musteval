/* agent_settings — the run settings the loop uses, resolved from config.
 *
 * Lived inline in `load_run_settings` in board-only `agent.c`, where no test could
 * reach it, and drifted from `03-config.md`: the documented default for
 * `agent.max_input_bytes` is 3072 and the code applied 8192, so a prompt longer than
 * the documented bound was accepted on the board. The rule that a documented default
 * is what the code applies is only enforceable if the resolution is host-testable.
 * Pure. */

#include "agent.h"

void agent_run_settings_resolve(const agent_raw_settings_t *raw,
                                agent_run_settings_t *out)
{
    if (raw == NULL || out == NULL) return;

    out->max_tool_rounds = raw->has_max_tool_rounds
                               ? raw->max_tool_rounds
                               : AGENT_DEFAULT_MAX_TOOL_ROUNDS;
    out->max_history_messages = raw->has_max_history
                                    ? raw->max_history
                                    : AGENT_DEFAULT_MAX_HISTORY;

    int32_t input = raw->has_max_input ? raw->max_input : AGENT_DEFAULT_MAX_INPUT_BYTES;
    if (input < 0) input = 0;
    /* The buffer is the hard ceiling: a configured value above it cannot be honoured,
     * and silently writing past it is worse than clamping. */
    if (input > AGENT_INPUT_BUFFER_MAX) input = AGENT_INPUT_BUFFER_MAX;
    out->max_input_bytes = (size_t)input;

    /* The validator bounds this, but the resolver is what a run is held to, and a
     * config the validator never saw (a default, or a value from before the key
     * existed) must land somewhere sane rather than disabling the check by being
     * out of range. Clamped to the endpoints, not snapped to the step: a value the
     * validator accepted is already on the step, and rounding one it did not would
     * hide the drift instead of failing it. */
    int32_t pct = raw->has_autocompact ? raw->autocompact_pct : AGENT_DEFAULT_AUTOCOMPACT;
    if (pct < AGENT_AUTOCOMPACT_MIN) pct = AGENT_AUTOCOMPACT_MIN;
    if (pct > AGENT_AUTOCOMPACT_MAX) pct = AGENT_AUTOCOMPACT_MAX;
    out->autocompact_pct = pct;
}

bool agent_autocompact_due(size_t used, size_t cap, int32_t pct)
{
    if (cap == 0) return false;
    if (pct >= AGENT_AUTOCOMPACT_MAX) return false;
    if (pct < AGENT_AUTOCOMPACT_MIN) pct = AGENT_AUTOCOMPACT_MIN;
    size_t threshold = (size_t)(((uint64_t)cap * (uint64_t)pct + 99u) / 100u);
    return used >= threshold;
}
