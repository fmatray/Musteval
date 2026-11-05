# `link` is L2 and reaches JSON only through `store`'s wrapper (`08` §3), never
# cJSON. `link_log.c` is pure so the level filter and the `log` frame shape are
# provable on the host; only the vprintf install in `link.c` is board-bound.
set(link_srcs "link.c" "link_log.c")
set(link_reqs "store" "json")
if(NOT IDF_TARGET STREQUAL "linux")
    # `link_task` owns the USB-Serial/JTAG driver, which has no `linux` counterpart.
    list(APPEND link_srcs "link_task.c")
    list(APPEND link_reqs "esp_driver_usb_serial_jtag")
endif()

idf_component_register(SRCS ${link_srcs} INCLUDE_DIRS "include"
                       REQUIRES ${link_reqs})
