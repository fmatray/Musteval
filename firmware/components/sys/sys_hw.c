/* Board half of `sys`: GPIO, ADC and the task watchdog (`02-firmware.md` §sys).
 * Compiled only for a chip target — the `linux` host target has no GPIO, and a
 * stub here would be a test of the stub (`AGENTS.md`, coverage gates). */

#include "driver/gpio.h"
#include "esp_task_wdt.h"

#include "sys.h"

#define SYS_WDT_TIMEOUT_S 5

/* `01-hardware.md` §6: onboard user LED, active-low, not on the header. */
#define SYS_LED_PIN GPIO_NUM_21
#define SYS_LED_LEVEL_ON  0
#define SYS_LED_LEVEL_OFF 1

esp_err_t sys_board_init(void)
{
    /* The ISR service is installed once here; `sched` adds its own handler after
     * `sys_gpio_edge_config()` so `sys` stays trigger-agnostic (`02` §sys). */
    esp_err_t err = gpio_install_isr_service(0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        /* INVALID_STATE means another caller already installed it, which is the
         * idempotent case: `sys_board_init` is safe to call twice. */
        return err;
    }

    /* The watchdog goes in before the LED: it is the safety net for a wedged boot,
     * and a pin that cannot be configured must not be able to leave the device
     * without one. `02-firmware.md` §3.1 step 2 asks for both, not either.
     *
     * The idle tasks are the subscribers: nothing calls esp_task_wdt_add() yet, so a
     * mask of 0 would arm a watchdog that watches nobody. IDF's own init used to
     * subscribe them, and disabling it (sdkconfig.defaults) moved that duty here. */
    esp_task_wdt_config_t wdt = {
        .timeout_ms = SYS_WDT_TIMEOUT_S * 1000,
        .idle_core_mask = (1U << CONFIG_FREERTOS_NUMBER_OF_CORES) - 1U,
        .trigger_panic = true,
    };
    err = esp_task_wdt_init(&wdt);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    /* LED starts dark: a lit LED during a failed boot would be indistinguishable
     * from the "running" state it is meant to signal. */
    gpio_config_t led = {
        .pin_bit_mask = 1ULL << SYS_LED_PIN,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    err = gpio_config(&led);
    if (err == ESP_OK) {
        err = sys_led_set(false);
    }
    return err;
}

esp_err_t sys_led_set(bool on)
{
    return gpio_set_level(SYS_LED_PIN, on ? SYS_LED_LEVEL_ON : SYS_LED_LEVEL_OFF);
}
