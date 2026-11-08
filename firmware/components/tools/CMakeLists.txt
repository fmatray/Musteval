# `tools_spec.c`, `tools_gate.c` and `tools_declared.c` are pure: registry, schemas,
# validation, the gate decisions and the `/tools/*.json` loader, so `test_host`
# covers them (`AGENTS.md`, firmware host row).
set(srcs "tools_spec.c" "tools_gate.c" "tools_declared.c")

# `tools.c` touches GPIO, ADC, I2C, LittleFS, NVS and the network: board-only. A
# stub here would be a test of the stub, not of the code that ships.
if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "tools.c")
endif()

if(IDF_TARGET STREQUAL "linux")
    # The driver requirements belong to `tools.c`, which is not built here, and
    # `esp_adc`/`esp_driver_i2c` have no `linux` target at all.
    idf_component_register(SRCS ${srcs}
                           INCLUDE_DIRS "include"
                           REQUIRES store
                           PRIV_REQUIRES cfg clock net skills sys esp_timer)
else()
    idf_component_register(SRCS ${srcs}
                           INCLUDE_DIRS "include"
                           REQUIRES store
                           PRIV_REQUIRES cfg clock net skills sys esp_timer
                                        esp_driver_gpio esp_adc esp_driver_i2c)
endif()
