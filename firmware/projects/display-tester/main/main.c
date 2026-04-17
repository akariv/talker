/**
 * Display Tester — 2.9" BWR e-ink (SSD1680) board-bring-up firmware.
 *
 * Runs five staged fill patterns on loop so you can visually verify
 * wiring, SPI timing, and both color planes. See display-plan.md.
 *
 * Stages: white → black → red → vertical stripes → border+block.
 */

#include <stdbool.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_log.h"
#include "esp_err.h"

static const char *TAG = "display_tester";

// ---- Panel geometry (SSD1680, 128×296 tri-color) ----
#define PANEL_W       128
#define PANEL_H       296
#define ROW_BYTES     (PANEL_W / 8)         // 16
#define PLANE_BYTES   (ROW_BYTES * PANEL_H) // 4736

// ---- Pin map (per display-plan.md) ----
#define PIN_CLK       GPIO_NUM_18
#define PIN_MOSI      GPIO_NUM_23
#define PIN_CS        GPIO_NUM_5
#define PIN_DC        GPIO_NUM_17
#define PIN_RES       GPIO_NUM_16
#define PIN_BUSY      GPIO_NUM_4

#define SPI_HOST      SPI3_HOST
#define SPI_CLOCK_HZ  (4 * 1000 * 1000)

static spi_device_handle_t s_spi;

// ------------------------------------------------------------------
// Low-level SSD1680 driver
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
        .spics_io_num = -1, // manual CS
        .queue_size = 1,
    };
    ESP_ERROR_CHECK(spi_bus_add_device(SPI_HOST, &dev, &s_spi));
}

static void wait_busy(void)
{
    // SSD1680 pulls BUSY high during refresh; low means ready.
    const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(30000);
    while (gpio_get_level(PIN_BUSY) == 1) {
        if (xTaskGetTickCount() > deadline) {
            ESP_LOGW(TAG, "BUSY still high after 30s — check wiring");
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

static void reset_panel(void)
{
    gpio_set_level(PIN_RES, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(PIN_RES, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(PIN_RES, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
    wait_busy();
}

static void spi_tx(const uint8_t *buf, size_t len)
{
    if (len == 0) return;
    spi_transaction_t t = {
        .length = len * 8,
        .tx_buffer = buf,
    };
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

static void write_data1(uint8_t b)
{
    write_data(&b, 1);
}

static void set_ram_addr(void)
{
    write_cmd(0x4E);
    write_data1(0x00);
    write_cmd(0x4F);
    uint8_t y0[] = {0x00, 0x00};
    write_data(y0, sizeof(y0));
}

static void display_init(void)
{
    reset_panel();

    write_cmd(0x12); // SW reset
    wait_busy();

    write_cmd(0x01); // driver output: 296 lines, gate scan dir
    uint8_t driver[] = {0x27, 0x01, 0x00};
    write_data(driver, sizeof(driver));

    write_cmd(0x11); // data entry: X inc, Y inc
    write_data1(0x03);

    write_cmd(0x44); // RAM X start/end (bytes 0..15)
    uint8_t rx[] = {0x00, 0x0F};
    write_data(rx, sizeof(rx));

    write_cmd(0x45); // RAM Y start/end (0..295)
    uint8_t ry[] = {0x00, 0x00, 0x27, 0x01};
    write_data(ry, sizeof(ry));

    write_cmd(0x3C); // border waveform
    write_data1(0x05);

    write_cmd(0x18); // temperature sensor: internal
    write_data1(0x80);

    set_ram_addr();
    wait_busy();
}

static void display_write_planes(const uint8_t *black, const uint8_t *red)
{
    set_ram_addr();
    write_cmd(0x24); // B/W plane
    write_data(black, PLANE_BYTES);

    set_ram_addr();
    write_cmd(0x26); // RED plane
    write_data(red, PLANE_BYTES);
}

static void display_refresh(void)
{
    write_cmd(0x22);
    write_data1(0xF7); // full update with display on
    write_cmd(0x20);   // master activation
    wait_busy();
}

// ------------------------------------------------------------------
// Framebuffer helpers
// ------------------------------------------------------------------

static void fill(uint8_t *plane, uint8_t byte)
{
    memset(plane, byte, PLANE_BYTES);
}

static void fill_stripes(uint8_t *plane)
{
    for (int i = 0; i < PLANE_BYTES; i++) {
        plane[i] = (i & 1) ? 0x55 : 0xAA;
    }
}

static void set_pixel(uint8_t *plane, int x, int y, bool set)
{
    if (x < 0 || x >= PANEL_W || y < 0 || y >= PANEL_H) return;
    int idx = y * ROW_BYTES + (x >> 3);
    uint8_t mask = 0x80 >> (x & 7);
    // Planes are "set = 1 means color off" for black (1 = white paper).
    // We invert here so callers can pass `true` to mean "draw the mark".
    if (set) plane[idx] &= ~mask;
    else     plane[idx] |= mask;
}

static void build_stage5(uint8_t *black, uint8_t *red)
{
    // Start from white on both planes.
    fill(black, 0xFF);
    fill(red, 0xFF);

    // Black: 2-pixel border rectangle.
    for (int t = 0; t < 2; t++) {
        for (int x = 0; x < PANEL_W; x++) {
            set_pixel(black, x, t, true);
            set_pixel(black, x, PANEL_H - 1 - t, true);
        }
        for (int y = 0; y < PANEL_H; y++) {
            set_pixel(black, t, y, true);
            set_pixel(black, PANEL_W - 1 - t, y, true);
        }
    }

    // Red: 32×32 filled block near center.
    const int bx = (PANEL_W - 32) / 2;
    const int by = (PANEL_H - 32) / 2;
    for (int y = by; y < by + 32; y++) {
        for (int x = bx; x < bx + 32; x++) {
            set_pixel(red, x, y, true);
        }
    }
}

// ------------------------------------------------------------------
// Test stages
// ------------------------------------------------------------------

static void run_stage(int n, const char *name,
                      uint8_t *black, uint8_t *red)
{
    ESP_LOGI(TAG, "=== STAGE %d: %s ===", n, name);
    display_write_planes(black, red);
    display_refresh();
    ESP_LOGI(TAG, "  refresh complete — hold for inspection");
    vTaskDelay(pdMS_TO_TICKS(3000));
}

// ------------------------------------------------------------------
// app_main
// ------------------------------------------------------------------

void app_main(void)
{
    ESP_LOGI(TAG, "Display tester booting");
    ESP_LOGI(TAG, "Panel %dx%d, plane=%d bytes", PANEL_W, PANEL_H, PLANE_BYTES);

    gpio_setup();
    spi_setup();

    uint8_t *black_plane = heap_caps_malloc(PLANE_BYTES, MALLOC_CAP_8BIT);
    uint8_t *red_plane   = heap_caps_malloc(PLANE_BYTES, MALLOC_CAP_8BIT);
    if (!black_plane || !red_plane) {
        ESP_LOGE(TAG, "Failed to allocate plane buffers");
        return;
    }

    while (1) {
        ESP_LOGI(TAG, "---- new loop: initializing display ----");
        display_init();

        fill(black_plane, 0xFF); fill(red_plane, 0xFF);
        run_stage(1, "blank white", black_plane, red_plane);

        fill(black_plane, 0x00); fill(red_plane, 0xFF);
        run_stage(2, "full black", black_plane, red_plane);

        fill(black_plane, 0xFF); fill(red_plane, 0x00);
        run_stage(3, "full red", black_plane, red_plane);

        fill_stripes(black_plane); fill(red_plane, 0xFF);
        run_stage(4, "stripes (AA/55)", black_plane, red_plane);

        build_stage5(black_plane, red_plane);
        run_stage(5, "border + red block", black_plane, red_plane);

        ESP_LOGI(TAG, "---- loop complete, restarting in 10s ----");
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}
