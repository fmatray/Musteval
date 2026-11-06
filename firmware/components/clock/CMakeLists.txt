# `clock` is L0 and may call `net`, `cfg` and `store` (`08-code-architecture.md`
# §1.1's enumerated edges). `clock_pure.c` holds the arithmetic and builds for the
# host; `clock.c` needs a radio and the system clock, so it is board-only, exactly as
# `cfg.c` is beside `cfg_doc.c`.
set(srcs "clock_pure.c")
if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "clock.c")
endif()
idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "include"
                       REQUIRES cfg esp_timer)
