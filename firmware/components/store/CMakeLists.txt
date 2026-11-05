# `store_json.c`, `store_path.c` and `store_quota.c` are pure and must build on the
# host target so `test_host` can cover canonical emit, path confinement, quota and
# chunk arithmetic. `store.c` is the board half: NVS and the two LittleFS mounts are
# board-bound storage that a stub would only pretend to test (`AGENTS.md`, coverage
# gates).
set(srcs "store_json.c" "store_path.c" "store_quota.c" "store_sandbox.c")
set(reqs "json")

if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "store.c")
    list(APPEND reqs "nvs_flash" "joltwallet__littlefs")
endif()
idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "include"
                       REQUIRES ${reqs} "mbedtls")
