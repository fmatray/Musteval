# `tools_spec.c`, `tools_gate.c` and `tools_declared.c` are pure: registry, schemas,
# validation, the gate decisions, and a `/tools/*.json` file's compile, bind and find,
# so `test_host` covers them (`AGENTS.md`, firmware host row).
set(srcs "tools_spec.c" "tools_gate.c" "tools_declared.c" "tools_json.c")

# `tools.c` touches GPIO, ADC, I2C, LittleFS, NVS and the network: board-only. A
# stub here would be a test of the stub, not of the code that ships.
# `tools_declared_io.c` is the loader and the caller: `store_read_file`,
# `cfg_resolve_secret` and `net_request` only exist on a board, and a host test of
# them proves the stub.
if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "tools.c" "tools_declared_io.c")
endif()

if(IDF_TARGET STREQUAL "linux")
    # The driver requirements belong to `tools.c`, which is not built here, and
    # `esp_adc`/`esp_driver_i2c` have no `linux` target at all.
    idf_component_register(SRCS ${srcs}
                           INCLUDE_DIRS "include"
                           REQUIRES store sched mcp
                           PRIV_REQUIRES cfg clock net skills sys esp_timer)
else()
    idf_component_register(SRCS ${srcs}
                           INCLUDE_DIRS "include"
                           REQUIRES store sched geo broker mcp
                           PRIV_REQUIRES cfg clock net skills sys esp_timer
                                        esp_driver_gpio esp_adc esp_driver_i2c llm)
    # `geo`, `mqtt` and `mcp` are required rather than private because `tools.h` names
    # their types: `tools_context_t` carries the discovered MCP tools.
endif()
