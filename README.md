# Glimpse
*A dumb little frame on my desk. I tell it what to show.*


200×200 pixels of black-and-white e-paper. A photo, some Markdown, live weather,
something FLUX come up — pushed from one line in the shell.

```bash
glimpse photo.jpg                            # local image file
glimpse https://example.com/x.png            # image URL (fetched, then uploaded)
glimpse '# Today\n- buy milk\n- **call** mom'  # Markdown (auto-detected)
glimpse -m flux 'a mountain lake at dusk'    # AI image (FLUX)
glimpse -m llm 'capital of Peru?'            # ask DeepSeek, render answer
glimpse -m weather 'rio de janeiro, sao paulo'  # real weather per city
glimpse -m text 'plain centered text'        # simple text canvas
echo '# Hi' | glimpse -                      # Markdown from stdin
glimpse --reset                              # reset to default image

# Gallery
glimpse -k -m flux 'a koi pond'   # push AND save to the rotating gallery
glimpse -k -m weather 'rio, sp'   # kept recipe -> regenerated live each show
glimpse -l -m text 'BRB'          # show it and freeze (pause rotation)
glimpse -u -m text 'FIRE!'        # urgent -> /urgent.txt=true, device beeps
glimpse --lock                    # pause rotation, hold current image
glimpse --unlock                  # resume rotation
glimpse --interval 600            # rotate every 10 minutes (min 5s)
glimpse --gallery                 # list saved entries
glimpse --gallery-clear           # wipe the gallery
glimpse --help                    # full usage
```

Three pieces, one repo:

| Part | What it is |
|------|-----------|
| [`firmware/`](#firmware) | ESP32-S3 firmware for the [Waveshare 1.54" e-paper board](https://www.waveshare.com/esp32-s3-epaper-1.54.htm). Wakes, fetches a PNG, shows it, sleeps. |
| [`server/`](#server) | Flask API that turns *anything* into a canonical 200×200 1-bit PNG served at one fixed URL. |
| [`glimpse`](#the-glimpse-cli) | Shell CLI that pushes to the server. |

The device is deliberately dumb. It knows one URL and how to draw it. That's it.
Everything else — resizing, dithering, generating — happens server-side, so you
can change what the frame shows without ever reflashing it.

## How it fits together

![The glimpse CLI POSTs to /upload; the server resizes, dithers and generates,
then writes a single 200×200 1-bit PNG to /image.png, which a gallery thread
rotates every N seconds; the ESP32-S3 GETs that one URL on each
wake.](docs/architecture.svg)

One image at `/image.png`. Always. Each push overwrites the last one. Keep pushes
in the **gallery** and a background thread rotates that URL among them on an
interval. The device never knows the difference — same URL, every wake.

---

## Server

A tiny public API. Feed it an uploaded image, a text prompt, or nothing at all,
and it hands back one canonical PNG: always **200×200**, always **≤ 50 KB**.

### Modes

`POST /upload` accepts input in priority order:

1. **An image file** (`multipart/form-data`, field `image`) — resized to 200×200
   and compressed under 50 KB.
2. **Text** (form field `text`, JSON `{"text": ...}`, or raw body), interpreted
   according to the `mode` field:
   - `mode=markdown` (**default**) — renders Markdown (headers, bullets, bold)
     to a crisp 1-bit image via **wkhtmltoimage**. Best for dashboard text.
   - `mode=flux` — sends the text as a prompt to **FLUX.1-schnell** via
     [fal.ai](https://fal.ai). Requires `FAL_KEY`.
   - `mode=llm` — sends the text as a question to **DeepSeek**, which returns a
     concise Markdown answer that is then rendered. Requires `DEEPSEEK_KEY`.
   - `mode=weather` — real current weather per city via **Open-Meteo** (free, no
     key). Comma-separated city names, e.g. `rio de janeiro, sao paulo`.
   - `mode=text` — simple centered plain-text canvas (Pillow, no deps).
   - `mode=clock` — current time rendered as Markdown ("Right now"), in the
     timezone named by `TZ`. Needs no text input; as a kept gallery recipe it
     regenerates live each show.
3. **Nothing** — falls back to a baked-in default image (`default.png`, a 1-bit
   "sad face").

Photo-like inputs (uploads, FLUX, default) are resized to 200×200, then run
through [`server/prepare_image.py`](server/prepare_image.py) — resize-to-fill,
autocontrast, sharpen, **1-bit Floyd–Steinberg dither** — so the panel displays
them verbatim. Markdown is thresholded to crisp 1-bit (no dithering) to keep text
edges sharp.

**Fail-safe:** on *any* error (invalid file, generation failure, compression
can't hit the size limit, unexpected exception) the API does not return an error
— it writes the default image into `image.png` instead.

### Gallery (rotating images)

Add `keep=1` to any `POST /upload` (or `-k` with the CLI) to also save that push
into a **gallery**. A background thread then rotates `/image.png` among the saved
entries at a fixed interval — picked randomly, never repeating the same entry
twice in a row.

Two kinds of entries:

- **Image** (uploaded file) — the processed snapshot is cached and replayed
  verbatim on each show.
- **Recipe** (`flux` / `llm` / `weather` / `markdown` / `text` / `clock`) — the
  original text+mode is **regenerated fresh** every time it is shown, so a kept
  weather entry always displays live conditions. A snapshot is saved as a
  fallback if regeneration ever fails.

The baked-in photo (`gallery_default.png`) is seeded as the first gallery entry
on first run, so the gallery is never empty.

**Lock/unlock:** `lock=1` pauses rotation and holds the current image on screen;
`/gallery/unlock` resumes it. Handy for pinning a one-shot push you don't want
rotated away.

**Urgent flag:** pushing with `urgent=1` also sets `GET /urgent.txt` to `true`,
so the device can poll a tiny endpoint to decide whether to play a
[sound alert](#urgent-sound-alert). Every other write — a normal push, a reset,
or a gallery rotation — resets it back to `false`. It is per-image, not sticky.

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET`  | `/image.png` | none  | The current image (200×200 PNG, ≤50 KB). Serves the default if none set. |
| `GET`  | `/urgent.txt` | none | `true`/`false` — whether the current image was pushed urgent. |
| `POST` | `/upload`    | token | Set the image from a file, text prompt, or default. `keep=1` saves to the gallery; `lock=1` freezes on it; `urgent=1` flags it urgent. |
| `GET`  | `/gallery`   | token | List gallery entries, the rotation interval, and the lock state. |
| `GET`  | `/gallery/<id>.png` | token | The cached snapshot for one entry. |
| `POST` | `/gallery/interval` | token | Set rotation interval in seconds (`interval=`, min 5). |
| `POST` | `/gallery/lock` | token | Pause rotation, freezing the current image. |
| `POST` | `/gallery/unlock` | token | Resume rotation. |
| `POST` | `/gallery/delete/<id>` | token | Remove one entry. |
| `POST` | `/gallery/clear` | token | Remove all entries. |

Only `/image.png` and `/urgent.txt` (what the frame fetches) are **open to
everyone**. Everything else — the gallery and all writes — requires
`UPLOAD_TOKEN` when it is set —
supply it as an `X-Upload-Token:` header, a `?token=` query param, or a `token=`
form field. If `UPLOAD_TOKEN` is unset, all writes are allowed (private-VM mode).

### Configuration

Environment variables, loaded from `server/.env` (git-ignored):

| Variable | Required | Description |
|----------|----------|-------------|
| `FAL_KEY` | for `mode=flux` | fal.ai API key. |
| `FAL_MODEL` | no | fal model id (default `fal-ai/flux/schnell`). |
| `DEEPSEEK_KEY` | for `mode=llm` | DeepSeek API key. |
| `DEEPSEEK_MODEL` | no | DeepSeek model id (default `deepseek-chat`). |
| `UPLOAD_TOKEN` | recommended if public | Secret required for `POST /upload`. |
| `TZ` | for `mode=clock` | Standard Unix timezone — any IANA name, e.g. `America/Sao_Paulo`. Defaults to `UTC`. DST is handled for you; an unknown name aborts at startup rather than silently serving the wrong hour. |

### Running

Needs the **wkhtmltoimage** binary for Markdown rendering
(`sudo apt-get install wkhtmltopdf`).

```bash
cd server
python3 -m venv venv
./venv/bin/pip install -r requirements.txt   # flask, pillow, markdown

cat > .env <<'EOF'
FAL_KEY=your-fal-key
UPLOAD_TOKEN=your-secret-token
TZ=America/Sao_Paulo
EOF
chmod 600 .env

./venv/bin/python app.py    # serves on 0.0.0.0:8000
```

In production, [`server/imgapi.service`](server/imgapi.service) runs it under
systemd with `EnvironmentFile=` for the secrets:

```bash
sudo cp server/imgapi.service /etc/systemd/system/imgapi.service
sudo systemctl daemon-reload
sudo systemctl enable --now imgapi
# logs: journalctl -u imgapi -f
```

Runtime state lives in `server/data/` (git-ignored, created on first run):
`image.png`, `urgent.txt`, and `gallery/`.

**How I run mine:** a small box on [exe.dev](https://exe.dev). Not required —
anything that serves HTTP and can hold a Python process will do.

**Cost:** text→image uses FLUX.1-schnell on fal.ai, roughly **$0.003 per image**
(~300+ generations per dollar). Uploads and the default fallback are free.

---

## The `glimpse` CLI

`glimpse` auto-detects the input type — image file, image URL, Markdown, or an
AI prompt — or you can force a mode with `-m`.

```bash
export API=https://dash.example.com      # your server
export GLIMPSE_TOKEN=your-upload-token

glimpse photo.jpg                            # local image file
glimpse https://example.com/x.png            # image URL (fetched, then uploaded)
glimpse '# Today\n- buy milk\n- **call** mom'  # Markdown (auto-detected)
glimpse -m flux 'a mountain lake at dusk'    # AI image (FLUX)
glimpse -m llm 'capital of Peru?'            # ask DeepSeek, render answer
glimpse -m weather 'rio de janeiro, sao paulo'  # real weather per city
glimpse -m text 'plain centered text'        # simple text canvas
echo '# Hi' | glimpse -                      # Markdown from stdin
glimpse --reset                              # reset to default image

# Gallery
glimpse -k -m flux 'a koi pond'   # push AND save to the rotating gallery
glimpse -k -m weather 'rio, sp'   # kept recipe -> regenerated live each show
glimpse -l -m text 'BRB'          # show it and freeze (pause rotation)
glimpse -u -m text 'FIRE!'        # urgent -> /urgent.txt=true, device beeps
glimpse --lock                    # pause rotation, hold current image
glimpse --unlock                  # resume rotation
glimpse --interval 600            # rotate every 10 minutes (min 5s)
glimpse --gallery                 # list saved entries
glimpse --gallery-clear           # wipe the gallery
glimpse --help                    # full usage
```

Auto-detection: input that is an existing file or `http(s)://` URL is treated as
an image; otherwise text that looks like Markdown (`#`, `-`, `*`, `|`, links,
backticks…) uses `markdown`, and anything else is sent to `flux` as a prompt.

`API` (your server's base URL) and `GLIMPSE_TOKEN` are both required —
`glimpse` exits with a hint if either is unset. `EINK_DASH_TOKEN` still works as
a fallback for the token.

### Raw curl

```bash
# Markdown (default mode)
curl -X POST https://dash.example.com/upload -H "X-Upload-Token: $TOKEN" \
  --data-urlencode $'text=# Tasks\n- buy milk\n- ship code'

# Generate from a prompt (FLUX)
curl -X POST https://dash.example.com/upload -H "X-Upload-Token: $TOKEN" \
  --data-urlencode "text=a green forest path" --data-urlencode "mode=flux"

# Upload an existing image
curl -X POST https://dash.example.com/upload -H "X-Upload-Token: $TOKEN" \
  -F "image=@photo.jpg"

# Reset to the default image (send no input)
curl -X POST https://dash.example.com/upload -H "X-Upload-Token: $TOKEN"
```

Successful responses look like `{"ok": true, "bytes": 21601, "url": "/image.png"}`;
fallbacks add `"default": true` and a `"reason"`.

---

## Firmware

Firmware for the **Waveshare ESP32-S3-ePaper-1.54** board (1.54" 200×200
black/white e-paper). On each wake it connects to Wi-Fi, downloads a 1-bit PNG,
shows it with the battery percentage in the top-right corner, then deep-sleeps
until the next interval (or until the BOOT button wakes it).

The battery percentage adapts to the image beneath it: over a dark area it draws
white text on a black pad, over a light area black text on a white pad, so it
stays legible. A `LOW BATTERY!` notice appears below 20%.

### Build

Everything below runs from `firmware/`. It builds with
[PlatformIO](https://platformio.org/install).

```bash
cd firmware
pio run                 # compile
pio run -t upload       # compile + flash over USB-C
pio device monitor      # serial console @ 115200
```

The first build downloads the toolchain and pinned libraries (GxEPD2, PNGdec,
Adafruit GFX/BusIO) automatically; nothing else to install.

> **Arduino IDE / arduino-cli alternative:** the sketch lives in
> `firmware/src/firmware.ino`.
> ```bash
> arduino-cli core install esp32:esp32
> arduino-cli lib install GxEPD2 PNGdec "Adafruit GFX Library"
> arduino-cli compile -b esp32:esp32:esp32s3 \
>   --build-property build.psram_type=opi src/firmware.ino
> ```

### Configure before flashing

Wi-Fi credentials and the image URL live in **`firmware/.env`** (git-ignored,
never committed). Copy the template and fill it in:

```bash
cd firmware
cp .env.example .env
$EDITOR .env
```

```ini
WIFI_SSID=YourNetwork                # required
WIFI_PASSWORD=YourPassword           # required
IMAGE_URL=http://your-server.com/image.png   # required
UPDATE_INTERVAL=300                  # optional, default 300 — seconds between refreshes
# URGENT_URL=http://your-server.com/urgent.txt  # optional, enables the sound alert
# URGENT_VOLUME=100                  # optional, default 100 (percent, = codec unity gain)
# UPLOAD_PORT=/dev/cu.usbmodem101    # optional, auto-detected if omitted
```

`WIFI_SSID`, `WIFI_PASSWORD`, and `IMAGE_URL` are **required**: the build fails
with a clear error if any is missing — by design, so you can't flash a firmware
that has no network config. `URGENT_URL` is optional and gates the
[sound alert](#urgent-sound-alert); leave it unset and the audio code is compiled
out entirely.

`inject_secrets.py` reads these at build time and injects them as `-D` flags, so
they never need to be hard-coded in the sketch. Then build and flash:

```bash
./flash.sh        # source .env, build, upload over USB-C
./flash.sh -m     # ...and open the serial monitor afterwards
```

Less-common knobs (battery thresholds, download cap) live at the top of
`firmware/src/firmware.ino`.

### Image requirements

`IMAGE_URL` should serve an **already-dithered, 1-bit 200×200 PNG** — which is
exactly what [the server](#server) produces. The device does no dithering; it
thresholds each pixel (dark → black) and displays it, so what you prepare is
exactly what shows on the panel.

- **200×200 pixels.** The image is *not* scaled. Larger images are clipped to the
  top-left 200×200; smaller ones are placed top-left on a white background.
- **Under 50 KB** (`MAX_IMAGE_BYTES`). Larger downloads are rejected and the
  refresh is skipped.

To prepare a file by hand without the server, use the same script it uses:

```bash
pip install Pillow
python3 server/prepare_image.py <source-image-or-URL> -o image.png
```

### Urgent sound alert

An optional, fail-safe sound alert played through the board's ES8311 codec, gated
entirely behind the `URGENT_URL` build flag.

When enabled, each wake — after refreshing the display and while Wi-Fi is still
up — the device GETs `URGENT_URL` and plays an embedded chime **only if the
response body is exactly `true`** (case-insensitive, whitespace-trimmed), then
deep-sleeps as usual. Any fetch error, a non-`true` body, or an audio-init
failure simply skips the sound — it never blocks the sleep/wake cycle.

Point `URGENT_URL` at the server's `/urgent.txt` and `glimpse -u` becomes the
whole control surface: the flag resets itself to `false` on the next non-urgent
write, so it beeps once rather than every wake.

The chime is embedded in flash as **16-bit mono 16 kHz PCM**
(`firmware/src/urgent.pcm`). Regenerate it from any WAV with
[`firmware/tools/wav_to_pcm.sh`](firmware/tools/wav_to_pcm.sh):

```bash
./firmware/tools/wav_to_pcm.sh path/to/sound.wav firmware/src/urgent.pcm
```

The script converts to the right format and bakes in a **+12 dB boost through a
brick-wall limiter** — the board's small speaker needs a loud, dense alert, and
the codec volume is already at unity (0 dB) so loudness has to come from the clip
itself. `URGENT_VOLUME` only attenuates below that; values above 100 are clamped
by the codec, not amplified.

> **Hardware note:** the audio-section power enable on **GPIO42 is active-LOW**
> (pull-up; drive low to enable). Driving it high leaves the amplifier powered off
> while the codec still initializes cleanly — the cause of perfectly-silent
> "working" audio if you port this to another board.

### Board / module

| | |
|---|---|
| Module | ESP32-S3-PICO-1-N8R8 |
| Flash | 8 MB (QIO) |
| PSRAM | 8 MB (Octal / OPI) |
| Display | 1.54" 200×200 mono, SSD1681 → GxEPD2_154_D67 |

These settings are encoded in `firmware/platformio.ini` (`memory_type = qio_opi`,
8 MB flash).

### Pin map (verified against Waveshare's example code)

| Signal | GPIO | Notes |
|---|---|---|
| EPD CS | 11 | |
| EPD DC | 10 | |
| EPD RST | 9 | |
| EPD BUSY | 8 | |
| EPD SCK | 12 | |
| EPD MOSI | 13 | |
| EPD power enable | 6 | **active-LOW** (0 = panel on) |
| Battery sense | 4 | ADC1_CH3, 2:1 divider |
| Battery-sense enable | 17 | **active-HIGH** (1 = measure); also the power latch held through sleep |
| BOOT button (wake) | 0 | active-LOW, RTC wake source |
| Audio power enable | 42 | **active-LOW** (0 = audio on); only used when `URGENT_URL` is set |
| Codec I2C SDA / SCL | 47 / 48 | ES8311 @ 0x18 |
| Codec I2S BCLK / WS / DOUT / MCLK | 15 / 38 / 45 / 14 | PA on GPIO46, MCLK enabled |

### Notes / caveats

- `Serial` output goes over the **native USB-C port** (`ARDUINO_USB_CDC_ON_BOOT=1`).
  The port disappears in deep sleep; for flashing, enter ROM download mode (hold
  BOOT while powering on with PWR) so the port stays stable.
- The fetch path is **PNG only**; non-PNG responses log an error and skip the
  update.

Sources for the board/pin details:
[Waveshare wiki](https://www.waveshare.com/wiki/ESP32-S3-ePaper-1.54),
[Waveshare example repo](https://github.com/waveshareteam/ESP32-S3-ePaper-1.54).

## Repo layout

```
glimpse            the CLI
firmware/          ESP32-S3 firmware (PlatformIO)
  src/             firmware.ino + the embedded chime
  lib/audio/       vendored esp_codec_dev / codec_board
  tools/           wav_to_pcm.sh
  flash.sh         source .env, build, upload
server/            Flask API
  app.py           the whole server
  prepare_image.py e-paper image prep (resize-fill, contrast, 1-bit dither)
  imgapi.service   systemd unit
docs/              architecture diagram
```

The server used to live in its own repo. Merged it here so the firmware, the API
it talks to, and the CLI that drives them move together. Fewer repos to chase.

## License

Public domain, under [The Unlicense](LICENSE). Do whatever you want with it.
