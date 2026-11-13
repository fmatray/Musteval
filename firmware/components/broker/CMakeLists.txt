# `mqtt_parse.c` is pure — the allowlist question, the port and client-id defaults and
# the payload cap — so `test_host` covers it. `mqtt.c` opens TLS connections and owns
# `esp_mqtt_client`, which a stub would only pretend to test (`AGENTS.md`).
set(srcs "mqtt_parse.c")

set(reqs "store" "cfg" "clock")

if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "mqtt.c")
    # This component is `broker` because ESP-IDF's client component is already named
    # `mqtt` and a local component of that name shadows it, leaving `mqtt_client.h` off
    # the include path. The public header stays `mqtt.h`: the module is `mqtt` in every
    # contract, only its directory differs (`08-code-architecture.md` §2 names the
    # layout `components/<name>/`, so this is the one place the name cannot follow).
    list(APPEND reqs "mqtt" "esp-tls" "esp_event" "tcp_transport")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "include"
    REQUIRES ${reqs}
)
