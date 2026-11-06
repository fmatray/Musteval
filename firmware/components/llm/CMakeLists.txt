# `llm` is L1, above `net`, `cfg`, `clock` and `store` (`08-code-architecture.md` §1).
# `llm_budget.c` and `llm_body.c` are pure and build for the host, where the window
# and body rules are proven; `llm.c` reads NVS and opens sockets, so it is board-only.
set(srcs "llm_budget.c" "llm_body.c")
if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "llm.c")
endif()
idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "include"
                       REQUIRES net cfg clock store)
