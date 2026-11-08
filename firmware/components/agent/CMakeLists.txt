# The rule half of the agent — the todo tree, the close-out verdicts, the history
# bound, the reply parse and a rejected prompt's code — is pure and host-tested.
# `agent.c` owns the task, the queue and the frame emitters, so it is board-only like
# `tools.c`.
set(srcs "agent_todo.c" "agent_closeout.c" "agent_history.c" "agent_reply.c"
        "agent_prompt.c")

if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "agent.c")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "include"
    REQUIRES store cfg llm tools skills link sys clock
    PRIV_REQUIRES esp_timer
)
