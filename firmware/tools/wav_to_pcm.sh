#!/usr/bin/env bash
# Convert a WAV to mono 16 kHz 16-bit little-endian raw PCM for embedding.
#   ./tools/wav_to_pcm.sh [INPUT.wav] [OUTPUT.pcm]
# Defaults: ~/Downloads/urgent.wav -> src/urgent.pcm
set -euo pipefail
cd "$(dirname "$0")/.."
IN="${1:-$HOME/Downloads/urgent.wav}"
OUT="${2:-src/urgent.pcm}"
command -v ffmpeg >/dev/null || { echo "ffmpeg not found (brew install ffmpeg)"; exit 1; }
# Boost +12 dB into a brick-wall limiter: the small speaker needs a loud, dense
# alert. Lifts a typically quiet chime ~10 dB in perceived loudness with no
# clipping. Codec volume is already at unity (0 dB), so loudness comes from here.
ffmpeg -y -i "$IN" -af "volume=12dB,alimiter=limit=0.97:level=false" -ac 1 -ar 16000 -f s16le "$OUT"
echo "wrote $OUT ($(wc -c < "$OUT") bytes)"
