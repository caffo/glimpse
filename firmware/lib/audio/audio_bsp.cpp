// Audio BSP: thin wrapper over the Waveshare/Espressif esp_codec_dev framework.
//
// The hand-rolled ES8311 driver got the codec registers right but the deprecated
// legacy driver/i2s.h on espressif32@6.9.0 would not reliably emit MCLK, so no
// sound came out. This now drives the vendor esp_codec_dev stack (new i2s_std
// driver) on the bumped pioarduino platform. Public API is unchanged.
//
// Board profile "S3_ePaper_1_54" (see lib/audio/codec_board/board_cfg.h):
//   i2c  sda 47 / scl 48
//   i2s  bclk 15 / ws 38 / dout 45 / din 16 / mclk 14
//   codec ES8311 in_out, PA on GPIO46, MCLK enabled, pa_gain 6 dB
// GPIO42 is the audio-section power enable. It is ACTIVE LOW (pull-up enabled,
// drive LOW = on, HIGH = off) — confirmed against the known-good pala-note
// firmware for this exact board. The earlier active-HIGH assumption held the
// rail OFF, which is why init succeeded but playback was silent.

#include "audio_bsp.h"
#include <Arduino.h>
#include "driver/gpio.h"

extern "C" {
#include "codec_board/codec_board.h"
#include "codec_board/codec_init.h"
#include "esp_codec_dev/include/esp_codec_dev.h"
}

#define AUDIO_PWR_PIN 42   // audio-section power enable, ACTIVE LOW
#define AUDIO_PWR_ON   LOW
#define AUDIO_PWR_OFF  HIGH

static esp_codec_dev_handle_t s_play = nullptr;

// TEMP diagnostics: how far audioInit got + the open() return code, for on-screen
// reporting (serial is unreliable on this board's native USB).
//   stage: 1=power 2=init_codec ok 3=handle ok 4=open ok 5=done
int g_audio_stage   = 0;
int g_audio_open_rc = 999;
int g_audio_initrc  = 999;   // init_codec() return code

bool audioInit(int sampleRate, int volumePct) {
  // Power the audio section before touching I2C/I2S (active low, pull-up).
  pinMode(AUDIO_PWR_PIN, INPUT_PULLUP);
  pinMode(AUDIO_PWR_PIN, OUTPUT);
  digitalWrite(AUDIO_PWR_PIN, AUDIO_PWR_ON);
  delay(50);
  g_audio_stage = 1;

  set_codec_board_type("S3_ePaper_1_54");

  codec_init_cfg_t cfg = {};
  cfg.in_mode    = CODEC_I2S_MODE_STD;
  cfg.out_mode   = CODEC_I2S_MODE_STD;
  cfg.in_use_tdm = false;
  cfg.reuse_dev  = false;
  g_audio_initrc = init_codec(&cfg);
  if (g_audio_initrc != 0) {
    digitalWrite(AUDIO_PWR_PIN, AUDIO_PWR_OFF);
    return false;
  }
  g_audio_stage = 2;

  s_play = get_playback_handle();
  if (s_play == nullptr) {
    deinit_codec();
    digitalWrite(AUDIO_PWR_PIN, AUDIO_PWR_OFF);
    return false;
  }
  g_audio_stage = 3;

  esp_codec_dev_sample_info_t fs = {};
  fs.bits_per_sample = 16;
  fs.channel         = 2;   // stereo (matches the vendor example; we duplicate mono -> L+R)
  fs.sample_rate     = (uint32_t)sampleRate;
  g_audio_open_rc = esp_codec_dev_open(s_play, &fs);
  if (g_audio_open_rc != ESP_CODEC_DEV_OK) {
    s_play = nullptr;
    deinit_codec();
    digitalWrite(AUDIO_PWR_PIN, AUDIO_PWR_OFF);
    return false;
  }
  g_audio_stage = 4;

  // Belt-and-suspenders: ensure the I2S TX channel is enabled after open. If the
  // codec's open-time I2S reconfig left it disabled, esp_codec_dev_write would
  // block forever and no sound would play. Harmless if already enabled.
  codec_i2s_force_enable();

  esp_codec_dev_set_out_vol(s_play, volumePct);   // 0..100
  g_audio_stage = 5;
  return true;
}

void audioPlayPCM16(const uint8_t *data, size_t bytes) {
  if (s_play == nullptr || data == nullptr || bytes == 0) return;
  // PCM is mono; codec is opened stereo. Duplicate each 16-bit sample to L+R
  // and write in chunks.
  const int16_t *mono = (const int16_t *)data;
  size_t samples = bytes / 2;
  static int16_t stereo[256 * 2];
  size_t i = 0;
  while (i < samples) {
    size_t n = samples - i;
    if (n > 256) n = 256;
    for (size_t k = 0; k < n; k++) { stereo[k * 2] = mono[i + k]; stereo[k * 2 + 1] = mono[i + k]; }
    esp_codec_dev_write(s_play, stereo, (int)(n * 4));
    i += n;
  }
}

void audioDeinit(void) {
  if (s_play != nullptr) {
    esp_codec_dev_close(s_play);
    s_play = nullptr;
  }
  deinit_codec();
  digitalWrite(AUDIO_PWR_PIN, AUDIO_PWR_OFF);   // cut audio power before sleep
}
