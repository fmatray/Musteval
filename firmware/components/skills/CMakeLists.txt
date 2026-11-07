# `skills_index.c` is pure — frontmatter parse and index assembly — so `test_host`
# covers it. `skills.c` reads LittleFS and is board-only, like `tools.c`.
set(srcs "skills_index.c")

if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "skills.c")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "include"
    REQUIRES store
    PRIV_REQUIRES esp_common
)
