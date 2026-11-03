#include "sdkconfig.h"

#include "esp_flash.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_psram.h"
#include "inttypes.h"

static const char *TAG = "musteval";

void app_main(void)
{
    ESP_LOGI(TAG, "esp-idf %s, target %s", esp_get_idf_version(), CONFIG_IDF_TARGET);

    size_t psram_size = esp_psram_get_size();
    if (psram_size == 0) {
        ESP_LOGE(TAG, "psram: not detected, check CONFIG_SPIRAM_MODE_OCT");
    } else {
        ESP_LOGI(TAG, "psram: %" PRIu32 " bytes", (uint32_t)psram_size);
    }

    uint32_t flash_size = 0;
    esp_err_t err = esp_flash_get_size(NULL, &flash_size);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "flash: size query failed: %s", esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "flash: %" PRIu32 " bytes", flash_size);
    }
}
