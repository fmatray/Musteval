# `store` is the only JSON and path owner (`08-code-architecture.md` §3), so `main`
# reaches the boot counter through it rather than calling nvs_flash itself.
idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_psram spi_flash store sys link cfg net clock llm
                       PRIV_REQUIRES esp_timer heap)
