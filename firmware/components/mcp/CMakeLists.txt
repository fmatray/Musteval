# `mcp_parse.c` is pure — the JSON-RPC envelopes, the tools projection and the
# namespace rules — so `test_host` covers it. `mcp.c` opens the TLS session, which a
# stub would only pretend to test (`AGENTS.md`, coverage gates).
set(srcs "mcp_parse.c")

set(reqs "store" "net" "cfg")

if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "mcp.c")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "include"
    REQUIRES ${reqs}
)
