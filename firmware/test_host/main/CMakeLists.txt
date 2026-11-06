set(SRCS "test_main.c" "test_smoke.c" "test_store.c" "test_link.c" "test_sys.c"
         "test_frame.c" "test_quota.c" "test_log.c" "test_sandbox.c" "test_cfg.c")

# WHOLE_ARCHIVE is required, not cosmetic: TEST_CASE registers itself from a
# constructor in a translation unit nothing references, so a normal link drops
# the object and the suite silently reports `0 Tests`.
idf_component_register(SRCS ${SRCS}
                       INCLUDE_DIRS "."
                       REQUIRES unity store link sys cfg
                       WHOLE_ARCHIVE)

# IDF includes this file a second time in CMake *script* mode to collect
# requirements, where target commands do not exist. Guard on that mode.
if(NOT CMAKE_SCRIPT_MODE_FILE)
    # The §3 corpus test reads the real doc, so a protocol edit that breaks the
    # codec fails here (`AGENTS.md` non-regression rule 3). Absolute, because the
    # test binary runs from `build/`.
    target_compile_definitions(${COMPONENT_LIB} PRIVATE
        PROTOCOL_DOC_PATH="${CMAKE_CURRENT_LIST_DIR}/../../../docs/04-protocol.md")
endif()
