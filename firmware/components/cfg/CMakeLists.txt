# `cfg` is L0 and depends only on `store` (`08-code-architecture.md` §1, §2).
# `cfg_doc.c` and `cfg_validate.c` are pure, so they build for the host target and
# every rule in them is covered without a board. `cfg.c` reads `/config.json` and
# opens NVS, which a stub would only pretend to test (`AGENTS.md` coverage gates),
# so it stays on the board exactly like `store.c`.
set(srcs "cfg_doc.c" "cfg_validate.c")
if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND srcs "cfg.c")
endif()
idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "include"
                       REQUIRES store)
