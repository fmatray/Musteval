/* Board half of `sys`: GPIO, ADC and the task watchdog (`02-firmware.md` §sys).
 * Compiled only for a chip target — the `linux` host target has no GPIO, and a
 * stub here would be a test of the stub (`AGENTS.md`, coverage gates). */

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_log.h"
#include "esp_pm.h"
#include "esp_task_wdt.h"

#include "sys.h"

#define SYS_WDT_TIMEOUT_S 5

static adc_oneshot_unit_handle_t s_adc;
static i2c_master_bus_handle_t s_i2c_bus;
/* One shared lock rather than one per caller: a run is the only thing that holds it,
 * so a second handle would only add a way to leak the first. */
static esp_pm_lock_handle_t s_pm_busy;

esp_err_t sys_pm_init(void)
{
#if CONFIG_PM_ENABLE
    /* `08-code-architecture.md` §5.3. Light sleep stays off: `01-hardware.md` §5
     * requires `ping` and `schedule` to answer while the link is open, and light
     * sleep is what breaks that (§5.4). 80 MHz is the floor because below it the
     * SPI clock driving the LCD-free but DMA-bound peripherals starts to matter,
     * and the saving is already most of the way there. */
    const esp_pm_config_t pm = {
        .max_freq_mhz = 160,
        .min_freq_mhz = 80,
        .light_sleep_enable = false,
    };
    esp_err_t err = esp_pm_configure(&pm);
    if (err != ESP_OK) return err;
    return esp_pm_lock_create(ESP_PM_CPU_FREQ_MAX, 0, "agent-run", &s_pm_busy);
#else
    /* No DFS in this build: `CONFIG_PM_ENABLE` is what compiles the API in at all,
     * so naming it here would not link. */
    return ESP_ERR_NOT_SUPPORTED;
#endif
}

esp_err_t sys_pm_busy_begin(void)
{
#if CONFIG_PM_ENABLE
    return s_pm_busy != NULL ? esp_pm_lock_acquire(s_pm_busy) : ESP_OK;
#else
    return ESP_OK;
#endif
}

void sys_pm_busy_end(void)
{
#if CONFIG_PM_ENABLE
    if (s_pm_busy != NULL) {
        (void)esp_pm_lock_release(s_pm_busy);
    }
#endif
}

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

/* A pin is configured on every call rather than at boot: the model chooses the pin
 * at call time, and a pin left configured is a pin left driving something. */
esp_err_t sys_gpio_read(int pin, int *level)
{
    if (level == NULL) return ESP_ERR_INVALID_ARG;
    if (!sys_pin_readable(pin)) return ESP_ERR_INVALID_ARG;

    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&cfg);
    if (err != ESP_OK) return err;
    *level = gpio_get_level(pin);
    return ESP_OK;
}

esp_err_t sys_gpio_write(int pin, int level)
{
    if (!sys_pin_writable(pin)) return ESP_ERR_INVALID_ARG;
    if (level != 0 && level != 1) return ESP_ERR_INVALID_ARG;

    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&cfg);
    if (err != ESP_OK) return err;
    return gpio_set_level(pin, level);
}

esp_err_t sys_gpio_edge_config(int pin, int edge)
{
    if (!sys_pin_readable(pin)) return ESP_ERR_INVALID_ARG;
    /* Only the three documented edges: passing an arbitrary `gpio_int_type_t` would let
     * a caller request a level-triggered interrupt the scheduler never drains. */
    if (edge != SYS_EDGE_RISING && edge != SYS_EDGE_FALLING && edge != SYS_EDGE_ANY) {
        return ESP_ERR_INVALID_ARG;
    }

    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_INPUT,
        /* Pull-up: an edge on a floating pin is noise, and every header pin is
         * open-drain on the sensor boards this targets. */
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = (gpio_int_type_t)edge,
    };
    return gpio_config(&cfg);
}

/* ADC1 only: ADC2 is claimed by the radio while WiFi is up (`tools/adc.md`). */
esp_err_t sys_adc_read(int pin, int *raw)
{
    if (raw == NULL || !sys_pin_adc(pin)) return ESP_ERR_INVALID_ARG;

    adc_oneshot_unit_handle_t unit = NULL;
    if (s_adc == NULL) {
        adc_oneshot_unit_init_cfg_t init = {.unit_id = ADC_UNIT_1};
        esp_err_t err = adc_oneshot_new_unit(&init, &s_adc);
        if (err != ESP_OK) return err;
        unit = s_adc;
    } else {
        unit = s_adc;
    }

    adc_channel_t channel = pin == 1 ? ADC_CHANNEL_0
                          : pin == 2 ? ADC_CHANNEL_1
                          : ADC_CHANNEL_3;  /* GPIO4 */
    adc_oneshot_chan_cfg_t chan = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    esp_err_t err = adc_oneshot_config_channel(unit, channel, &chan);
    if (err != ESP_OK) return err;
    return adc_oneshot_read(unit, channel, raw);
}

/* One bus, opened on the first call. `tools.i2c_sda`/`i2c_scl` are boot-only
 * (`03-config.md` §tools), so the first call's pins are the pins for the boot. */
static bool s_i2c_open;
static int s_i2c_sda = -1;
static int s_i2c_scl = -1;

static esp_err_t i2c_ensure(int sda, int scl)
{
    if (s_i2c_open) {
        return sda == s_i2c_sda && scl == s_i2c_scl ? ESP_OK : ESP_ERR_INVALID_STATE;
    }
    i2c_master_bus_config_t bus = {
        .i2c_port = -1,             /* auto */
        .sda_io_num = sda,
        .scl_io_num = scl,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t err = i2c_new_master_bus(&bus, &s_i2c_bus);
    if (err != ESP_OK) return err;
    s_i2c_open = true;
    s_i2c_sda = sda;
    s_i2c_scl = scl;
    return ESP_OK;
}

/* A device handle is created per call, so a model that probes sixty addresses does
 * not leak sixty handles and a removed device is reinitialised rather than cached. */
static esp_err_t i2c_device(int addr, i2c_master_dev_handle_t *out)
{
    i2c_device_config_t dev = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = (uint16_t)addr,
        .scl_speed_hz = 100000,
    };
    return i2c_master_bus_add_device(s_i2c_bus, &dev, out);
}

esp_err_t sys_i2c_probe(int sda, int scl, int addr)
{
    esp_err_t err = i2c_ensure(sda, scl);
    if (err != ESP_OK) return err;
    return i2c_master_probe(s_i2c_bus, (uint16_t)addr, 50);
}

esp_err_t sys_i2c_read(int sda, int scl, int addr, uint8_t *out, size_t len)
{
    if (out == NULL || len == 0) return ESP_ERR_INVALID_ARG;
    esp_err_t err = i2c_ensure(sda, scl);
    if (err != ESP_OK) return err;

    i2c_master_dev_handle_t dev = NULL;
    err = i2c_device(addr, &dev);
    if (err != ESP_OK) return err;
    err = i2c_master_receive(dev, out, len, 100);
    i2c_master_bus_rm_device(dev);
    return err;
}

esp_err_t sys_i2c_write(int sda, int scl, int addr, const uint8_t *data, size_t len)
{
    if (data == NULL || len == 0) return ESP_ERR_INVALID_ARG;
    esp_err_t err = i2c_ensure(sda, scl);
    if (err != ESP_OK) return err;

    i2c_master_dev_handle_t dev = NULL;
    err = i2c_device(addr, &dev);
    if (err != ESP_OK) return err;
    err = i2c_master_transmit(dev, data, len, 100);
    i2c_master_bus_rm_device(dev);
    return err;
}
