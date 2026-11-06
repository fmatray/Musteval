# `net` is L0 and reads WiFi credentials through `cfg` (`08-code-architecture.md`
# §1.1). `net_select.c` is the pure ordering and backoff arithmetic and builds for
# the host; `net.c` needs the radio and the HTTP client and is board-only.
#
# The radio and TLS requirements are conditional for the same reason as the source:
# `esp_wifi` and `esp_http_client` do not exist on the `linux` target, and a REQUIRES
# that names an absent component fails configuration before any test can run.
set(srcs "net_select.c")
set(reqs cfg)
if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "net.c")
    list(APPEND reqs esp_wifi esp_netif esp_event esp_http_client esp-tls mbedtls
                esp_timer)
endif()
idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "include"
                       REQUIRES ${reqs})
