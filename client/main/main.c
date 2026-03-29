#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_http_client.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "driver/dac_continuous.h"
#include "secrets.h"

static const char *TAG = "intercom";

// ---- Config ----
#define MIC_SAMPLE_RATE      16000
#define MIC_BUF_SIZE         4096
#define DAC_SAMPLE_RATE      16000
#define DAC_GAIN             15
#define RECORD_MAX_SECONDS   30

#define POLL_IDLE_INTERVAL_MS   10000
#define POLL_ACTIVE_INTERVAL_MS 1000
#define POLL_ACTIVE_RETRIES     30

// I2S mic pins
#define MIC_SCK_PIN  33
#define MIC_WS_PIN   25
#define MIC_SD_PIN   32

// Button and LED
#define BUTTON_PIN   GPIO_NUM_4
#define LED_PIN      GPIO_NUM_2

// ---- WiFi ----
static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGW(TAG, "Disconnected, retrying...");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Connected! IP: " IPSTR, IP2STR(&event->ip_info.ip));
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

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Connecting to WiFi...");
    xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
}

static bool wifi_connected(void)
{
    return (xEventGroupGetBits(s_wifi_event_group) & WIFI_CONNECTED_BIT) != 0;
}

// ---- Embedded root CA for Cloud Run (GTS Root R1) ----
extern const uint8_t server_root_ca_pem_start[] asm("_binary_server_root_ca_pem_start");
extern const uint8_t server_root_ca_pem_end[]   asm("_binary_server_root_ca_pem_end");

// ---- Single persistent HTTP client ----
// One client with keep-alive for all requests. TLS session is cached
// so switching between URLs doesn't require a new handshake.
static esp_http_client_handle_t http_client = NULL;

static void ensure_http_client(void)
{
    if (http_client != NULL) return;

    char url[256];
    snprintf(url, sizeof(url), "%s/poll", SERVER_URL);

    esp_http_client_config_t config = {
        .url = url,
        .cert_pem = (const char *)server_root_ca_pem_start,
        .timeout_ms = 15000,
        .keep_alive_enable = true,
    };
    http_client = esp_http_client_init(&config);
    esp_http_client_set_header(http_client, "X-Client-Name", CLIENT_NAME);
    esp_http_client_set_header(http_client, "X-Api-Key", CLIENT_API_KEY);
    esp_http_client_set_header(http_client, "Content-Type", "application/octet-stream");
}

static void reset_http_client(void)
{
    if (http_client != NULL) {
        esp_http_client_close(http_client);
        esp_http_client_cleanup(http_client);
        http_client = NULL;
        ESP_LOGI(TAG, "HTTP client reset. Free heap: %lu bytes", esp_get_free_heap_size());
    }
}

// ---- Chunked encoding helpers ----
static int write_chunk(const char *data, int len)
{
    char header[16];
    int hlen = snprintf(header, sizeof(header), "%x\r\n", len);

    if (esp_http_client_write(http_client, header, hlen) < 0) return -1;
    if (len > 0 && esp_http_client_write(http_client, data, len) < 0) return -1;
    if (esp_http_client_write(http_client, "\r\n", 2) < 0) return -1;
    return len;
}

static int write_chunk_end(void)
{
    return esp_http_client_write(http_client, "0\r\n\r\n", 5);
}

// ---- I2S Mic ----
static i2s_chan_handle_t mic_handle = NULL;

static void mic_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &mic_handle));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)MIC_SCK_PIN,
            .ws   = (gpio_num_t)MIC_WS_PIN,
            .din  = (gpio_num_t)MIC_SD_PIN,
            .dout = I2S_GPIO_UNUSED,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(mic_handle, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(mic_handle));
    ESP_LOGI(TAG, "Mic initialized (I2S RX, 16-bit mono, %d Hz)", MIC_SAMPLE_RATE);
}

static void mic_deinit(void)
{
    i2s_channel_disable(mic_handle);
    i2s_del_channel(mic_handle);
    mic_handle = NULL;
}

// ---- Button + LED ----
static void gpio_init(void)
{
    gpio_config_t btn_cfg = {
        .pin_bit_mask = (1ULL << BUTTON_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&btn_cfg));

    gpio_config_t led_cfg = {
        .pin_bit_mask = (1ULL << LED_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&led_cfg));
    gpio_set_level(LED_PIN, 0);
}

static bool button_pressed(void)
{
    return gpio_get_level(BUTTON_PIN) == 0;
}

// ---- Forward declarations ----
static void poll_loop(int interval_ms, int max_retries);

// ---- Record + stream via chunked POST ----
static void record_and_upload(void)
{
    uint8_t buf[MIC_BUF_SIZE];
    size_t bytes_read = 0;
    int max_bytes = RECORD_MAX_SECONDS * MIC_SAMPLE_RATE * 2;
    int total_sent = 0;
    bool upload_ok = false;

    gpio_set_level(LED_PIN, 1);

    // Discard stale I2S data
    i2s_channel_read(mic_handle, buf, sizeof(buf), &bytes_read, pdMS_TO_TICKS(50));

    // Open chunked POST to /voice
    ensure_http_client();
    char url[256];
    snprintf(url, sizeof(url), "%s/voice", SERVER_URL);
    esp_http_client_set_url(http_client, url);
    esp_http_client_set_method(http_client, HTTP_METHOD_POST);

    esp_err_t err = esp_http_client_open(http_client, -1);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open voice connection: %s", esp_err_to_name(err));
        reset_http_client();
        gpio_set_level(LED_PIN, 0);
        return;
    }

    ESP_LOGI(TAG, "Recording — speak now!");

    while (total_sent < max_bytes) {
        err = i2s_channel_read(mic_handle, buf, sizeof(buf),
                               &bytes_read, portMAX_DELAY);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "I2S read error: %s", esp_err_to_name(err));
            break;
        }
        if (bytes_read == 0) continue;

        if (write_chunk((const char *)buf, bytes_read) < 0) {
            ESP_LOGE(TAG, "Stream write failed");
            reset_http_client();
            break;
        }
        total_sent += bytes_read;

        if (!button_pressed()) break;
    }

    gpio_set_level(LED_PIN, 0);

    float duration = (float)total_sent / (MIC_SAMPLE_RATE * 2);
    ESP_LOGI(TAG, "Recorded %.1fs (%d bytes)", duration, total_sent);

    if (http_client != NULL && total_sent >= 3200) {
        write_chunk_end();
        esp_http_client_fetch_headers(http_client);
        int status = esp_http_client_get_status_code(http_client);
        esp_http_client_close(http_client);

        if (status == 200 || status == 202) {
            ESP_LOGI(TAG, "Upload done (status %d, heap: %lu), processing...",
                     status, esp_get_free_heap_size());
            upload_ok = true;
        } else {
            ESP_LOGE(TAG, "Upload failed (status %d)", status);
        }
    } else {
        if (http_client != NULL) esp_http_client_close(http_client);
        if (total_sent < 3200) ESP_LOGW(TAG, "Too short, discarding");
    }

    if (upload_ok) {
        poll_loop(POLL_ACTIVE_INTERVAL_MS, POLL_ACTIVE_RETRIES);
    }
}

// ---- DAC Playback ----
static void play_audio(int content_length)
{
    dac_continuous_handle_t dac_handle;
    dac_continuous_config_t dac_cfg = {
        .chan_mask = DAC_CHANNEL_MASK_CH1,
        .desc_num = 8,
        .buf_size = 2048,
        .freq_hz = DAC_SAMPLE_RATE,
        .offset = 0,
        .clk_src = DAC_DIGI_CLK_SRC_APLL,
        .chan_mode = DAC_CHANNEL_MODE_SIMUL,
    };
    ESP_ERROR_CHECK(dac_continuous_new_channels(&dac_cfg, &dac_handle));
    ESP_ERROR_CHECK(dac_continuous_enable(dac_handle));

    ESP_LOGI(TAG, "Playing %d bytes (16-bit PCM -> 8-bit DAC)", content_length);

    uint8_t read_buf[2048 + 1];
    uint8_t dac_buf[1024];
    int bytes_played = 0;
    int leftover = 0;

    while (bytes_played < content_length) {
        int to_read = sizeof(read_buf) - leftover;
        if (to_read > content_length - bytes_played) {
            to_read = content_length - bytes_played;
        }

        int n = esp_http_client_read(http_client, (char *)read_buf + leftover, to_read);
        if (n <= 0) break;

        int available = leftover + n;
        int num_samples = available / 2;
        leftover = available % 2;

        int16_t *samples = (int16_t *)read_buf;
        for (int i = 0; i < num_samples; i++) {
            int32_t amplified = (int32_t)samples[i] * DAC_GAIN;
            if (amplified > 32767) amplified = 32767;
            if (amplified < -32768) amplified = -32768;
            dac_buf[i] = (uint8_t)((amplified + 32768) >> 8);
        }

        if (leftover) {
            read_buf[0] = read_buf[num_samples * 2];
        }

        size_t bytes_written = 0;
        ESP_ERROR_CHECK(dac_continuous_write(dac_handle, dac_buf, num_samples,
                                            &bytes_written, portMAX_DELAY));
        bytes_played += n;
    }

    ESP_LOGI(TAG, "Playback complete (%d bytes)", bytes_played);
    vTaskDelay(pdMS_TO_TICKS(500));

    dac_continuous_disable(dac_handle);
    dac_continuous_del_channels(dac_handle);
    ESP_LOGI(TAG, "Free heap after playback: %lu bytes", esp_get_free_heap_size());
}

// ---- Poll + play ----
static int poll_once(void)
{
    if (!wifi_connected()) return -1;

    ensure_http_client();
    char url[256];
    snprintf(url, sizeof(url), "%s/poll", SERVER_URL);
    esp_http_client_set_url(http_client, url);
    esp_http_client_set_method(http_client, HTTP_METHOD_GET);

    esp_err_t err = esp_http_client_open(http_client, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "GET /poll failed: %s", esp_err_to_name(err));
        reset_http_client();
        return -1;
    }

    int content_length = esp_http_client_fetch_headers(http_client);
    int status = esp_http_client_get_status_code(http_client);

    if (status == 200 && content_length > 0) {
        mic_deinit();
        play_audio(content_length);
        mic_init();
        esp_http_client_close(http_client);
        return 1;
    }

    // Must read any remaining response body before closing
    esp_http_client_close(http_client);

    if (status == 204) {
        ESP_LOGI(TAG, "Poll: no messages (heap: %lu)", esp_get_free_heap_size());
        return 0;
    }

    ESP_LOGW(TAG, "Poll returned status %d", status);
    return -1;
}

static void poll_loop(int interval_ms, int max_retries)
{
    int empty_count = 0;

    while (empty_count < max_retries) {
        int result = poll_once();
        if (result == 1) {
            empty_count = 0;
        } else {
            empty_count++;
        }

        if (button_pressed()) return;

        vTaskDelay(pdMS_TO_TICKS(interval_ms));
    }
}

// ---- Main ----
void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    gpio_init();
    wifi_init();
    mic_init();

    ESP_LOGI(TAG, "Client '%s' ready. Press button to record.", CLIENT_NAME);
    ESP_LOGI(TAG, "Free heap: %lu bytes", esp_get_free_heap_size());

    while (1) {
        // Poll for incoming messages
        int result;
        do {
            result = poll_once();
        } while (result == 1);

        // Check button
        if (button_pressed()) {
            vTaskDelay(pdMS_TO_TICKS(50));
            if (button_pressed()) {
                record_and_upload();
                continue;
            }
        }

        // Idle wait — check button every 100ms
        for (int i = 0; i < POLL_IDLE_INTERVAL_MS / 100; i++) {
            vTaskDelay(pdMS_TO_TICKS(100));
            if (button_pressed()) {
                vTaskDelay(pdMS_TO_TICKS(50));
                if (button_pressed()) {
                    record_and_upload();
                    break;
                }
            }
        }
    }
}
