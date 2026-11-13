# `geo_parse.c` is pure — provider-response parsing, the scan payload and the cache
# rule — so `test_host` covers it. `geo.c` opens sockets, scans the radio and reads
# NVS, which a stub would only pretend to test (`AGENTS.md`, coverage gates).
set(srcs "geo_parse.c")

set(reqs "store" "clock" "net" "cfg")

if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "geo.c")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "include"
    REQUIRES ${reqs}
)
