# Urgent-flag sound alert — design

**Date:** 2026-06-18
**Status:** Approved (pending spec review)
**Repo:** m5s3dash (ESP32-S3 e-paper dashboard firmware)

## Goal

On each wake cycle, in addition to fetching the dashboard image, the device checks
an "urgent" flag URL. If the flag is true, the device plays an embedded sound
through its onboard ES8311 codec/speaker. The displayed image is not changed
(sound only).

## Product decisions (settled)

- **Trigger:** level-triggered. Play on **every** wake while `urgent.txt` reads
  `true`. No cross-sleep state tracking. (Server clears the flag to stop alerts.)
- **Sound delivery:** the sound is **embedded in the firmware** (converted to
  mono 16 kHz 16-bit PCM at build prep time). No per-alert download, no
  filesystem — the alert works with no network dependency beyond the flag check.
- **Visual:** none. The dashboard image is untouched when urgent.
- **Latency (accepted):** an alert fires at the next wake, i.e. up to one refresh
  interval (default 5 min) after the flag flips true. Inherent to the deep-sleep
  architecture.

## Hardware (Waveshare ESP32-S3-ePaper-1.54)

Confirmed from the vendor repo (`waveshareteam/ESP32-S3-ePaper-1.54`,
`02_Example/Arduino/08_Audio_Test`) and `user_config.h`:

- Codec: **ES8311** (output), ES7210 (mic, unused here), plus a power amp (PA).
- Audio power enable: **GPIO42** (`Audio_PWR_PIN`) — must be HIGH to power the
  audio section.
- Codec control over I2C (shared bus with RTC@0x51 / SHTC3@0x70): SDA=47, SCL=48.
- I2S pins + PA pin + i2c address: taken from the vendor's `S3_ePaper_1_54` board
  profile (the example calls `set_codec_board_type("S3_ePaper_1_54")`). **Exact
  I2S pin numbers to be confirmed from the vendor code during implementation** —
  the profile is not in the committed `board_cfg.txt` and is registered elsewhere
  in the example.
- Power latch: GPIO17 (`VBAT_PWR_PIN`) — held HIGH through deep sleep (already
  fixed; unaffected by this feature).

## Approach

Vendor Waveshare's proven audio stack (`esp_codec_dev` + ES8311 driver + minimal
codec init) rather than hand-rolling the ES8311 bring-up. Chosen for first-try
reliability on a device that is laborious to reflash. (Alternative considered:
minimal hand-rolled ES8311 + raw I2S — rejected as higher silent-output risk.)

## Components & files

- **`tools/wav_to_pcm.sh`** — converts `~/Downloads/urgent.wav` (or a path arg)
  to mono 16 kHz 16-bit raw PCM at `src/urgent.pcm` via ffmpeg:
  `ffmpeg -i <in> -ac 1 -ar 16000 -f s16le src/urgent.pcm`. The generated
  `src/urgent.pcm` (~65 KB) is committed so the firmware build does not require
  ffmpeg.
- **Embedded asset** — `src/urgent.pcm` baked into flash via PlatformIO
  `board_build.embed_files`, exposed as linker symbols
  `_binary_src_urgent_pcm_start` / `_binary_src_urgent_pcm_end` (the same
  mechanism the vendor example uses for `canon_pcm`).
- **`lib/audio/`** — vendored, trimmed `esp_codec_dev` + ES8311 driver + minimal
  board init, behind a thin wrapper `audio_bsp.h`:
  - `bool audioInit();`            // power GPIO42, init ES8311 over I2C, I2S, PA on, set volume
  - `void audioPlayPCM16(const uint8_t* data, size_t bytes, int sampleRate);` // blocking
  - `void audioDeinit();`          // PA off, codec/I2S released, GPIO42 off
- **`src/firmware.ino`** — `fetchUrgent()` and the play call.

## Control flow (in `loop()`, each wake)

```
read battery
haveImage = fetchImage()              // unchanged (with retries)
urgent    = fetchUrgent(URGENT_URL)   // GET, trim whitespace, case-insensitive == "true"
renderScreen(...)                     // unchanged (image render / keep-last on failure)
if (urgent) {
    if (audioInit()) {
        audioPlayPCM16(_binary_src_urgent_pcm_start,
                       _binary_src_urgent_pcm_end - _binary_src_urgent_pcm_start,
                       16000);        // blocks ~2 s
        audioDeinit();
    }
}
enterDeepSleep(UPDATE_INTERVAL);      // GPIO17 latch held, as fixed
```

Order rationale: both network fetches happen while WiFi is up; the image renders
before the sound plays; audio is fully torn down before sleep so it cannot draw
current during deep sleep.

## `fetchUrgent()` semantics

- HTTP GET `URGENT_URL`.
- Read body, strip surrounding whitespace/newlines, compare case-insensitively to
  `true`.
- Any failure (HTTP error, empty body, timeout) → returns `false`. **Fail-safe:
  never raise a false alarm on a network hiccup.**

## Audio asset / playback

- Source: `urgent.wav` (48 kHz stereo, 16-bit, 2.04 s) → converted to mono
  16 kHz 16-bit PCM (~65 KB). Played back as 16-bit mono @ 16 kHz through ES8311.
- Volume: `#define URGENT_VOLUME 70` (percent, tunable); PA gain from board profile.

## Config

- **`.env`**: add `URGENT_URL=https://dash.example.com/urgent.txt`.
- **`inject_secrets.py`**: add `URGENT_URL` as an **optional** quoted-string key
  (not in `REQUIRED_KEYS`). If unset, the firmware `#ifdef`s the whole feature
  out (no audio code compiled, behaves exactly as today).
- **`.env.example`**: document `URGENT_URL`.

## Error handling

- Urgent fetch failure → not urgent (fail-safe, above).
- `audioInit()` failure → log and skip playback; never blocks the sleep/wake cycle.
- Playback is synchronous and bounded (~2 s) so it cannot stall the cycle.
- GPIO17 power latch untouched; GPIO42 + PA explicitly turned off before sleep.

## Testing

- **Manual on-device:** set server `urgent.txt` to `true` → confirm the sound
  plays each wake; set to `false` → silence. Verify the normal sleep/wake/refresh
  cycle still works (serial capture at a short interval, per the flashing recipe).
- **Build check:** embedded PCM byte length ≈ 2.04 s × 16000 × 2 ≈ 65 KB.
- **Regression:** with `URGENT_URL` unset, firmware builds and behaves exactly as
  before (feature compiled out).

## Risks / open items

1. **Integrating `esp_codec_dev` into the Arduino/PlatformIO build** — main effort
   and risk; the vendor example is IDF-component-structured.
2. **Exact `S3_ePaper_1_54` I2S pin mapping** — confirm from vendor code during
   implementation (audio power GPIO42 and I2C 47/48 already known).
3. **Flash growth** — ~65 KB PCM + codec lib; comfortably within the 3 MB app
   partition (currently ~28% used).

## Out of scope (YAGNI)

- Edge-triggered / cooldown alert modes.
- On-screen urgent indicator.
- Configurable/downloadable sound, multiple sounds, or per-alert volume.
