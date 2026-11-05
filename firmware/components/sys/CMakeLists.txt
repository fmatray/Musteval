# `sys_policy.c` is pure and must build on the host target so `test_host` can cover
# the boot-guard arithmetic (`AGENTS.md`, firmware host row). The hardware half is
# board-only: there is no GPIO or watchdog on the `linux` target.
set(srcs "sys_policy.c")

if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "sys_hw.c")
endif()

# `esp_driver_gpio` provides driver/gpio.h, `esp_system` provides esp_task_wdt.h.
idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES esp_driver_gpio esp_system)
