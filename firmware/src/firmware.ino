// ESP32-S3 + 1.54" mono e-Paper (GxEPD2) — PNG fetch, battery monitor, deep sleep
//
// Display library: GxEPD2  (install via Library Manager: "GxEPD2" by Jean-Marc Zingg)
// Panel:           1.54" 200x200 black/white, controller SSD1681 -> GxEPD2_154_D67
// PNG decoder:     PNGdec by Larry Bank
//
// NOTE: from-scratch rewrite of the original sketch; never run on hardware.
// Pin map verified against Waveshare's ESP32-S3-ePaper-1.54 example code.

#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <GxEPD2_BW.h>
#include <esp_sleep.h>
#include <driver/gpio.h>   // gpio_hold_en / gpio_deep_sleep_hold_en (power latch)
#include <PNGdec.h>

// ===== URGENT SOUND ALERT (optional) =====
// Gated entirely behind the URGENT_URL build flag (set in .env). When unset, no
// audio code compiles and behavior is identical to the plain dashboard.
#ifdef URGENT_URL
#include "audio_bsp.h"
extern const uint8_t urgent_pcm_start[] asm("_binary_src_urgent_pcm_start");
extern const uint8_t urgent_pcm_end[]   asm("_binary_src_urgent_pcm_end");

#ifndef URGENT_VOLUME
#define URGENT_VOLUME 70   // playback volume, percent
#endif

// Play the embedded urgent PCM once, in its own FreeRTOS task: a blocking
// esp_codec_dev_write on the main task trips the watchdog and reboot-loops the
// device. Sets s_urgentDone when finished so the main flow can sleep.
static volatile bool s_urgentDone = false;
static void urgentAudioTask(void *arg) {
  if (audioInit(16000, URGENT_VOLUME)) {
    audioPlayPCM16(urgent_pcm_start, (size_t)(urgent_pcm_end - urgent_pcm_start));
    audioDeinit();   // mute PA + cut audio power (GPIO42 high)
  }
  s_urgentDone = true;
  vTaskDelete(NULL);
}

// GET URGENT_URL; true only if the trimmed body is exactly "true". Any failure
// (begin/HTTP/non-true body) returns false — fail-safe to silence.
static bool urgentFlagSet() {
  HTTPClient http;
  if (!http.begin(URGENT_URL)) return false;
  bool urgent = false;
  if (http.GET() == HTTP_CODE_OK) {
    String body = http.getString();
    body.trim();
    urgent = body.equalsIgnoreCase("true");
  }
  http.end();
  return urgent;
}

// Check the flag and, if set, play the alert and block until it finishes (clip is
// ~2 s). Capped so a stuck codec can never prevent the device from sleeping.
static void playUrgentIfFlagged() {
  if (!urgentFlagSet()) { Serial.println("Urgent: not flagged"); return; }
  Serial.println("Urgent: FLAGGED — playing alert");
  s_urgentDone = false;
  xTaskCreatePinnedToCore(urgentAudioTask, "urgent", 8192, NULL, 5, NULL, 1);
  for (int i = 0; i < 100 && !s_urgentDone; i++) delay(100);   // up to 10 s
}
#endif  // URGENT_URL

// ========== CONFIGURATION ==========
// Config comes from .env, injected as build flags by inject_secrets.py (see flash.sh).
// WIFI_SSID / WIFI_PASSWORD / IMAGE_URL are REQUIRED — the build fails below if they
// aren't provided. Copy .env.example to .env and fill it in (see README).
#ifndef WIFI_SSID
#error "WIFI_SSID not set. Copy .env.example to .env and fill it in (see README)."
#endif
#ifndef WIFI_PASSWORD
#error "WIFI_PASSWORD not set. Copy .env.example to .env and fill it in (see README)."
#endif
#ifndef IMAGE_URL
#error "IMAGE_URL not set. Copy .env.example to .env and fill it in (see README)."
#endif

// Optional — these fall back to sensible defaults if not set in .env.
#ifndef UPDATE_INTERVAL
#define UPDATE_INTERVAL 300   // seconds between refreshes (5 min)
#endif

#define DISPLAY_WIDTH   200
#define DISPLAY_HEIGHT  200
#define MAX_IMAGE_BYTES 50000 // safety cap on download size

// ========== PIN DEFINITIONS ==========
// Verified against Waveshare's user_config.h for the ESP32-S3-ePaper-1.54 board
// (waveshareteam/ESP32-S3-ePaper-1.54, 02_Example/Arduino/07_BATT_PWR_Test).
#define EPAPER_CS    11
#define EPAPER_DC    10
#define EPAPER_RST   9
#define EPAPER_BUSY  8
#define EPAPER_SCK   12
#define EPAPER_MOSI  13
#define EPD_PWR_PIN  6    // EPD power enable — ACTIVE LOW (drive 0 to power the panel)
#define PWR_HOLD_PIN 17   // POWER LATCH hold — ACTIVE HIGH. MUST stay HIGH (incl. deep
                          // sleep, via gpio_hold) or the board powers OFF on battery.
                          // Also gates the battery-sense path. (Was mislabeled VBAT_EN.)
#define BUTTON_PIN   0    // BOOT button — ACTIVE LOW, RTC-capable (deep-sleep wake source)
#define BATTERY_PIN  4    // GPIO4 = ADC1_CH3, behind a 2:1 divider

// GxEPD2: <panel, page-height>. Page height == panel height -> single full-screen page.
GxEPD2_BW<GxEPD2_154_D67, GxEPD2_154_D67::HEIGHT> display(
    GxEPD2_154_D67(EPAPER_CS, EPAPER_DC, EPAPER_RST, EPAPER_BUSY));

// ========== BATTERY CONFIG ==========
// 400mAh LiPo, 100k/100k divider (2:1). ADC reads pin voltage, x2 = battery voltage.
#define BATTERY_CAPACITY 400   // mAh
#define VOLTAGE_DIVIDER  2.0f  // 100k / 100k
#define BATTERY_FULL     4.20f // 100% (4.2V LiPo)
#define BATTERY_EMPTY    3.00f // 0%   (3.0V LiPo)

// ========== BUFFERS ==========
// Grayscale decode target (1 byte/px) and the packed 1-bit framebuffer (8 px/byte).
uint8_t image_buffer[DISPLAY_WIDTH * DISPLAY_HEIGHT];
uint8_t display_buffer[DISPLAY_WIDTH * DISPLAY_HEIGHT / 8];

// Survives deep sleep (lives in RTC memory): true once we've drawn a real image,
// so a later failed fetch can leave the existing image on the panel instead of
// wiping it to a "Failed to fetch" screen. Cleared only on a full power-cycle.
RTC_DATA_ATTR bool g_haveShownImage = false;

// How many times to retry the image fetch within a single wake (WiFi already up)
// before giving up and going back to sleep.
#define FETCH_RETRIES 3

// ========== PNG DECODER ==========
PNG png;

// PNGdec is a streaming decoder: it calls this callback once per scanline.
// We pull each line as RGB565, convert to grayscale, and store into image_buffer.
// The callback must return int (1 = keep decoding) per PNG_DRAW_CALLBACK.
int PNGDraw(PNGDRAW* pDraw) {
  if (pDraw->y >= DISPLAY_HEIGHT) return 1;

  static uint16_t lineRGB565[DISPLAY_WIDTH];
  png.getLineAsRGB565(pDraw, lineRGB565, PNG_RGB565_BIG_ENDIAN, 0xffffffff);

  int w = pDraw->iWidth < DISPLAY_WIDTH ? pDraw->iWidth : DISPLAY_WIDTH;
  int row = pDraw->y * DISPLAY_WIDTH;
  for (int x = 0; x < w; x++) {
    uint16_t p = lineRGB565[x];
    uint8_t r = ((p >> 11) & 0x1F) << 3;
    uint8_t g = ((p >> 5)  & 0x3F) << 2;
    uint8_t b = ( p        & 0x1F) << 3;
    image_buffer[row + x] = (r * 299 + g * 587 + b * 114) / 1000;
  }
  return 1;
}

bool decodePNG(uint8_t* png_data, int png_size) {
  Serial.println("Decoding PNG...");

  // Default to white so a smaller-than-screen image leaves a clean background.
  memset(image_buffer, 0xFF, sizeof(image_buffer));

  int rc = png.openRAM(png_data, png_size, PNGDraw);
  if (rc != PNG_SUCCESS) {
    Serial.printf("PNG open error: %d\n", rc);
    return false;
  }

  if (png.getWidth() > DISPLAY_WIDTH || png.getHeight() > DISPLAY_HEIGHT) {
    Serial.printf("PNG %dx%d larger than %dx%d — clipping\n",
                  png.getWidth(), png.getHeight(), DISPLAY_WIDTH, DISPLAY_HEIGHT);
  }

  rc = png.decode(nullptr, 0);   // invokes PNGDraw per line
  png.close();

  if (rc != PNG_SUCCESS) {
    Serial.printf("PNG decode error: %d\n", rc);
    return false;
  }

  Serial.println("PNG converted to grayscale");
  return true;
}

// ========== GRAYSCALE -> PACKED 1-BIT ==========
// The server sends an already-dithered 1-bit PNG, so there's no on-device
// dithering — just threshold the decoded grayscale into the packed framebuffer.
void packTo1Bit(uint8_t* gray, uint8_t* dst, int width, int height) {
  memset(dst, 0, width * height / 8);
  for (int y = 0; y < height; y++) {
    for (int x = 0; x < width; x++) {
      if (gray[y * width + x] < 128) {          // dark pixel -> black ink (set bit)
        int byte_idx = y * (width / 8) + (x / 8);
        int bit_pos  = 7 - (x % 8);
        dst[byte_idx] |= (1 << bit_pos);
      }
    }
  }
}

// ========== IMAGE FETCHING ==========
bool fetchImage(const char* url) {
  HTTPClient http;
  if (!http.begin(url)) {
    Serial.println("http.begin failed");
    return false;
  }

  int httpCode = http.GET();
  if (httpCode != HTTP_CODE_OK) {
    Serial.printf("HTTP error: %d\n", httpCode);
    http.end();
    return false;
  }

  int contentLen = http.getSize(); // -1 if unknown/chunked
  size_t cap = (contentLen > 0 && contentLen <= MAX_IMAGE_BYTES)
                 ? (size_t)contentLen : MAX_IMAGE_BYTES;

  uint8_t* image_data = (uint8_t*)malloc(cap);
  if (!image_data) {
    Serial.println("malloc failed for image_data");
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  size_t bytes_read = 0;
  unsigned long last_data = millis();

  // Read until we have the whole body, the cap, or a 5s stall.
  while (http.connected() && bytes_read < cap) {
    size_t avail = stream->available();
    if (avail) {
      size_t want = cap - bytes_read;
      if (avail < want) want = avail;
      int n = stream->readBytes(image_data + bytes_read, want);
      if (n <= 0) break;
      bytes_read += n;
      last_data = millis();
    } else {
      if (millis() - last_data > 5000) { Serial.println("read timeout"); break; }
      delay(10);
    }
    if (contentLen > 0 && bytes_read >= (size_t)contentLen) break;
  }
  http.end();

  if (bytes_read < 8) {
    Serial.printf("too few bytes: %u\n", (unsigned)bytes_read);
    free(image_data);
    return false;
  }

  bool decoded = false;
  if (image_data[0] == 0x89 && image_data[1] == 0x50 &&
      image_data[2] == 0x4E && image_data[3] == 0x47) {
    decoded = decodePNG(image_data, bytes_read);
  } else {
    Serial.println("Not a PNG (BMP path removed — server should send PNG)");
  }

  free(image_data);
  return decoded;
}

// ========== BATTERY MONITORING ==========
float readBatteryVoltage() {
  // analogReadMilliVolts() applies the factory ADC calibration — far more accurate
  // than a raw 0-4095 * 3.3/4095 conversion.
  uint32_t mv = analogReadMilliVolts(BATTERY_PIN);
  return (mv / 1000.0f) * VOLTAGE_DIVIDER;
}

float calculateBatteryPercentage(float voltage) {
  if (voltage >= BATTERY_FULL)  return 100.0f;
  if (voltage <= BATTERY_EMPTY) return 0.0f;
  return ((voltage - BATTERY_EMPTY) / (BATTERY_FULL - BATTERY_EMPTY)) * 100.0f;
}

float estimateBatteryLifeDays(float percentage) {
  float active_power_mA = 100.0f;             // mA while awake
  float active_time_s   = 30.0f;              // s of activity per cycle
  float sleep_power_mA  = 0.000015f;          // 15uA in deep sleep
  float sleep_time_s    = UPDATE_INTERVAL;    // s asleep per cycle

  float active_mah = (active_power_mA * active_time_s) / 3600.0f;
  float sleep_mah  = (sleep_power_mA  * sleep_time_s)  / 3600.0f;
  float per_cycle  = active_mah + sleep_mah;

  float cycles_per_day    = 86400.0f / (active_time_s + sleep_time_s);
  float daily_consumption = per_cycle * cycles_per_day;
  float remaining_mah     = (percentage / 100.0f) * BATTERY_CAPACITY;

  return daily_consumption > 0 ? remaining_mah / daily_consumption : 0.0f;
}

// ========== DISPLAY HELPERS (drawn inside the GxEPD2 page loop) ==========
void drawText(int16_t x, int16_t y, const String& s, uint8_t textSize, uint16_t color) {
  display.setTextColor(color);
  display.setTextSize(textSize);
  display.setCursor(x, y);
  display.print(s);
}

// Fraction of black (ink) pixels in the packed framebuffer over a rect.
float regionDarkness(int x0, int y0, int w, int h) {
  long black = 0, total = 0;
  for (int y = y0; y < y0 + h; y++) {
    if (y < 0 || y >= DISPLAY_HEIGHT) continue;
    for (int x = x0; x < x0 + w; x++) {
      if (x < 0 || x >= DISPLAY_WIDTH) continue;
      int byte_idx = y * (DISPLAY_WIDTH / 8) + (x / 8);
      int bit_pos  = 7 - (x % 8);
      black += (display_buffer[byte_idx] >> bit_pos) & 1;
      total++;
    }
  }
  return total ? (float)black / total : 0.0f;
}

void drawBatteryIndicator(float percentage, bool overImage) {
  // Battery percentage, top-right. Pick colors from the image underneath so it
  // stays legible: dark area -> black pad + white text, light area -> the reverse.
  String pct = String((int)percentage) + "%";
  const int textSize = 2;
  int w = pct.length() * 6 * textSize;   // default GFX font advances 6px/char
  int h = 8 * textSize;
  int x = DISPLAY_WIDTH - w - 4;
  int y = 4;
  int px = x - 2, py = y - 2, pw = w + 4, ph = h + 4;

  bool dark = overImage && regionDarkness(px, py, pw, ph) > 0.5f;
  uint16_t bg = dark ? GxEPD_BLACK : GxEPD_WHITE;
  uint16_t fg = dark ? GxEPD_WHITE : GxEPD_BLACK;

  display.fillRect(px, py, pw, ph, bg);
  drawText(x, y, pct, textSize, fg);
}

void drawBatteryWarning(float percentage) {
  if (percentage < 20) {
    drawText(40, 150, "LOW BATTERY!", 2, GxEPD_BLACK);
    drawText(40, 172, "Charge soon!", 1, GxEPD_BLACK);
  }
}

// Push the image plus battery overlay in one paged refresh, so both actually
// land on the panel (the original re-pushed the image and lost the overlay).
void renderScreen(bool haveImage, float percentage) {
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    if (haveImage) {
      display.drawBitmap(0, 0, display_buffer, DISPLAY_WIDTH, DISPLAY_HEIGHT, GxEPD_BLACK);
    } else {
      drawText(10, 20, "Failed to fetch", 2, GxEPD_BLACK);
    }
    drawBatteryIndicator(percentage, haveImage);
    drawBatteryWarning(percentage);
  } while (display.nextPage());
}

// ========== POWER ==========
void enterDeepSleep(int seconds) {
  Serial.printf("Sleeping %d s (button wake enabled)...\n", seconds);
  display.hibernate();                 // panel into deep sleep
  digitalWrite(EPD_PWR_PIN, HIGH);     // cut panel power (active-low enable -> off)

  // CRITICAL: keep the power latch (GPIO17) HIGH through deep sleep. A plain
  // digitalWrite won't survive sleep — the pad must be latched with gpio_hold,
  // or the rail collapses and the board powers OFF on battery (never waking).
  digitalWrite(PWR_HOLD_PIN, HIGH);
  gpio_hold_en((gpio_num_t)PWR_HOLD_PIN);
  gpio_deep_sleep_hold_en();

  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  // GPIO0 is RTC-capable, so use ext1 for deep-sleep wake. The BOOT button idles
  // HIGH and pulls LOW when pressed -> wake when the pin goes low.
  esp_sleep_enable_ext1_wakeup(1ULL << BUTTON_PIN, ESP_EXT1_WAKEUP_ANY_LOW);

  WiFi.disconnect(true);
  delay(100);
  esp_deep_sleep_start();
}

// ========== SETUP ==========
void setup() {
  // POWER LATCH — must be the very first thing: the PWR button only supplies
  // momentary power, so GPIO17 must be driven HIGH immediately to hold the rail,
  // or the board dies before we get anywhere (on battery). On a timer wake the
  // pad was held HIGH through deep sleep (gpio_hold) — release the hold so we can
  // actively drive it again, then keep it HIGH.
  gpio_hold_dis((gpio_num_t)PWR_HOLD_PIN);
  pinMode(PWR_HOLD_PIN, OUTPUT);
  digitalWrite(PWR_HOLD_PIN, HIGH);

  Serial.begin(115200);
  delay(1000);

  Serial.println("\n=== ESP32-S3 e-Paper + Battery ===");
  Serial.printf("Battery: %d mAh\n", BATTERY_CAPACITY);

  pinMode(BUTTON_PIN, INPUT_PULLUP);   // BOOT button is active-low

  // EPD enable is ACTIVE-LOW. (PWR_HOLD_PIN already driven HIGH at top of setup.)
  pinMode(EPD_PWR_PIN, OUTPUT);
  digitalWrite(EPD_PWR_PIN, LOW);      // power the panel ON
  delay(10);

  analogSetPinAttenuation(BATTERY_PIN, ADC_11db);  // ~0-3.1V at pin (0-6.2V battery via 2:1)

  // SPI on the configured pins, then bring up the panel.
  SPI.begin(EPAPER_SCK, -1, EPAPER_MOSI, EPAPER_CS);
  display.init(115200);
  display.setRotation(0);

  switch (esp_sleep_get_wakeup_cause()) {
    case ESP_SLEEP_WAKEUP_EXT1:  Serial.println("Woke from BUTTON"); break;
    case ESP_SLEEP_WAKEUP_TIMER: Serial.println("Woke from TIMER");  break;
    default:                     Serial.println("First boot");       break;
  }

  WiFi.mode(WIFI_STA);
  Serial.print("WiFi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  // Cap TX power well below the ~20dBm default: the radio's transmit current
  // spike is what sags the small LiPo into brownout on battery (works fine on
  // USB). 8.5dBm roughly halves that peak; bump up if the AP is far/marginal.
  WiFi.setTxPower(WIFI_POWER_8_5dBm);
  int timeout = 0;
  while (WiFi.status() != WL_CONNECTED && timeout < 150) { // ~15s
    delay(100);
    Serial.print(".");
    timeout++;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(" failed!");
    enterDeepSleep(UPDATE_INTERVAL);   // does not return
  }
  Serial.println(" connected!");
}

// ========== MAIN LOOP (runs once, then deep-sleeps) ==========
void loop() {
  Serial.println("\n=== Update cycle ===");

  float voltage    = readBatteryVoltage();
  float percentage = calculateBatteryPercentage(voltage);
  float days       = estimateBatteryLifeDays(percentage);
  Serial.printf("Battery: %.2fV (%.1f%%) ~%.1f days\n", voltage, percentage, days);

  bool haveImage = false;
  for (int attempt = 1; attempt <= FETCH_RETRIES && !haveImage; attempt++) {
    Serial.printf("Fetch attempt %d/%d\n", attempt, FETCH_RETRIES);
    haveImage = fetchImage(IMAGE_URL);
    if (!haveImage && attempt < FETCH_RETRIES) delay(500);
  }

  if (haveImage) {
    Serial.println("Image fetched");
    packTo1Bit(image_buffer, display_buffer, DISPLAY_WIDTH, DISPLAY_HEIGHT);
    renderScreen(true, percentage);
    g_haveShownImage = true;
    Serial.println("Displayed");
  } else if (!g_haveShownImage) {
    // Nothing good has ever been on the panel — show the failure screen once so
    // the device isn't just blank on first boot.
    Serial.println("Fetch failed (no prior image) — showing failure screen");
    renderScreen(false, percentage);
  } else {
    // We already have a valid image on the panel; don't wipe it for a transient
    // miss. Leave it as-is (e-paper holds the last frame) and try again next wake.
    Serial.println("Fetch failed — keeping last good image");
  }

#ifdef URGENT_URL
  // Display is already up to date; check the urgent flag and beep if set. Runs
  // while WiFi is still connected and before sleep cuts it.
  playUrgentIfFlagged();
#endif

  enterDeepSleep(UPDATE_INTERVAL);   // does not return
}
