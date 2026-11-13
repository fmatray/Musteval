# `sched_parse.c` is pure — the closed task schema, the trigger matchers, the due-time
# arithmetic and the template renderer — so `test_host` covers it. `sched.c` owns NVS,
# `esp_timer`, the GPIO ISR and the two ports `main` installs; a stub of those would
# only be a test of the stub (`AGENTS.md`, coverage gates).
set(srcs "sched_parse.c")

set(reqs "store" "clock" "sys" "link")

if(NOT IDF_TARGET STREQUAL "linux")
    # The board half owns the GPIO driver, `esp_timer` and NVS writes.
    list(APPEND srcs "sched.c")
    list(APPEND reqs "esp_driver_gpio" "esp_timer")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "include"
    REQUIRES ${reqs}
)
