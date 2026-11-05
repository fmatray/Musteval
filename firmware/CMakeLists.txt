cmake_minimum_required(VERSION 3.16)

# The board project's own components live in `components/`; ESP-IDF picks that
# directory up automatically. `test_host/` is a separate project, so its tests are
# not part of this build.
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(musteval VERSION 1.0.0)
