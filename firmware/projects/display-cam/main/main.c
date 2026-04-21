/**
 * display-cam — WiFi-connected SSD1680 clock + periodic OV7670 upload.
 *
 * Two loops in one task:
 *   15s  GET  /display/frame?w=296&h=128   (landscape; server rotates)
 *              200 → body is 9472 bytes = black plane ‖ red plane (panel native)
 *              204 → no change, skip refresh
 *   60s  POST /display/photo               (raw QVGA grayscale bytes)
 */

#include <stdbool.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_http_client.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_camera.h"
#include "secrets.h"

static const char *TAG = "display_cam";

// ---- Timing ----
#define DISPLAY_POLL_INTERVAL_MS  15000
#define PHOTO_UPLOAD_INTERVAL_MS  60000
#define SCHED_TICK_MS               500

// ---- SSD1680 refresh modes (arg to cmd 0x22) ----
// 0xF7 = full multi-pass LUT — slow (~15s), no ghosting, correct on BWR.
// 0xC7 = fast/partial — single mono LUT — ~1s but red plane is undefined
// on BWR panels. Experiment: do a full update every FULL_UPDATE_EVERY
// cycles to clear ghosting and re-seat the red plane.
#define DISPLAY_UPDATE_FULL   0xF7
#define DISPLAY_UPDATE_FAST   0xC7
#define FULL_UPDATE_EVERY     10

// ---- Panel geometry (SSD1680, 128×296 native; mounted landscape) ----
#define PANEL_W       128
#define PANEL_H       296
#define ROW_BYTES     (PANEL_W / 8)         // 16
#define PLANE_BYTES   (ROW_BYTES * PANEL_H) // 4736
#define FRAME_BYTES   (PLANE_BYTES * 2)     // 9472

// Logical landscape size the server renders into before rotating.
#define LOGICAL_W     PANEL_H  // 296
#define LOGICAL_H     PANEL_W  // 128

// ---- Display pins ----
#define PIN_CLK       GPIO_NUM_23
#define PIN_MOSI      GPIO_NUM_18
#define PIN_CS        GPIO_NUM_16
#define PIN_DC        GPIO_NUM_17
#define PIN_RES       GPIO_NUM_5
#define PIN_BUSY      GPIO_NUM_4

#define SPI_HOST      SPI3_HOST
#define SPI_CLOCK_HZ  (4 * 1000 * 1000)

// ---- OV7670 camera pins ----
#define CAM_PIN_SIOD   33
#define CAM_PIN_SIOC   25
#define CAM_PIN_XCLK   15
#define CAM_PIN_PCLK   19
#define CAM_PIN_VSYNC  26
#define CAM_PIN_HREF   32
#define CAM_PIN_D0     35
#define CAM_PIN_D1     22
#define CAM_PIN_D2     34
#define CAM_PIN_D3     13
#define CAM_PIN_D4     39
#define CAM_PIN_D5     14
#define CAM_PIN_D6     36
#define CAM_PIN_D7     27
#define CAM_PIN_RESET  21
#define CAM_PIN_PWDN   -1
#define CAM_XCLK_HZ    (8 * 1000 * 1000)
#define CAM_W          320
#define CAM_H          240
#define CAM_FRAME_BYTES (CAM_W * CAM_H)  // grayscale, 76800

static spi_device_handle_t s_spi;
static uint8_t *s_black_plane;
static uint8_t *s_red_plane;

// ------------------------------------------------------------------
// WiFi
// ------------------------------------------------------------------

static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

static void wifi_event_handler(void *arg, esp_event_base_t base,
                                int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGW(TAG, "WiFi disconnected, retrying...");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Connected, IP: " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void)
{
    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_cfg = {
        .sta = { .ssid = WIFI_SSID, .password = WIFI_PASS },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Connecting to WiFi (%s)...", WIFI_SSID);
    xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
}

static bool wifi_connected(void)
{
    return (xEventGroupGetBits(s_wifi_event_group) & WIFI_CONNECTED_BIT) != 0;
}

// ------------------------------------------------------------------
// HTTP client — single persistent handle with keep-alive + embedded CA
// ------------------------------------------------------------------

extern const uint8_t server_root_ca_pem_start[] asm("_binary_server_root_ca_pem_start");
extern const uint8_t server_root_ca_pem_end[]   asm("_binary_server_root_ca_pem_end");

static esp_http_client_handle_t s_http = NULL;

static void ensure_http_client(void)
{
    if (s_http != NULL) return;

    esp_http_client_config_t cfg = {
        .url = SERVER_URL,  // overridden per request
        .cert_pem = (const char *)server_root_ca_pem_start,
        .timeout_ms = 15000,
        .keep_alive_enable = true,
    };
    s_http = esp_http_client_init(&cfg);
    esp_http_client_set_header(s_http, "X-Client-Name", CLIENT_NAME);
    esp_http_client_set_header(s_http, "X-Api-Key", CLIENT_API_KEY);
}

static void reset_http_client(void)
{
    if (s_http == NULL) return;
    esp_http_client_close(s_http);
    esp_http_client_cleanup(s_http);
    s_http = NULL;
    ESP_LOGI(TAG, "HTTP client reset (heap: %lu)", esp_get_free_heap_size());
}

// ------------------------------------------------------------------
// SSD1680 driver (copied verbatim from display-tester)
// ------------------------------------------------------------------

static void gpio_setup(void)
{
    gpio_config_t out = {
        .mode = GPIO_MODE_OUTPUT,
        .pin_bit_mask = (1ULL << PIN_CS) | (1ULL << PIN_DC) | (1ULL << PIN_RES),
    };
    ESP_ERROR_CHECK(gpio_config(&out));

    gpio_config_t in = {
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pin_bit_mask = (1ULL << PIN_BUSY),
    };
    ESP_ERROR_CHECK(gpio_config(&in));

    gpio_set_level(PIN_CS, 1);
    gpio_set_level(PIN_DC, 1);
    gpio_set_level(PIN_RES, 1);
}

static void spi_setup(void)
{
    spi_bus_config_t bus = {
        .mosi_io_num = PIN_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = PIN_CLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = PLANE_BYTES + 16,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(SPI_HOST, &bus, SPI_DMA_CH_AUTO));

    spi_device_interface_config_t dev = {
        .clock_speed_hz = SPI_CLOCK_HZ,
        .mode = 0,
        .spics_io_num = -1,
        .queue_size = 1,
    };
    ESP_ERROR_CHECK(spi_bus_add_device(SPI_HOST, &dev, &s_spi));
}

static void wait_busy(void)
{
    const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(30000);
    while (gpio_get_level(PIN_BUSY) == 1) {
        if (xTaskGetTickCount() > deadline) {
            ESP_LOGW(TAG, "BUSY still high after 30s");
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

static void reset_panel(void)
{
    gpio_set_level(PIN_RES, 1); vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(PIN_RES, 0); vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(PIN_RES, 1); vTaskDelay(pdMS_TO_TICKS(10));
    wait_busy();
}

static void spi_tx(const uint8_t *buf, size_t len)
{
    if (len == 0) return;
    spi_transaction_t t = { .length = len * 8, .tx_buffer = buf };
    ESP_ERROR_CHECK(spi_device_polling_transmit(s_spi, &t));
}

static void write_cmd(uint8_t cmd)
{
    gpio_set_level(PIN_DC, 0);
    gpio_set_level(PIN_CS, 0);
    spi_tx(&cmd, 1);
    gpio_set_level(PIN_CS, 1);
}

static void write_data(const uint8_t *buf, size_t len)
{
    gpio_set_level(PIN_DC, 1);
    gpio_set_level(PIN_CS, 0);
    spi_tx(buf, len);
    gpio_set_level(PIN_CS, 1);
}

static void write_data1(uint8_t b) { write_data(&b, 1); }

static void set_ram_addr(void)
{
    write_cmd(0x4E); write_data1(0x00);
    write_cmd(0x4F);
    uint8_t y0[] = {0x00, 0x00};
    write_data(y0, sizeof(y0));
}

static void display_init(void)
{
    reset_panel();

    write_cmd(0x12); wait_busy();

    write_cmd(0x01);
    uint8_t driver[] = {0x27, 0x01, 0x00};
    write_data(driver, sizeof(driver));

    write_cmd(0x11); write_data1(0x03);

    write_cmd(0x44);
    uint8_t rx[] = {0x00, 0x0F};
    write_data(rx, sizeof(rx));

    write_cmd(0x45);
    uint8_t ry[] = {0x00, 0x00, 0x27, 0x01};
    write_data(ry, sizeof(ry));

    write_cmd(0x3C); write_data1(0x05);
    write_cmd(0x18); write_data1(0x80);

    set_ram_addr();
    wait_busy();
}

static void display_write_planes(const uint8_t *black, const uint8_t *red)
{
    set_ram_addr();
    write_cmd(0x24); write_data(black, PLANE_BYTES);

    set_ram_addr();
    write_cmd(0x26); write_data(red, PLANE_BYTES);
}

static void display_refresh(uint8_t mode)
{
    write_cmd(0x22); write_data1(mode);
    write_cmd(0x20);
    wait_busy();
}

// ------------------------------------------------------------------
// Camera (per-capture init/deinit)
// ------------------------------------------------------------------

static esp_err_t camera_init(void)
{
    camera_config_t cfg = {
        .pin_pwdn       = CAM_PIN_PWDN,
        .pin_reset      = CAM_PIN_RESET,
        .pin_xclk       = CAM_PIN_XCLK,
        .pin_sccb_sda   = CAM_PIN_SIOD,
        .pin_sccb_scl   = CAM_PIN_SIOC,
        .pin_d7         = CAM_PIN_D7,
        .pin_d6         = CAM_PIN_D6,
        .pin_d5         = CAM_PIN_D5,
        .pin_d4         = CAM_PIN_D4,
        .pin_d3         = CAM_PIN_D3,
        .pin_d2         = CAM_PIN_D2,
        .pin_d1         = CAM_PIN_D1,
        .pin_d0         = CAM_PIN_D0,
        .pin_vsync      = CAM_PIN_VSYNC,
        .pin_href       = CAM_PIN_HREF,
        .pin_pclk       = CAM_PIN_PCLK,
        .xclk_freq_hz   = CAM_XCLK_HZ,
        .ledc_timer     = LEDC_TIMER_0,
        .ledc_channel   = LEDC_CHANNEL_0,
        .pixel_format   = PIXFORMAT_GRAYSCALE,
        .frame_size     = FRAMESIZE_QVGA,
        .fb_count       = 1,
        .fb_location    = CAMERA_FB_IN_DRAM,
        .grab_mode      = CAMERA_GRAB_WHEN_EMPTY,
    };
    return esp_camera_init(&cfg);
}

// ------------------------------------------------------------------
// /display/frame GET — stream 9472 bytes straight into plane buffers
// ------------------------------------------------------------------

static void do_display_poll(void)
{
    ensure_http_client();

    char url[256];
    snprintf(url, sizeof(url), "%s/display/frame?w=%d&h=%d",
             SERVER_URL, LOGICAL_W, LOGICAL_H);
    esp_http_client_set_url(s_http, url);
    esp_http_client_set_method(s_http, HTTP_METHOD_GET);

    esp_err_t err = esp_http_client_open(s_http, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "GET /display/frame open failed: %s", esp_err_to_name(err));
        reset_http_client();
        return;
    }

    int content_length = esp_http_client_fetch_headers(s_http);
    int status = esp_http_client_get_status_code(s_http);

    if (status == 204) {
        ESP_LOGI(TAG, "display: no change (heap: %lu)", esp_get_free_heap_size());
        esp_http_client_close(s_http);
        return;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "display: unexpected status %d", status);
        esp_http_client_close(s_http);
        return;
    }
    if (content_length != FRAME_BYTES) {
        ESP_LOGW(TAG, "display: unexpected length %d (want %d)",
                 content_length, FRAME_BYTES);
        esp_http_client_close(s_http);
        return;
    }

    // Read exactly PLANE_BYTES into black, then PLANE_BYTES into red.
    int got = 0;
    while (got < PLANE_BYTES) {
        int n = esp_http_client_read(s_http,
                    (char *)(s_black_plane + got), PLANE_BYTES - got);
        if (n <= 0) break;
        got += n;
    }
    if (got != PLANE_BYTES) {
        ESP_LOGW(TAG, "display: short read on black plane (%d/%d)", got, PLANE_BYTES);
        esp_http_client_close(s_http);
        return;
    }

    got = 0;
    while (got < PLANE_BYTES) {
        int n = esp_http_client_read(s_http,
                    (char *)(s_red_plane + got), PLANE_BYTES - got);
        if (n <= 0) break;
        got += n;
    }
    esp_http_client_close(s_http);
    if (got != PLANE_BYTES) {
        ESP_LOGW(TAG, "display: short read on red plane (%d/%d)", got, PLANE_BYTES);
        return;
    }

    static int s_refresh_count = 0;
    uint8_t mode = (s_refresh_count % FULL_UPDATE_EVERY == 0)
                       ? DISPLAY_UPDATE_FULL
                       : DISPLAY_UPDATE_FAST;
    s_refresh_count++;

    ESP_LOGI(TAG, "display: refresh #%d mode=0x%02X (heap: %lu)",
             s_refresh_count, mode, esp_get_free_heap_size());
    display_write_planes(s_black_plane, s_red_plane);
    display_refresh(mode);
}

// ------------------------------------------------------------------
// /display/photo POST — one QVGA grayscale frame, camera init+deinit
// ------------------------------------------------------------------

static void do_photo_upload(void)
{
    ESP_LOGI(TAG, "photo: initializing camera");
    if (camera_init() != ESP_OK) {
        ESP_LOGE(TAG, "photo: camera_init failed");
        return;
    }

    // Throw away the first frame — AGC/AWB needs one to settle.
    camera_fb_t *warmup = esp_camera_fb_get();
    if (warmup) esp_camera_fb_return(warmup);

    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        ESP_LOGW(TAG, "photo: fb_get returned NULL");
        esp_camera_deinit();
        return;
    }
    ESP_LOGI(TAG, "photo: captured %dx%d, %u bytes", fb->width, fb->height,
             (unsigned)fb->len);

    // Upload directly from fb->buf — no copy. The camera peripheral is
    // idle between fb_get/fb_return, so TLS running alongside is safe
    // and avoids needing another 76 KB contiguous heap block.
    int status = -1;
    int sent = 0;
    ensure_http_client();
    char url[256];
    snprintf(url, sizeof(url), "%s/display/photo", SERVER_URL);
    esp_http_client_set_url(s_http, url);
    esp_http_client_set_method(s_http, HTTP_METHOD_POST);
    esp_http_client_set_header(s_http, "Content-Type", "application/octet-stream");
    esp_http_client_set_header(s_http, "X-Image-Width",  "320");
    esp_http_client_set_header(s_http, "X-Image-Height", "240");
    esp_http_client_set_header(s_http, "X-Image-Format", "gray8");

    esp_err_t err = esp_http_client_open(s_http, fb->len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "photo: POST open failed: %s", esp_err_to_name(err));
        reset_http_client();
    } else {
        while (sent < (int)fb->len) {
            int n = esp_http_client_write(s_http,
                        (const char *)(fb->buf + sent), fb->len - sent);
            if (n < 0) { ESP_LOGE(TAG, "photo: write failed"); break; }
            sent += n;
        }
        if (sent == (int)fb->len) {
            esp_http_client_fetch_headers(s_http);
            status = esp_http_client_get_status_code(s_http);
        }
        esp_http_client_close(s_http);
    }

    esp_camera_fb_return(fb);
    esp_camera_deinit();

    // esp_camera_deinit() leaves the LEDC channel it used for XCLK still
    // routed to GPIO 27, which makes the *next* camera_init() warn
    // "GPIO 27 is not usable". Explicitly stop the channel and reset the
    // pin so the next init gets a clean slate.
    ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 0);
    gpio_reset_pin(CAM_PIN_XCLK);

    // Camera teardown tears down LEDC + I2S ISRs, which briefly starves
    // LWIP. Give the stack a beat before the next display poll's TLS call.
    vTaskDelay(pdMS_TO_TICKS(300));

    ESP_LOGI(TAG, "photo: uploaded %d bytes, status %d (heap: %lu)",
             sent, status, esp_get_free_heap_size());
}

// ------------------------------------------------------------------
// app_main
// ------------------------------------------------------------------

void app_main(void)
{
    ESP_LOGI(TAG, "display-cam booting as '%s'", CLIENT_NAME);
    ESP_LOGI(TAG, "Panel %dx%d native (%dx%d logical landscape), plane=%d bytes",
             PANEL_W, PANEL_H, LOGICAL_W, LOGICAL_H, PLANE_BYTES);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    gpio_setup();
    spi_setup();

    s_black_plane = heap_caps_malloc(PLANE_BYTES, MALLOC_CAP_8BIT);
    s_red_plane   = heap_caps_malloc(PLANE_BYTES, MALLOC_CAP_8BIT);
    if (!s_black_plane || !s_red_plane) {
        ESP_LOGE(TAG, "plane buffer alloc failed");
        return;
    }

    wifi_init();
    display_init();

    TickType_t last_display = 0, last_photo = 0;
    while (1) {
        TickType_t now = xTaskGetTickCount();

        if (wifi_connected()) {
            if (last_display == 0 ||
                (now - last_display) >= pdMS_TO_TICKS(DISPLAY_POLL_INTERVAL_MS)) {
                last_display = now;
                do_display_poll();
            }
            if (last_photo == 0 ||
                (now - last_photo) >= pdMS_TO_TICKS(PHOTO_UPLOAD_INTERVAL_MS)) {
                last_photo = now;
                do_photo_upload();
            }
        }

        vTaskDelay(pdMS_TO_TICKS(SCHED_TICK_MS));
    }
}
