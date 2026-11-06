# `cfg` is L0 and depends only on `store` (`08-code-architecture.md` §1, §2). No
# hardware API is used, so the same sources build for the host test target and
# every rule in it is covered without a board.
idf_component_register(SRCS "cfg.c" "cfg_validate.c"
                       INCLUDE_DIRS "include"
                       REQUIRES store)
