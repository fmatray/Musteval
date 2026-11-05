# Host test app: builds and runs on the `linux` target so pure logic is tested with
# no board (`AGENTS.md` Test Strategy). Target is set by the caller:
#   idf.py --preview set-target linux
cmake_minimum_required(VERSION 3.16)

# ESP-IDF's host port needs libbsd headers, which this machine does not ship
# (`components/linux/include/sys/cdefs.h` includes `<bsd/sys/cdefs.h>`). The
# headers are vendored under IDF_TOOLS_PATH by `setup.sh` next to this file, so the
# suite does not depend on a root install of libbsd-dev.
set(_libbsd "$ENV{IDF_TOOLS_PATH}")
if(NOT _libbsd)
    set(_libbsd "$ENV{HOME}/.espressif")
endif()
if(EXISTS "${_libbsd}/libbsd_include/aarch64-linux-gnu/bsd/sys/cdefs.h")
    include_directories("${_libbsd}/libbsd_include/aarch64-linux-gnu")
    link_directories("${_libbsd}/libbsd_lib")
endif()

# Only `main` and its real dependencies: without this IDF builds every component,
# and `esp_phy` drags `nvs_flash` in, which the host suite has no use for.
# `AGENTS.md` gates firmware host-testable modules at 80% line, and a gate that no
# command enforces is a suggestion. `-DCOVERAGE=ON` instruments the components and
# the suite, so `gcovr` reads the run rather than the source.
option(COVERAGE "Instrument the host suite for gcovr" OFF)
if(COVERAGE AND NOT CMAKE_SCRIPT_MODE_FILE)
    add_compile_options(-fprofile-arcs -ftest-coverage)
    add_link_options(--coverage)
endif()

set(COMPONENTS main)
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(musteval_test_host)
