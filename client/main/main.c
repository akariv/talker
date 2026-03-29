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
#include "esp_crt_bundle.h"
#include "driver/i2s_std.h"
#include "driver/dac_continuous.h"
#include "secrets.h"

static const char *TAG = "intercom";

// ---- Config ----
#define RECORD_SECONDS  10
#define MIC_SAMPLE_RATE 16000
#define MIC_BUF_SIZE    4096
#define DAC_SAMPLE_RATE 16000
#define DAC_GAIN        15
#define POLL_INTERVAL_MS 1000
#define POLL_MAX_RETRIES 30

// I2S mic pins
#define MIC_SCK_PIN     33
#define MIC_WS_PIN      25
#define MIC_SD_PIN      32

// ---- WiFi ----
static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
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

// ---- HTTP helpers ----
static esp_http_client_handle_t http_init(const char *path, esp_http_client_method_t method)
{
    char url[256];
    snprintf(url, sizeof(url), "%s%s", SERVER_URL, path);

    esp_http_client_config_t config = {
        .url = url,
        .crt_bundle_attach = esp_crt_bundle_attach,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);

    esp_http_client_set_method(client, method);
    esp_http_client_set_header(client, "X-Client-Name", CLIENT_NAME);
    esp_http_client_set_header(client, "X-Api-Key", CLIENT_API_KEY);

    return client;
}

static void http_post(const char *path, const uint8_t *data, int len)
{
    esp_http_client_handle_t client = http_init(path, HTTP_METHOD_POST);
    esp_http_client_set_header(client, "Content-Type", "application/octet-stream");

    esp_err_t err = esp_http_client_open(client, len);
    if (err == ESP_OK) {
        if (len > 0 && data != NULL) {
            esp_http_client_write(client, (const char *)data, len);
        }
        esp_http_client_fetch_headers(client);
    } else {
        ESP_LOGE(TAG, "POST %s failed: %s", path, esp_err_to_name(err));
    }

    esp_http_client_close(client);
    esp_http_client_cleanup(client);
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

// ---- Record + Upload ----
static void record_and_upload(void)
{
    uint8_t buf[MIC_BUF_SIZE];
    size_t bytes_read = 0;
    int total_bytes = RECORD_SECONDS * MIC_SAMPLE_RATE * 2;
    int total_sent = 0;

    http_post("/reset", NULL, 0);

    ESP_LOGI(TAG, "Recording %d seconds...", RECORD_SECONDS);

    while (total_sent < total_bytes) {
        esp_err_t err = i2s_channel_read(mic_handle, buf, sizeof(buf),
                                         &bytes_read, portMAX_DELAY);
        if (err != ESP_OK || bytes_read == 0) {
            ESP_LOGE(TAG, "I2S read error: %s", esp_err_to_name(err));
            continue;
        }

        http_post("/upload", buf, bytes_read);
        total_sent += bytes_read;

        float elapsed = (float)total_sent / (MIC_SAMPLE_RATE * 2);
        ESP_LOGI(TAG, "  Sent %d bytes (%.1fs)", total_sent, elapsed);
    }

    http_post("/done", NULL, 0);
    ESP_LOGI(TAG, "Recording complete (%d bytes)", total_sent);
}

// ---- DAC Playback ----
static void play_audio(esp_http_client_handle_t client, int content_length)
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

        int n = esp_http_client_read(client, (char *)read_buf + leftover, to_read);
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
}

// ---- Poll for responses ----
static void poll_and_play(void)
{
    ESP_LOGI(TAG, "Polling for AI response...");

    for (int attempt = 0; attempt < POLL_MAX_RETRIES; attempt++) {
        esp_http_client_handle_t client = http_init("/poll", HTTP_METHOD_GET);

        esp_err_t err = esp_http_client_open(client, 0);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "GET /poll failed: %s", esp_err_to_name(err));
            esp_http_client_cleanup(client);
            vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));
            continue;
        }

        int content_length = esp_http_client_fetch_headers(client);
        int status = esp_http_client_get_status_code(client);

        if (status == 200 && content_length > 0) {
            play_audio(client, content_length);
            esp_http_client_close(client);
            esp_http_client_cleanup(client);
            // Check for more messages
            attempt = -1;  // reset counter
            continue;
        }

        if (status == 204) {
            ESP_LOGI(TAG, "  No response yet (attempt %d/%d)", attempt + 1, POLL_MAX_RETRIES);
        } else if (status == 503) {
            ESP_LOGW(TAG, "  Server error (503), retrying...");
        } else {
            ESP_LOGW(TAG, "  Unexpected status %d", status);
        }

        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));
    }

    ESP_LOGI(TAG, "Polling complete");
}

// ---- Main ----
void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    wifi_init();

    while (1) {
        ESP_LOGI(TAG, "Starting in 3 seconds...");
        vTaskDelay(pdMS_TO_TICKS(3000));

        mic_init();
        record_and_upload();
        mic_deinit();

        poll_and_play();

        ESP_LOGI(TAG, "Cycle complete. Restarting...");
    }
}
