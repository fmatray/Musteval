# `sys_policy.c` is pure and must build on the host target so `test_host` can cover
# the boot-guard arithmetic (`AGENTS.md`, firmware host row). The hardware half is
# board-only: there is no GPIO or watchdog on the `linux` target.
set(srcs "sys_policy.c")

if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "sys_hw.c")
endif()

# The driver requirements exist only where `sys_hw.c` does: `esp_adc` and
# `esp_driver_i2c` have no `linux` build, and naming them there fails configure.
if(IDF_TARGET STREQUAL "linux")
    idf_component_register(SRCS ${srcs} INCLUDE_DIRS "include")
else()
    # `esp_driver_gpio` provides driver/gpio.h, `esp_system` esp_task_wdt.h, `esp_adc`
    # the oneshot unit and `esp_driver_i2c` the master bus.
    idf_component_register(SRCS ${srcs}
                           INCLUDE_DIRS "include"
                           PRIV_REQUIRES esp_driver_gpio esp_system esp_adc
                                        esp_driver_i2c)
endif()
