#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#ifdef __cplusplus
extern "C" {
#endif
extern int g_audio_stage;     // TEMP diag: how far audioInit got (5 = full success)
extern int g_audio_open_rc;   // TEMP diag: esp_codec_dev_open() return code
extern int g_audio_initrc;    // TEMP diag: init_codec() return code
extern int g_codec_init_fail; // TEMP diag: 1=i2c-out 2=i2s-out 3=i2c-in 4=i2s-in
extern int g_i2s_new_ret;     // TEMP diag: i2s_new_channel() return
extern int g_i2s_std_ret;     // TEMP diag: i2s_channel_init_std_mode(tx) return
bool audioInit(int sampleRate, int volumePct);          // power + init ES8311 (mono 16-bit); vol 0..100
void audioPlayPCM16(const uint8_t *data, size_t bytes);  // blocking I2S write
void audioDeinit(void);                                   // mute PA, power down, stop I2S, GPIO42 low
#ifdef __cplusplus
}
#endif
