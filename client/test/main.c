/**
 * Board Test Program
 *
 * Tests all hardware components in sequence, looping forever.
 * Watch serial output for PASS/FAIL results.
 *
 * Test sequence:
 *   1. Orange LED on/off
 *   2. Green LED on/off
 *   3. Both LEDs blink together
 *   4. Speaker: play a 440Hz test tone
 *   5. Microphone: record 1 second, check for signal
 *   6. WiFi: connect and report IP
 *   7. Button: wait for press (5s timeout)
 *   8. Repeat
 */

#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "secrets.h"

static const char *TAG = "board_test";

// ---- Pin definitions (must match main firmware) ----
#define MIC_SCK_PIN   33
#define MIC_WS_PIN    25
#define MIC_SD_PIN    32

#define SPK_SCK_PIN   27  // BCLK
#define SPK_WS_PIN    26  // LRC
#define SPK_SD_PIN    14  // DIN

#define LED_ORANGE    GPIO_NUM_5
#define LED_GREEN     GPIO_NUM_18
#define BUTTON_PIN    GPIO_NUM_4

#define SAMPLE_RATE   16000

// ---- WiFi ----
static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "  Connected! IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

// ---- Test functions ----

static void test_leds(void)
{
    ESP_LOGI(TAG, "=== TEST: LEDs ===");

    ESP_LOGI(TAG, "  Orange ON");
    gpio_set_level(LED_ORANGE, 1);
    vTaskDelay(pdMS_TO_TICKS(500));
    gpio_set_level(LED_ORANGE, 0);

    ESP_LOGI(TAG, "  Green ON");
    gpio_set_level(LED_GREEN, 1);
    vTaskDelay(pdMS_TO_TICKS(500));
    gpio_set_level(LED_GREEN, 0);

    ESP_LOGI(TAG, "  Both blinking");
    for (int i = 0; i < 6; i++) {
        gpio_set_level(LED_ORANGE, i % 2);
        gpio_set_level(LED_GREEN, i % 2);
        vTaskDelay(pdMS_TO_TICKS(250));
    }
    gpio_set_level(LED_ORANGE, 0);
    gpio_set_level(LED_GREEN, 0);

    ESP_LOGI(TAG, "  LEDs: PASS (verify visually)");
}

static void test_speaker(void)
{
    ESP_LOGI(TAG, "=== TEST: Speaker (440Hz tone, 1 second) ===");

    i2s_chan_handle_t spk_handle;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &spk_handle, NULL));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)SPK_SCK_PIN,
            .ws   = (gpio_num_t)SPK_WS_PIN,
            .din  = I2S_GPIO_UNUSED,
            .dout = (gpio_num_t)SPK_SD_PIN,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(spk_handle, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(spk_handle));

    // Generate 1 second of 440Hz sine wave
    int num_samples = SAMPLE_RATE;
    int16_t *tone_buf = malloc(num_samples * sizeof(int16_t));
    if (tone_buf == NULL) {
        ESP_LOGE(TAG, "  Failed to allocate tone buffer");
        return;
    }

    for (int i = 0; i < num_samples; i++) {
        tone_buf[i] = (int16_t)(10000.0f * sinf(2.0f * M_PI * 440.0f * i / SAMPLE_RATE));
    }

    size_t bytes_written;
    ESP_ERROR_CHECK(i2s_channel_write(spk_handle, tone_buf, num_samples * 2,
                                      &bytes_written, portMAX_DELAY));

    // Silence flush
    memset(tone_buf, 0, 4096);
    i2s_channel_write(spk_handle, tone_buf, 4096, &bytes_written, portMAX_DELAY);
    free(tone_buf);

    i2s_channel_disable(spk_handle);
    i2s_del_channel(spk_handle);

    ESP_LOGI(TAG, "  Speaker: PASS (verify you heard a tone)");
}

static void test_mic_and_playback(void)
{
    ESP_LOGI(TAG, "=== TEST: Record 3s + Playback ===");
    ESP_LOGI(TAG, "  Speak now...");

    // Green LED on during recording
    gpio_set_level(LED_GREEN, 1);

    // Init mic
    i2s_chan_handle_t mic_handle;
    i2s_chan_config_t mic_chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&mic_chan_cfg, NULL, &mic_handle));

    i2s_std_config_t mic_std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
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
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(mic_handle, &mic_std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(mic_handle));

    // Discard stale data
    uint8_t discard[4096];
    size_t bytes_read;
    i2s_channel_read(mic_handle, discard, sizeof(discard), &bytes_read, pdMS_TO_TICKS(100));

    // Record 3 seconds
    int rec_seconds = 3;
    int total_bytes = SAMPLE_RATE * 2 * rec_seconds;
    int16_t *rec_buf = malloc(total_bytes);
    if (rec_buf == NULL) {
        ESP_LOGE(TAG, "  Failed to allocate recording buffer");
        gpio_set_level(LED_GREEN, 0);
        return;
    }

    int offset = 0;
    while (offset < total_bytes) {
        int to_read = total_bytes - offset;
        if (to_read > 4096) to_read = 4096;
        ESP_ERROR_CHECK(i2s_channel_read(mic_handle, (uint8_t *)rec_buf + offset,
                                         to_read, &bytes_read, portMAX_DELAY));
        offset += bytes_read;
    }

    i2s_channel_disable(mic_handle);
    i2s_del_channel(mic_handle);
    gpio_set_level(LED_GREEN, 0);

    // Analyze
    int num_samples = total_bytes / 2;
    int32_t peak = 0;
    int64_t sum_sq = 0;
    for (int i = 0; i < num_samples; i++) {
        int32_t s = abs(rec_buf[i]);
        if (s > peak) peak = s;
        sum_sq += (int64_t)rec_buf[i] * rec_buf[i];
    }
    int32_t rms = (int32_t)sqrtf((float)sum_sq / num_samples);

    ESP_LOGI(TAG, "  Recorded %d bytes. Peak: %ld/32767, RMS: %ld", total_bytes, peak, rms);

    if (peak < 50) {
        ESP_LOGE(TAG, "  Microphone: FAIL (no signal detected)");
        free(rec_buf);
        return;
    }
    ESP_LOGI(TAG, "  Microphone: PASS");

    // Play it back through speaker
    ESP_LOGI(TAG, "  Playing back recording...");
    gpio_set_level(LED_ORANGE, 1);

    i2s_chan_handle_t spk_handle;
    i2s_chan_config_t spk_chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&spk_chan_cfg, &spk_handle, NULL));

    i2s_std_config_t spk_std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)SPK_SCK_PIN,
            .ws   = (gpio_num_t)SPK_WS_PIN,
            .din  = I2S_GPIO_UNUSED,
            .dout = (gpio_num_t)SPK_SD_PIN,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(spk_handle, &spk_std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(spk_handle));

    size_t bytes_written;
    offset = 0;
    while (offset < total_bytes) {
        int chunk = total_bytes - offset;
        if (chunk > 4096) chunk = 4096;
        ESP_ERROR_CHECK(i2s_channel_write(spk_handle, (uint8_t *)rec_buf + offset,
                                          chunk, &bytes_written, portMAX_DELAY));
        offset += bytes_written;
    }

    // Silence flush
    uint8_t silence[4096] = {0};
    i2s_channel_write(spk_handle, silence, sizeof(silence), &bytes_written, portMAX_DELAY);

    i2s_channel_disable(spk_handle);
    i2s_del_channel(spk_handle);
    free(rec_buf);
    gpio_set_level(LED_ORANGE, 0);

    ESP_LOGI(TAG, "  Playback complete. Verify you heard your voice.");
}

static void test_wifi(void)
{
    ESP_LOGI(TAG, "=== TEST: WiFi ===");
    ESP_LOGI(TAG, "  Connecting to '%s'...", WIFI_SSID);

    // Only init WiFi once
    static bool wifi_initialized = false;
    if (!wifi_initialized) {
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
        wifi_initialized = true;
    }

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                                           pdFALSE, pdTRUE, pdMS_TO_TICKS(10000));
    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "  WiFi: PASS");
    } else {
        ESP_LOGE(TAG, "  WiFi: FAIL (connection timeout)");
    }
}

static void test_button(void)
{
    ESP_LOGI(TAG, "=== TEST: Button (press within 5 seconds) ===");

    // Blink orange while waiting
    for (int i = 0; i < 50; i++) {  // 5 seconds, 100ms intervals
        if (gpio_get_level(BUTTON_PIN) == 0) {
            gpio_set_level(LED_ORANGE, 0);
            ESP_LOGI(TAG, "  Button: PASS (pressed!)");
            // Wait for release
            while (gpio_get_level(BUTTON_PIN) == 0) {
                vTaskDelay(pdMS_TO_TICKS(50));
            }
            return;
        }
        gpio_set_level(LED_ORANGE, (i / 2) % 2);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    gpio_set_level(LED_ORANGE, 0);
    ESP_LOGW(TAG, "  Button: SKIP (not pressed within timeout)");
}

// ---- Main ----
void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    // Init GPIOs
    gpio_config_t btn_cfg = {
        .pin_bit_mask = (1ULL << BUTTON_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&btn_cfg));

    gpio_config_t led_cfg = {
        .pin_bit_mask = (1ULL << LED_ORANGE) | (1ULL << LED_GREEN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&led_cfg));

    int cycle = 0;
    while (1) {
        cycle++;
        ESP_LOGI(TAG, "");
        ESP_LOGI(TAG, "========================================");
        ESP_LOGI(TAG, "  BOARD TEST CYCLE %d", cycle);
        ESP_LOGI(TAG, "  Free heap: %lu bytes", esp_get_free_heap_size());
        ESP_LOGI(TAG, "========================================");

        test_leds();
        vTaskDelay(pdMS_TO_TICKS(500));

        test_speaker();
        vTaskDelay(pdMS_TO_TICKS(500));

        test_mic_and_playback();
        vTaskDelay(pdMS_TO_TICKS(500));

        test_wifi();
        vTaskDelay(pdMS_TO_TICKS(500));

        test_button();
        vTaskDelay(pdMS_TO_TICKS(500));

        ESP_LOGI(TAG, "");
        ESP_LOGI(TAG, "  All tests complete. Restarting in 3 seconds...");
        // Both LEDs solid for 3 seconds to indicate cycle complete
        gpio_set_level(LED_ORANGE, 1);
        gpio_set_level(LED_GREEN, 1);
        vTaskDelay(pdMS_TO_TICKS(3000));
        gpio_set_level(LED_ORANGE, 0);
        gpio_set_level(LED_GREEN, 0);
    }
}
