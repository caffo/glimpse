# Urgent-flag Sound Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each wake cycle, check an "urgent" flag URL; if true, play an embedded sound through the board's ES8311 codec (display unchanged).

**Architecture:** Hand-roll a minimal ES8311 driver (I2C control + IDF `i2s_std` audio) wrapped in a tiny `audioInit/audioPlayPCM16/audioDeinit` API — staying on the pinned `espressif32@6.9.0` platform (no framework, no platform switch). The urgent sound is converted to mono 16 kHz 16-bit PCM and embedded in flash via the linker. `loop()` fetches the flag alongside the image and plays the embedded PCM when the flag is `true`. The whole feature is gated behind a `URGENT_URL` build flag.

**Tech Stack:** PlatformIO + arduino-esp32 3.20017 (IDF 5.1), ESP32-S3, GxEPD2, HTTPClient, IDF `i2s_std` + Wire (ES8311), ffmpeg (build-prep only).

## Global Constraints

- PlatformIO env: `esp32-s3-epaper-154` (board esp32-s3-devkitc-1, 8 MB flash, `default_8MB.csv`).
- **Do not change `platform = espressif32@6.9.0`.** The firmware is validated on it.
- Keep the GPIO17 power latch held through deep sleep (already implemented) — do not touch it.
- Audio power enable is **GPIO42**, active HIGH; turn it OFF before deep sleep.
- ES8311 profile pins: i2c sda 47 / scl 48; i2c addr 0x18; i2s bclk 15 / ws 38 / dout 45 / din 16 / mclk 14; pa 46; use_mclk; format 16-bit mono.
- Urgent sound playback format: **16-bit mono @ 16000 Hz**.
- Volume default: `URGENT_VOLUME = 70` (percent).
- Feature is **optional**: if `URGENT_URL` is not defined at build time, no audio code compiles and behavior is identical to today.
- Fail-safe: any `urgent.txt` fetch failure or non-`true` body ⇒ no sound. Audio init failure ⇒ log and skip; never block the sleep/wake cycle.
- Flashing uses the established recipe (sealed LiPo + native USB): enter download mode (hold BOOT, power on with PWR), then flash app-only with **no post-reset** using PlatformIO's Python. See memory `flashing-recipe`.

**Flash + verify helper (used by hardware verification steps below):**

```bash
# 1) Build
cd <repo>/firmware && set -a && source .env && set +a && pio run -e esp32-s3-epaper-154
# 2) Download mode: hold PWR ~4s (off) -> hold BOOT -> press PWR (on) -> release BOOT.
# 3) Flash app-only, no post-reset (waits for a stable port):
PY=~/.platformio/penv/bin/python; ESPTOOL=~/.platformio/packages/tool-esptoolpy/esptool.py
FW=.pio/build/esp32-s3-epaper-154/firmware.bin
for i in $(seq 1 300); do p=$(ls /dev/cu.usbmodem* 2>/dev/null|grep -v debug-console|head -1);
  if [ -n "$p" ]; then sleep 2; if [ -e "$p" ]; then
    "$PY" "$ESPTOOL" --chip esp32s3 --port "$p" --baud 460800 --before default_reset --after no_reset \
      write_flash 0x10000 "$FW" && break; fi; fi; sleep 0.5; done
# 4) Power-cycle normally (PWR off -> PWR on, no BOOT) to run it.
```

---

### Task 1: Audio asset pipeline + flash embed

**Files:**
- Create: `tools/wav_to_pcm.sh`
- Create (generated, committed): `src/urgent.pcm`
- Modify: `platformio.ini` (add `board_build.embed_files`)

**Status:** COMPLETE (commit `1aca357`). Symbols: `_binary_src_urgent_pcm_start` / `_binary_src_urgent_pcm_end`; size 65280 bytes. (Kept here for reference; do not redo.)

---

### Task 2: Minimal ES8311 driver + I2S playback wrapper

> Approach decision (2026-06-18): we do NOT vendor the `esp_codec_dev` framework — it requires a newer IDF than `espressif32@6.9.0` and would force a risky platform switch. Hand-roll a minimal ES8311 driver over I2C + the IDF `i2s_std` driver instead, staying on the proven platform.

**Files:**
- Create: `lib/audio/audio_bsp.h`
- Create: `lib/audio/es8311_min.h`
- Create: `lib/audio/es8311_min.cpp`
- Create: `lib/audio/audio_bsp.cpp`
- Modify (only if include path needed): `platformio.ini`

**Interfaces:**
- Produces:
  - `bool audioInit(int sampleRate, int volumePct);` — powers GPIO42, brings up I2S (master, MCLK), inits ES8311 over I2C, enables PA, sets volume. Returns false on failure.
  - `void audioPlayPCM16(const uint8_t* data, size_t bytes);` — blocking I2S write of 16-bit mono PCM.
  - `void audioDeinit(void);` — mutes PA, powers down codec, stops I2S, drops GPIO42 low.

**Reference material on disk (use it — do not re-fetch the framework):**
- `/tmp/audio_ref/es8311/es8311.c` (+ `es8311_reg.h`) — the canonical, board-validated ES8311 register sequence. Port the DAC/output path from here; replace its abstract I2C calls with direct `Wire` writes to addr `0x18`.
- `/tmp/audio_ref/board_cfg.h` — pin reference.

**Hardware constants (verbatim):** I2C SDA 47 / SCL 48; ES8311 I2C addr `0x18`; I2S MCLK 14, BCLK 15, WS 38, DOUT 45 (DIN unused); PA enable GPIO 46 (active HIGH); audio power GPIO 42 (active HIGH); MCLK = 256× sample rate; 16-bit mono.

- [ ] **Step 1: Write the wrapper header**

Create `lib/audio/audio_bsp.h`:
```c
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#ifdef __cplusplus
extern "C" {
#endif
bool audioInit(int sampleRate, int volumePct);          // power + init ES8311 (mono 16-bit); vol 0..100
void audioPlayPCM16(const uint8_t *data, size_t bytes);  // blocking I2S write
void audioDeinit(void);                                   // mute PA, power down, stop I2S, GPIO42 low
#ifdef __cplusplus
}
#endif
```

- [ ] **Step 2: Write the ES8311 register driver**

Create `lib/audio/es8311_min.h`:
```cpp
#pragma once
#include <stdint.h>
#include <stdbool.h>
// Minimal ES8311 control over I2C (Wire, addr 0x18). MCLK supplied externally by I2S.
// Wire.begin(47,48) is done by the caller (audioInit) before es8311_min_init.
bool es8311_min_init(uint32_t sample_rate);   // false if the chip doesn't ACK
void es8311_min_set_volume(int pct);          // 0..100 -> ES8311 DAC volume (reg 0x32)
void es8311_min_powerdown(void);
```

Create `lib/audio/es8311_min.cpp`: implement with `<Wire.h>`. Port the **output/DAC init register sequence** from `/tmp/audio_ref/es8311/es8311.c` — specifically the paths in `es8311_codec_open`, `es8311_set_bits_per_sample`, `es8311_config_fmt`, and the `get_coeff(mclk, sample_rate)` clock-coefficient table, with `mclk = 256 * sample_rate`. Write registers directly:
```cpp
static void wr(uint8_t reg, uint8_t val){ Wire.beginTransmission(0x18); Wire.write(reg); Wire.write(val); Wire.endTransmission(); }
static uint8_t rd(uint8_t reg){ Wire.beginTransmission(0x18); Wire.write(reg); Wire.endTransmission(false); Wire.requestFrom(0x18,1); return Wire.available()?Wire.read():0; }
```
`es8311_min_init` returns false if the reset-register readback doesn't ACK/match (probe `Wire.endTransmission()` result on addr 0x18 first). `es8311_min_set_volume` writes reg `0x32` = `pct*255/100`. `es8311_min_powerdown` follows the reference `es8311_codec_close`/standby path. Keep it self-contained — no esp_codec_dev / codec_board includes.

- [ ] **Step 3: Write the playback wrapper**

Create `lib/audio/audio_bsp.cpp`:
```cpp
#include "audio_bsp.h"
#include "es8311_min.h"
#include <Arduino.h>
#include <Wire.h>
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

#define AUDIO_PWR_PIN 42
#define PA_PIN        46
#define I2C_SDA       47
#define I2C_SCL       48
static const char *TAG = "audio_bsp";
static i2s_chan_handle_t s_tx = NULL;

bool audioInit(int sampleRate, int volumePct) {
  pinMode(AUDIO_PWR_PIN, OUTPUT); digitalWrite(AUDIO_PWR_PIN, HIGH);   // power the audio section
  vTaskDelay(pdMS_TO_TICKS(50));
  Wire.begin(I2C_SDA, I2C_SCL);

  // I2S TX (master, provides MCLK to the codec).
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  if (i2s_new_channel(&chan_cfg, &s_tx, NULL) != ESP_OK) { ESP_LOGE(TAG, "i2s chan"); return false; }
  i2s_std_config_t std_cfg = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG((uint32_t)sampleRate),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
    .gpio_cfg = {
      .mclk = GPIO_NUM_14, .bclk = GPIO_NUM_15, .ws = GPIO_NUM_38,
      .dout = GPIO_NUM_45, .din = I2S_GPIO_UNUSED,
      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
    },
  };
  if (i2s_channel_init_std_mode(s_tx, &std_cfg) != ESP_OK) { ESP_LOGE(TAG, "i2s std"); return false; }
  if (i2s_channel_enable(s_tx) != ESP_OK) { ESP_LOGE(TAG, "i2s en"); return false; }

  if (!es8311_min_init((uint32_t)sampleRate)) { ESP_LOGE(TAG, "es8311 no ack"); return false; }
  es8311_min_set_volume(volumePct);

  pinMode(PA_PIN, OUTPUT); digitalWrite(PA_PIN, HIGH);   // enable amp last (reduce pop)
  vTaskDelay(pdMS_TO_TICKS(10));
  return true;
}

void audioPlayPCM16(const uint8_t *data, size_t bytes) {
  if (!s_tx) return;
  size_t written = 0;
  i2s_channel_write(s_tx, data, bytes, &written, portMAX_DELAY);
}

void audioDeinit(void) {
  digitalWrite(PA_PIN, LOW);                  // mute amp first
  es8311_min_powerdown();
  if (s_tx) { i2s_channel_disable(s_tx); i2s_del_channel(s_tx); s_tx = NULL; }
  digitalWrite(AUDIO_PWR_PIN, LOW);           // cut audio power before sleep
}
```

- [ ] **Step 4: Build to verify it compiles/links**

PlatformIO only compiles `lib/audio` if it is `#include`d. TEMPORARILY add to `src/firmware.ino` (top): `#include "audio_bsp.h"` and inside `setup()` a guarded ref: `if (false) { audioInit(16000, 70); audioPlayPCM16(nullptr, 0); audioDeinit(); }`. Then:
```bash
set -a && source .env && set +a && pio run -e esp32-s3-epaper-154
```
Expected: SUCCESS with RAM/Flash printed. If `audio_bsp.h` isn't found, add `-Ilib/audio` to `platformio.ini` `build_flags`. **Then remove the temporary `#include` and the guarded ref** so `src/firmware.ino` has no diff (Task 3 adds the real call). Confirm with `git diff --stat src/firmware.ino` (empty).

- [ ] **Step 5: Commit**

```bash
git add lib/audio platformio.ini
git commit -m "Add minimal ES8311 driver and I2S playback wrapper"
```

---

### Task 3: On-device audio smoke test (prove playback)

**Files:**
- Modify: `src/firmware.ino` (temporary smoke-test call in `setup()`; removed in Task 5)

**Interfaces:**
- Consumes: `audioInit/audioPlayPCM16/audioDeinit` (Task 2); embed symbols `_binary_src_urgent_pcm_start` / `_binary_src_urgent_pcm_end` (Task 1).

- [ ] **Step 1: Add a temporary smoke test at end of `setup()`**

In `src/firmware.ino`, add near the top (after includes):
```cpp
#include "audio_bsp.h"
extern const uint8_t urgent_pcm_start[] asm("_binary_src_urgent_pcm_start");
extern const uint8_t urgent_pcm_end[]   asm("_binary_src_urgent_pcm_end");
```
At the very end of `setup()`, add:
```cpp
  // TEMP smoke test (remove in Task 5): play the urgent sound once on boot.
  Serial.println("[smoke] playing urgent sound...");
  if (audioInit(16000, 70)) {
    audioPlayPCM16(urgent_pcm_start, (size_t)(urgent_pcm_end - urgent_pcm_start));
    audioDeinit();
    Serial.println("[smoke] done");
  } else {
    Serial.println("[smoke] audioInit failed");
  }
```

- [ ] **Step 2: Build**

Run: `set -a && source .env && set +a && pio run -e esp32-s3-epaper-154`
Expected: SUCCESS.

- [ ] **Step 3: Flash and verify audio on hardware**

Flash using the helper at the top of this plan, then power-cycle normally.
Expected: the speaker plays `urgent.wav` (~2 s) once on boot; serial shows `[smoke] playing...` then `[smoke] done`. If silent: re-check the ES8311 register port vs `/tmp/audio_ref/es8311/es8311.c`, that GPIO42 and PA(46) went HIGH, the I2S pins (14/15/38/45), and the `nm` symbol names.

- [ ] **Step 4: Commit (checkpoint)**

```bash
git add src/firmware.ino
git commit -m "TEMP: on-device audio smoke test"
```

---

### Task 4: Urgent flag fetch + config

**Files:**
- Modify: `.env` (add `URGENT_URL`)
- Modify: `.env.example` (document `URGENT_URL`)
- Modify: `inject_secrets.py` (optional quoted key)
- Modify: `src/firmware.ino` (add `fetchUrgent()`)

**Interfaces:**
- Produces: `bool fetchUrgent(const char *url);` — GET `url`, trim, case-insensitive compare to `"true"`; returns false on any error/non-true. Compiled only when `URGENT_URL` is defined.

- [ ] **Step 1: Add config**

Append to `.env`:
```
# URL returning "true"/"false" — when true, the device plays the urgent sound.
URGENT_URL=https://dash.example.com/urgent.txt
```
Append to `.env.example`:
```
# Optional: URL returning "true"/"false" to trigger the urgent sound. Omit to disable.
URGENT_URL=https://your-server.com/urgent.txt
```

- [ ] **Step 2: Make `inject_secrets.py` inject it as an optional string**

In `inject_secrets.py`, add after the `NUMERIC_KEYS` loop:
```python
OPTIONAL_STR_KEYS = ("URGENT_URL",)
for key in OPTIONAL_STR_KEYS:
    value = lookup(key)
    if value:
        env.Append(CPPDEFINES=[(key, env.StringifyMacro(value))])
        print("config: injected %s" % key)
    else:
        print("config: %s not set, feature disabled" % key)
```

- [ ] **Step 3: Implement `fetchUrgent()`**

In `src/firmware.ino`, add (guarded) above `loop()`:
```cpp
#ifdef URGENT_URL
// GET the urgent flag. True only if the trimmed body equals "true" (case-insensitive).
// Any failure -> false (fail-safe: never a false alarm).
bool fetchUrgent(const char *url) {
  HTTPClient http;
  if (!http.begin(url)) return false;
  int code = http.GET();
  if (code != HTTP_CODE_OK) { http.end(); return false; }
  String body = http.getString();
  http.end();
  body.trim();
  body.toLowerCase();
  return body == "true";
}
#endif
```

- [ ] **Step 4: Temporarily log the flag in `loop()`**

Just after the battery print in `loop()`:
```cpp
#ifdef URGENT_URL
  Serial.printf("urgent flag: %s\n", fetchUrgent(URGENT_URL) ? "TRUE" : "false");
#endif
```

- [ ] **Step 5: Build, flash, verify against the server**

Build + flash (helper). With server `urgent.txt = false`, serial shows `urgent flag: false`. Change to `true`, power-cycle, serial shows `urgent flag: TRUE`.

- [ ] **Step 6: Commit**

```bash
git add .env.example inject_secrets.py src/firmware.ino
git commit -m "Add urgent flag fetch and optional URGENT_URL config"
```
(`.env` is gitignored — not committed.)

---

### Task 5: Wire playback into the cycle, gate the feature, finalize

**Files:**
- Modify: `src/firmware.ino` (remove smoke test + temp log; add real integration)
- Modify: `README.md`

**Interfaces:**
- Consumes: `fetchUrgent()` (Task 4), `audioInit/audioPlayPCM16/audioDeinit` (Task 2), embed symbols (Task 1).

- [ ] **Step 1: Remove the Task 3 smoke test**

Delete the TEMP smoke-test block at the end of `setup()` and the temp `urgent flag:` log line from `loop()` (Task 4 Step 4). Keep `#include "audio_bsp.h"` and the two `extern ... asm(...)` symbol declarations, moved inside `#ifdef URGENT_URL`.

- [ ] **Step 2: Guard the audio includes/symbols**

Ensure the top of `src/firmware.ino` has:
```cpp
#ifdef URGENT_URL
#include "audio_bsp.h"
extern const uint8_t urgent_pcm_start[] asm("_binary_src_urgent_pcm_start");
extern const uint8_t urgent_pcm_end[]   asm("_binary_src_urgent_pcm_end");
#endif
```

- [ ] **Step 3: Add the play step in `loop()`**

Fetch the flag while WiFi is up; play after rendering, before sleep:
```cpp
#ifdef URGENT_URL
  bool urgent = fetchUrgent(URGENT_URL);
#endif

  // ... existing renderScreen(...) logic stays here ...

#ifdef URGENT_URL
  if (urgent) {
    Serial.println("URGENT: playing alert sound");
    if (audioInit(16000, URGENT_VOLUME)) {
      audioPlayPCM16(urgent_pcm_start, (size_t)(urgent_pcm_end - urgent_pcm_start));
      audioDeinit();
    } else {
      Serial.println("URGENT: audioInit failed, skipping");
    }
  }
#endif

  enterDeepSleep(UPDATE_INTERVAL);
```
Add near the other `#define`s at the top:
```cpp
#ifndef URGENT_VOLUME
#define URGENT_VOLUME 70
#endif
```

- [ ] **Step 4: Verify the feature ON (build + flash + hardware)**

Build + flash. With server `urgent.txt = true`: each wake renders the image then plays the sound; serial shows `URGENT: playing alert sound`. With `false`: image updates, silence.

- [ ] **Step 5: Verify the feature compiles OUT when unset**

Comment `URGENT_URL` in `.env`, then:
```bash
set -a && source .env && set +a && pio run -e esp32-s3-epaper-154 2>&1 | grep -iE 'URGENT_URL|SUCCESS|error'
```
Expected: `config: URGENT_URL not set, feature disabled` and a clean build. Restore `URGENT_URL` afterward.

- [ ] **Step 6: Update README**

Add a short "Urgent sound alert" section to `README.md`: what it does, how to set the sound (`tools/wav_to_pcm.sh` then rebuild), the `URGENT_URL` env var, that it's optional, and that latency = up to one refresh interval.

- [ ] **Step 7: Commit**

```bash
git add src/firmware.ino README.md
git commit -m "Play urgent sound when flag is set; gate behind URGENT_URL"
```

---

## Notes for the implementer

- **Hardware-in-the-loop, not host tests.** Verification = a clean build plus observing serial/audio on the device via the flash helper. Treat each task's verify step as its gate.
- **Flashing is fiddly.** Sealed LiPo + native USB; use the download-mode + no-reset recipe. Plain `pio run -t upload` fails at the post-flash reset.
- **Do not disturb the GPIO17 power latch** (separate, already-shipped fix) and **do not change the platform** off `espressif32@6.9.0`.
- **ES8311 bring-up is the risk.** Port the register sequence from `/tmp/audio_ref/es8311/es8311.c` rather than inventing values; verify the chip ACKs at I2C 0x18 before trusting playback.
