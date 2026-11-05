/* The reader task for the USB-Serial/JTAG CDC endpoint (`02-firmware.md` §3.1
 * step 7, §3.3). Board-only: the host suite tests the codec, not the port, so this
 * file never enters the `linux` build. */

#include "link.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "driver/usb_serial_jtag.h"
#include "esp_log.h"

static const char *TAG = "link";

/* The driver owns one endpoint and one reader: this task. Statics are the honest
 * shape, because a second reader would desynchronise the framing (§1.1). */
static link_reader_t s_reader;

static esp_err_t cdc_write(const char *buf, size_t len)
{
    /* One call per frame: the framing depends on no other task interleaving bytes,
     * which `link_emit` guarantees by calling the writer exactly once (§1.2). */
    int written = usb_serial_jtag_write_bytes(buf, len, pdMS_TO_TICKS(100));
    return written == (int)len ? ESP_OK : ESP_FAIL;
}

static void read_line(const char *line, size_t len, link_line_status_t status)
{
    if (status != LINK_LINE_OK) {
        /* §1.2: the answer depends on whether an `id` is still recoverable, which
         * `link_refuse_line` decides. Length is the bytes actually seen. */
        link_refuse_line(line, len, "", "malformed frame", 0);
        return;
    }

    json_doc_t *frame = NULL;
    const char *bad_field = NULL;
    esp_err_t err = link_frame_parse(line, len, &frame, &bad_field);
    if (err != ESP_OK) {
        /* Naming the member is what makes the error actionable (§4 `E_PROTOCOL`,
         * §1.2). A parse failure has no type to dispatch on. */
        link_refuse_line(line, len, NULL,
                         bad_field != NULL ? bad_field : "frame", 0);
        return;
    }

    const json_node_t *root = json_root(frame);
    const char *type = json_string(json_get(root, "type"));

    /* `ping` is answered here, not by the sink: §3.7 makes it live while a run is
     * in flight, and routing it through `app` would put a keepalive behind the
     * agent. Direction is already validated by `link_frame_parse`. */
    if (type != NULL && strcmp(type, "ping") == 0) {
        const char *id = json_string(json_get(root, "id"));
        if (id != NULL) link_emit_pong(id, 0);
    } else if (type != NULL) {
        /* `link_sink_get` is `link`'s own accessor, so the task reads the sink the
         * same way any other module would: registration stays in `link.c`. */
        const link_sink_t *sink = link_sink_get();
        if (sink != NULL && sink->on_frame != NULL) {
            sink->on_frame(root);
        } else {
            ESP_LOGW(TAG, "inbound frame of type %s dropped: no sink", type);
        }
    }

    json_free(frame);
}

static void link_task(void *arg)
{
    (void)arg;
    uint8_t buf[256];

    for (;;) {
        /* A blocking read with a long timeout, not a poll: the task sleeps until
         * bytes arrive, which is the power rule `08-code-architecture.md` §5.2
         * requires. A timeout is simply "no frame yet". */
        int n = usb_serial_jtag_read_bytes(buf, sizeof(buf), pdMS_TO_TICKS(100));
        if (n <= 0) continue;

        for (int i = 0; i < n; i++) {
            size_t line_len = 0;
            link_line_status_t status = LINK_LINE_OK;
            if (link_reader_feed(&s_reader, (char)buf[i], &line_len, &status)) {
                read_line(link_reader_line(&s_reader), line_len, status);
            }
        }
    }
}

esp_err_t link_start(void)
{
    static bool started = false;
    if (started) return ESP_OK;

    /* The secondary console writes through this same driver's buffer, so installing
     * it does not create a second owner of the endpoint (`02-firmware.md` §3.1).
     *
     * The buffers are the protocol limits, not IDF's 256-byte default: `link_emit`
     * makes exactly one writer call per frame so two tasks cannot interleave halves
     * of a line, and a ring smaller than `LINK_TX_MAX` would refuse that single call
     * for any frame over 256 bytes and drop it. */
    usb_serial_jtag_driver_config_t config = {
        .tx_buffer_size = LINK_TX_MAX + 2,
        .rx_buffer_size = LINK_RX_MAX + 2,
    };
    esp_err_t err = usb_serial_jtag_driver_install(&config);
    if (err != ESP_OK) {
        /* Reported, not fatal: `app` decides whether a device without a link boots
         * to a halt or keeps going (`02-firmware.md` §3.1 step 7). */
        ESP_LOGE(TAG, "cdc driver install failed: %s", esp_err_to_name(err));
        return err;
    }

    link_reader_reset(&s_reader);
    link_set_writer(cdc_write);

    /* High priority: a frame must be answered while the agent is busy (§3.3). The
     * task blocks on the read, so it costs nothing when idle. */
    BaseType_t created = xTaskCreate(link_task, "link_task", 4096, NULL,
                                     configMAX_PRIORITIES - 3, NULL);
    if (created != pdPASS) {
        ESP_LOGE(TAG, "link task could not start");
        return ESP_ERR_NO_MEM;
    }

    started = true;
    return ESP_OK;
}
