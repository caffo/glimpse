import io
import os
import html as _html
import textwrap
import tempfile
import subprocess
import urllib.request
import urllib.parse
import unicodedata
import re
import json
import time
import uuid
import random
import shutil
import threading
import datetime
import zoneinfo

import markdown as _markdown
from flask import Flask, request, jsonify, send_file, abort
from PIL import Image, ImageDraw, ImageFont

import prepare_image

app = Flask(__name__)

FAL_KEY = os.environ.get("FAL_KEY", "").strip()
FAL_MODEL = os.environ.get("FAL_MODEL", "fal-ai/flux/schnell")
UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "").strip()
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# Standard Unix TZ: an IANA name like "America/Sao_Paulo". Resolved once here so
# a typo fails loudly at startup rather than silently serving the wrong hour --
# glibc would just fall back to UTC. ZoneInfo also tracks DST, which the fixed
# -3 offset this replaced did not.
_TZ_NAME = os.environ.get("TZ", "UTC").strip() or "UTC"
try:
    CLOCK_TZ = zoneinfo.ZoneInfo(_TZ_NAME)
except zoneinfo.ZoneInfoNotFoundError:
    raise SystemExit(
        f"TZ={_TZ_NAME!r} is not a known IANA timezone "
        '(expected e.g. "America/Sao_Paulo", "Europe/Lisbon", "UTC")'
    )

# Keep answers tiny: a 200x200 1-bit panel fits only a handful of short lines.
LLM_SYSTEM = (
    "You answer questions for display on a tiny 200x200 pixel e-paper screen. "
    "Be extremely concise: at most ~6 short lines total. Use GitHub-flavored "
    "Markdown (a short header, bullets, **bold**) when it helps. No preamble, "
    "no closing remarks, no code fences unless code is essential. "
    "Prefer fragments over full sentences."
)


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on", "keep")


def _set_urgent(flag):
    """Write the urgent state ('true'/'false') alongside image.png. Every write
    to the image sets this, so it always reflects the current image."""
    try:
        with open(URGENT_PATH, "w") as f:
            f.write("true" if flag else "false")
    except Exception:
        app.logger.exception("failed to write urgent.txt")


def _authorized():
    """True if writes are allowed. If no token is configured, allow all
    (private-VM mode). Otherwise require a matching token."""
    if not UPLOAD_TOKEN:
        return True
    import hmac
    supplied = (
        request.headers.get("X-Upload-Token")
        or request.args.get("token")
        or request.form.get("token")
        or ""
    )
    return hmac.compare_digest(supplied, UPLOAD_TOKEN)


def _generate_with_fal(prompt):
    """Call fal.ai (FLUX.1-schnell) and return a PIL image."""
    body = json.dumps({
        "prompt": prompt,
        # Generate a tight square close to the 200x200 panel so we barely
        # downscale (less detail lost to the 1-bit dither). 256 keeps it a
        # multiple of 16, which the model expects.
        "image_size": {"width": 256, "height": 256},
        "num_images": 1,
        "enable_safety_checker": True,
    }).encode()
    req = urllib.request.Request(
        f"https://fal.run/{FAL_MODEL}",
        data=body,
        headers={
            "Authorization": f"Key {FAL_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
    url = payload["images"][0]["url"]
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    img = Image.open(io.BytesIO(data))
    img.load()
    return img

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
IMAGE_PATH = os.path.join(DATA_DIR, "image.png")
URGENT_PATH = os.path.join(DATA_DIR, "urgent.txt")
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default.png")

GALLERY_DIR = os.path.join(DATA_DIR, "gallery")
os.makedirs(GALLERY_DIR, exist_ok=True)
GALLERY_CONFIG = os.path.join(GALLERY_DIR, "config.json")

SIZE = (200, 200)
MAX_BYTES = 50 * 1024

DEFAULT_INTERVAL = 240  # seconds between gallery rotations (4 min)
MIN_INTERVAL = 5

# Serializes all writes to image.png (upload handler vs. rotation thread).
_io_lock = threading.RLock()
# Tracks the last gallery entry shown so we never repeat twice in a row.
_rotation = {"last": None, "interval": DEFAULT_INTERVAL, "reset_timer": False,
             # Bumped under _io_lock on every image.png write. A rotation snapshots
             # it before its (slow) render and passes it to _commit_image; if a push
             # landed meanwhile the stale render is dropped instead of clobbering it.
             "gen": 0}


# CSS tuned for a 200px-wide 1-bit e-paper panel: large, high-contrast,
# tight margins. Pure black on white so the threshold step stays crisp.
MARKDOWN_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: 200px; background: #fff; color: #000; }
body {
  font-family: 'DejaVu Sans', sans-serif;
  font-size: 20px; line-height: 1.25; padding: 6px 8px;
  -webkit-font-smoothing: none;
}
h1 { font-size: 30px; line-height: 1.1; margin-bottom: 5px; border-bottom: 2px solid #000; }
h2 { font-size: 25px; line-height: 1.1; margin: 7px 0 4px; }
h3 { font-size: 22px; margin: 6px 0 3px; }
p  { margin: 0 0 5px; }
ul, ol { margin: 0 0 5px; padding-left: 20px; }
li { margin-bottom: 3px; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 17px; }
pre { background: #eee; padding: 3px; overflow: hidden; }
strong { font-weight: bold; }
hr { border: none; border-top: 1px solid #000; margin: 4px 0; }
img { max-width: 100%; }
"""


# WMO codes -> short description.
WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Hail",
    80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow",
    86: "Snow", 95: "Thunderstorm", 96: "Thunderstorm/hail",
    99: "Thunderstorm/hail",
}


def _norm(s):
    """Lowercase without accents, for comparing city names."""
    return "".join(
        c for c in unicodedata.normalize("NFD", (s or "").lower())
        if unicodedata.category(c) != "Mn"
    ).strip()


def _best_match(query, results):
    """Pick the result whose name exactly matches the query (accents
    stripped); break ties by largest population. Otherwise, the first."""
    q = _norm(query)
    exact = [r for r in results if _norm(r.get("name")) == q]
    if exact:
        return max(exact, key=lambda r: r.get("population") or 0)
    return results[0]


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "eink-dash/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _weather_markdown(cities_text):
    """Real weather per city via Open-Meteo (free, no API key)."""
    cities = [c.strip() for c in re.split(r"[,;\n]+", cities_text) if c.strip()]
    if not cities:
        cities = [cities_text.strip()]
    lines = ["# Weather"]
    for city in cities[:5]:  # ~5 cities fit on the panel
        geo = _get_json(
            "https://geocoding-api.open-meteo.com/v1/search?count=10"
            "&language=en&format=json&name=" + urllib.parse.quote(city)
        )
        results = geo.get("results") or []
        if not results:
            lines.append(f"**{city}**: not found")
            continue
        loc = _best_match(city, results)
        lat, lon = loc["latitude"], loc["longitude"]
        name = loc.get("name", city)
        fc = _get_json(
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,weather_code"
            "&daily=temperature_2m_max,temperature_2m_min"
            "&timezone=auto&forecast_days=1"
        )
        cur = fc.get("current", {})
        daily = fc.get("daily", {})
        temp = round(cur.get("temperature_2m", 0))
        desc = WMO.get(cur.get("weather_code"), "")
        tmax = round((daily.get("temperature_2m_max") or [temp])[0])
        tmin = round((daily.get("temperature_2m_min") or [temp])[0])
        lines.append(f"**{name}**: {temp}°C, {desc}")
        lines.append(f"- min {tmin}° / max {tmax}°")
    return "\n".join(lines)


def _clock_markdown(_text=None):
    """Markdown for the current time in the configured TZ.
    Built fresh on every call so a kept entry regenerates live."""
    now = datetime.datetime.now(CLOCK_TZ)
    return now.strftime("# Right now\n\n# %H:%M\n\n%a, %d %b")


def _ask_deepseek(question):
    """Send a question to DeepSeek and return a concise Markdown answer."""
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM},
            {"role": "user", "content": question},
        ],
        "max_tokens": 300,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode())
    return payload["choices"][0]["message"]["content"].strip()


def _markdown_to_image(text):
    """Render a Markdown string to a 200px-wide PNG via wkhtmltoimage,
    then return a PIL image. Height auto-fits the content."""
    body = _markdown.markdown(
        text, extensions=["extra", "sane_lists", "nl2br"]
    )
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>%s</style></head><body>%s</body></html>" % (MARKDOWN_CSS, body)
    )
    with tempfile.TemporaryDirectory() as d:
        html_path = os.path.join(d, "in.html")
        png_path = os.path.join(d, "out.png")
        with open(html_path, "w") as f:
            f.write(doc)
        subprocess.run(
            ["wkhtmltoimage", "--width", "200", "--disable-smart-width",
             "--quality", "100", "--quiet", html_path, png_path],
            check=True, capture_output=True, timeout=30,
        )
        rendered = Image.open(png_path)
        rendered.load()
    # Place the (200px-wide, auto-height) render onto a 200x200 white canvas,
    # top-aligned. If it's taller than 200, scale down to fit. Then threshold
    # to crisp 1-bit (no dithering, so text edges stay sharp).
    rendered = rendered.convert("L")
    w, h = rendered.size
    if h > prepare_image.SIZE:
        new_w = max(1, round(w * prepare_image.SIZE / h))
        rendered = rendered.resize((new_w, prepare_image.SIZE), Image.LANCZOS)
        w, h = rendered.size
    canvas = Image.new("L", (prepare_image.SIZE, prepare_image.SIZE), 255)
    canvas.paste(rendered, ((prepare_image.SIZE - w) // 2, 0))
    return canvas.point(lambda p: 0 if p < 128 else 255, mode="1")


def _load_font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _text_to_image(text):
    """Render text centered on a 200x200 white canvas."""
    img = Image.new("RGB", SIZE, "white")
    draw = ImageDraw.Draw(img)
    text = text.strip() or " "

    # Pick the largest font size that fits, wrapping text to the canvas.
    for font_size in range(64, 7, -2):
        font = _load_font(font_size)
        # Estimate chars per line from average glyph width.
        avg = draw.textlength("abcdefghijklmnopqrstuvwxyz", font=font) / 26 or 1
        wrap_at = max(1, int(190 / avg))
        lines = []
        for paragraph in text.splitlines() or [text]:
            # Keep words whole (don't split "YES" into "YE"/"S"); if a single
            # word is still too wide, the bbox check below shrinks the font.
            lines.extend(
                textwrap.wrap(paragraph, width=wrap_at,
                              break_long_words=False, break_on_hyphens=False)
                or [""]
            )
        block = "\n".join(lines)
        bbox = draw.multiline_textbbox((0, 0), block, font=font, spacing=4, align="center")
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= 190 and h <= 190:
            x = (SIZE[0] - w) / 2 - bbox[0]
            y = (SIZE[1] - h) / 2 - bbox[1]
            draw.multiline_text((x, y), block, fill="black", font=font,
                                spacing=4, align="center")
            return img
    return img  # fallback (shouldn't reach)


def _fit_under_limit(img):
    """Return PNG bytes for img, guaranteed 200x200 and <= MAX_BYTES."""
    img = img.convert("RGB").resize(SIZE, Image.LANCZOS)
    # Try increasingly aggressive palette quantization until it fits.
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    if buf.tell() <= MAX_BYTES:
        return buf.getvalue()
    for colors in (256, 128, 64, 32, 16, 8, 4, 2):
        buf = io.BytesIO()
        q = img.quantize(colors=colors, method=Image.MEDIANCUT)
        q.save(buf, format="PNG", optimize=True)
        if buf.tell() <= MAX_BYTES:
            return buf.getvalue()
    return buf.getvalue()  # smallest we managed


def _use_default(reason):
    """Write the baked-in default (the crisp 1-bit "Sad Mac") into image.png.
    It is authored ready-to-serve, so copy it verbatim - re-running the photo
    postprocess would dither and ruin the clean line art."""
    if os.path.exists(DEFAULT_PATH):
        with _io_lock:
            with open(DEFAULT_PATH, "rb") as src, open(IMAGE_PATH, "wb") as dst:
                dst.write(src.read())
            _set_urgent(False)
        return jsonify(ok=True, default=True, reason=reason,
                       bytes=os.path.getsize(IMAGE_PATH), url="/image.png")
    return jsonify(error="no input and no default image available"), 500


def _postprocess():
    """Run the e-paper prep on image.png in place (1-bit, 200x200).
    Resize-to-fill + autocontrast + contrast/brightness + sharpen, then
    Floyd-Steinberg dither to 1-bit so the device displays it verbatim."""
    out = prepare_image.prepare(
        IMAGE_PATH, prepare_image.SIZE,
        contrast=1.6, brightness=1.15, sharpen=150,
        autocontrast=True, one_bit=True,
    )
    out.save(IMAGE_PATH, "PNG", optimize=True, bits=1)


def _render_text(text, mode):
    """Turn (text, mode) into a PIL image.
    Returns (img, finalized, reason). On failure img is None and reason is set
    to a short explanation (the caller decides how to fall back)."""
    mode = (mode or "markdown").strip().lower()
    if mode == "clock":
        try:
            return _markdown_to_image(_clock_markdown(text)), True, None
        except Exception:
            app.logger.exception("clock render failed")
            return None, False, "clock render failed"
    elif mode == "weather":
        try:
            answer = _weather_markdown(text)
        except Exception:
            app.logger.exception("weather fetch failed")
            return None, False, "weather fetch failed"
        try:
            return _markdown_to_image(answer), True, None
        except Exception:
            app.logger.exception("markdown render failed")
            return None, False, "markdown render failed"
    elif mode == "llm":
        if not DEEPSEEK_KEY:
            return None, False, "llm requested but no DEEPSEEK_KEY configured"
        try:
            answer = _ask_deepseek(text)
        except Exception:
            app.logger.exception("deepseek request failed")
            return None, False, "llm request failed"
        try:
            return _markdown_to_image(answer), True, None
        except Exception:
            app.logger.exception("markdown render failed")
            return None, False, "markdown render failed"
    elif mode == "flux":
        if not FAL_KEY:
            return None, False, "flux requested but no FAL_KEY configured"
        try:
            return _generate_with_fal(text), False, None
        except Exception:
            app.logger.exception("fal generation failed")
            return None, False, "image generation failed"
    elif mode == "text":
        img = _text_to_image(text).convert("L").point(
            lambda p: 0 if p < 128 else 255, mode="1")
        return img, True, None
    else:  # markdown (default)
        try:
            return _markdown_to_image(text), True, None
        except Exception:
            app.logger.exception("markdown render failed")
            return None, False, "markdown render failed"


def _commit_image(img, finalized, urgent=False, expected_gen=None):
    """Write a rendered image to image.png following the canonical save path.
    Also (re)sets urgent.txt - any new image is non-urgent unless flagged.
    If expected_gen is given and no longer matches (a newer image was committed
    while the caller was rendering), skip the write and return "superseded".
    Returns None on success or a short reason string on failure."""
    with _io_lock:
        if expected_gen is not None and expected_gen != _rotation["gen"]:
            return "superseded"
        if finalized:
            img.save(IMAGE_PATH, "PNG", optimize=True, bits=1)
        else:
            data = _fit_under_limit(img)
            if len(data) > MAX_BYTES:
                return "could not compress under 50KB"
            with open(IMAGE_PATH, "wb") as f:
                f.write(data)
            _postprocess()
        _set_urgent(urgent)
        _rotation["gen"] += 1
    return None


# --------------------------------------------------------------------------
# Gallery: a rotating set of saved "entries". Two kinds:
#   recipe -> {text, mode}; regenerated on every show (fresh weather/llm/flux)
#   image  -> a cached snapshot PNG; reused verbatim on every show
# Each show always rewrites the canonical image.png via _commit_image so the
# device only ever fetches /image.png.
# --------------------------------------------------------------------------

def _gallery_load():
    if os.path.exists(GALLERY_CONFIG):
        try:
            with open(GALLERY_CONFIG) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    else:
        cfg = {}
    cfg.setdefault("entries", [])
    cfg.setdefault("interval", DEFAULT_INTERVAL)
    cfg.setdefault("locked", False)
    return cfg


def _gallery_save(cfg):
    tmp = GALLERY_CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, GALLERY_CONFIG)


def _set_locked(locked):
    """Freeze (True) or resume (False) gallery rotation."""
    with _io_lock:
        cfg = _gallery_load()
        cfg["locked"] = bool(locked)
        _gallery_save(cfg)


def _gallery_add(kind, snapshot_img, text=None, mode=None):
    """Add an entry. snapshot_img (a PIL image) is always saved so we can show
    it even if a recipe regeneration later fails."""
    with _io_lock:
        cfg = _gallery_load()
        eid = uuid.uuid4().hex[:12]
        snap_path = os.path.join(GALLERY_DIR, eid + ".png")
        try:
            snapshot_img.convert("RGB").save(snap_path, "PNG", optimize=True)
        except Exception:
            app.logger.exception("gallery snapshot save failed")
        entry = {"id": eid, "kind": kind, "snapshot": eid + ".png",
                 "created": time.time()}
        if kind == "recipe":
            entry["text"] = text
            entry["mode"] = mode
        cfg["entries"].append(entry)
        _gallery_save(cfg)
        return eid


def _show_entry(entry):
    """Render one gallery entry into image.png. Regenerate recipes fresh; fall
    back to the saved snapshot on any failure. Returns True on success."""
    # Snapshot the generation before the (possibly slow) render so a push that
    # lands mid-render supersedes us instead of getting clobbered.
    gen = _rotation["gen"]
    if entry.get("kind") == "recipe":
        img, finalized, reason = _render_text(entry.get("text"), entry.get("mode"))
        if img is not None:
            result = _commit_image(img, finalized, expected_gen=gen)
            if result is None:
                return True
            if result == "superseded":
                return False  # a fresh push landed; leave it on screen
        app.logger.warning("gallery recipe regen failed (%s); using snapshot", reason)
    # image kind, or recipe fell back: replay the cached snapshot.
    snap = os.path.join(GALLERY_DIR, entry.get("snapshot", ""))
    if os.path.exists(snap):
        try:
            img = Image.open(snap)
            img.load()
            return _commit_image(img, False, expected_gen=gen) is None
        except Exception:
            app.logger.exception("gallery snapshot load failed")
    return False


def _rotate_once():
    """Pick a random entry (never the same as last time) and show it."""
    cfg = _gallery_load()
    entries = cfg.get("entries", [])
    if not entries:
        return
    choices = entries
    if len(entries) > 1 and _rotation["last"] is not None:
        filtered = [e for e in entries if e["id"] != _rotation["last"]]
        if filtered:
            choices = filtered
    entry = random.choice(choices)
    if _show_entry(entry):
        _rotation["last"] = entry["id"]


def _rotation_loop():
    # Poll in short steps so interval changes take effect promptly and we don't
    # sit through a long stale sleep before the first rotation.
    elapsed = 0
    while True:
        time.sleep(1)
        elapsed += 1
        cfg = _gallery_load()
        interval = max(MIN_INTERVAL, int(cfg.get("interval", DEFAULT_INTERVAL)))
        _rotation["interval"] = interval
        if _rotation.get("reset_timer"):
            # An urgent push just landed; restart the interval so it shows for
            # a full slot before the gallery rotates back in.
            _rotation["reset_timer"] = False
            elapsed = 0
        if cfg.get("locked"):
            # Frozen on the current image; don't advance the timer either, so
            # rotation resumes a full interval after unlocking.
            elapsed = 0
            continue
        if elapsed < interval:
            continue
        elapsed = 0
        if not cfg.get("entries"):
            continue
        try:
            _rotate_once()
        except Exception:
            app.logger.exception("gallery rotation failed")


@app.post("/upload")
def upload():
    """Produce image.png from an uploaded image or text prompt.
    On ANY failure (bad input, generation error, compression error) we
    fall back to the default image rather than returning an error."""
    if not _authorized():
        return jsonify(error="unauthorized: valid upload token required"), 401
    _json = request.get_json(silent=True) or {} if request.is_json else {}
    keep = _truthy(request.values.get("keep")) or _truthy(_json.get("keep"))
    lock = _truthy(request.values.get("lock")) or _truthy(_json.get("lock"))
    urgent = _truthy(request.values.get("urgent")) or _truthy(_json.get("urgent"))
    try:
        img = None
        finalized = False  # True if img is already a finished 200x200 1-bit image
        kind = None        # "image" or "recipe" (for the gallery)
        keep_text = keep_mode = None

        # 1) image file via multipart/form-data
        if "image" in request.files and request.files["image"].filename:
            try:
                img = Image.open(request.files["image"].stream)
                img.load()
                kind = "image"
            except Exception:
                return _use_default("invalid image file")

        # 2) text via form field or JSON or raw body
        if img is None:
            text = request.form.get("text")
            mode = request.form.get("mode")
            if request.is_json:
                j = request.get_json(silent=True) or {}
                if text is None:
                    text = j.get("text")
                if mode is None:
                    mode = j.get("mode")
            if text is None and request.data:
                text = request.data.decode("utf-8", "replace")
            norm_mode = (mode or "markdown").strip().lower()
            # 'clock' generates its own content, so it needs no text input.
            if norm_mode != "clock" and (not text or not text.strip()):
                return _use_default("no input provided")

            keep_text, keep_mode = text, norm_mode
            kind = "recipe"
            img, finalized, reason = _render_text(text, mode)
            if img is None:
                return _use_default(reason)

        reason = _commit_image(img, finalized, urgent=urgent)
        if reason is not None:
            return _use_default(reason)
        if urgent:
            # Give the urgent image a full interval on screen before the
            # gallery rotates it away ("show once, then back to gallery").
            _rotation["reset_timer"] = True

        gid = None
        if keep and kind:
            if kind == "recipe":
                gid = _gallery_add("recipe", img, text=keep_text, mode=keep_mode)
            else:
                gid = _gallery_add("image", img)
        if lock:
            _set_locked(True)
        resp = dict(ok=True, bytes=os.path.getsize(IMAGE_PATH), url="/image.png")
        if gid:
            resp["kept"] = gid
        if lock:
            resp["locked"] = True
        resp["urgent"] = urgent
        return jsonify(resp)
    except Exception:
        app.logger.exception("unexpected upload error")
        return _use_default("unexpected error")


@app.get("/image.png")
def serve_image():
    if not os.path.exists(IMAGE_PATH):
        if os.path.exists(DEFAULT_PATH):
            return send_file(DEFAULT_PATH, mimetype="image/png", max_age=0)
        abort(404)
    return send_file(IMAGE_PATH, mimetype="image/png",
                     max_age=0, last_modified=os.path.getmtime(IMAGE_PATH))


@app.get("/urgent.txt")
def serve_urgent():
    """Plain-text 'true'/'false' indicating whether the current image was
    pushed with the urgent flag. Defaults to 'false'."""
    val = "false"
    if os.path.exists(URGENT_PATH):
        try:
            with open(URGENT_PATH) as f:
                val = "true" if f.read().strip().lower() == "true" else "false"
        except Exception:
            val = "false"
    resp = app.response_class(val, mimetype="text/plain")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/gallery")
def gallery_list():
    if not _authorized():
        return jsonify(error="unauthorized: valid upload token required"), 401
    cfg = _gallery_load()
    entries = [
        {"id": e["id"], "kind": e.get("kind"), "mode": e.get("mode"),
         "text": e.get("text"), "created": e.get("created")}
        for e in cfg.get("entries", [])
    ]
    return jsonify(count=len(entries), interval=cfg.get("interval"),
                   locked=cfg.get("locked", False), entries=entries)


@app.get("/gallery/<eid>.png")
def gallery_snapshot(eid):
    if not _authorized():
        return jsonify(error="unauthorized: valid upload token required"), 401
    if not re.fullmatch(r"[0-9a-f]{6,32}", eid):
        abort(404)
    path = os.path.join(GALLERY_DIR, eid + ".png")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/png", max_age=0)


@app.post("/gallery/interval")
def gallery_interval():
    if not _authorized():
        return jsonify(error="unauthorized: valid upload token required"), 401
    val = request.values.get("interval")
    if val is None and request.is_json:
        val = (request.get_json(silent=True) or {}).get("interval")
    try:
        interval = max(MIN_INTERVAL, int(val))
    except (TypeError, ValueError):
        return jsonify(error="interval must be an integer (seconds)"), 400
    with _io_lock:
        cfg = _gallery_load()
        cfg["interval"] = interval
        _gallery_save(cfg)
    _rotation["interval"] = interval
    return jsonify(ok=True, interval=interval)


@app.post("/gallery/lock")
def gallery_lock():
    if not _authorized():
        return jsonify(error="unauthorized: valid upload token required"), 401
    _set_locked(True)
    return jsonify(ok=True, locked=True)


@app.post("/gallery/unlock")
def gallery_unlock():
    if not _authorized():
        return jsonify(error="unauthorized: valid upload token required"), 401
    _set_locked(False)
    return jsonify(ok=True, locked=False)


@app.post("/gallery/clear")
def gallery_clear():
    if not _authorized():
        return jsonify(error="unauthorized: valid upload token required"), 401
    with _io_lock:
        cfg = _gallery_load()
        for e in cfg.get("entries", []):
            snap = os.path.join(GALLERY_DIR, e.get("snapshot", ""))
            if os.path.exists(snap):
                try:
                    os.remove(snap)
                except OSError:
                    pass
        cfg["entries"] = []
        _gallery_save(cfg)
    _rotation["last"] = None
    return jsonify(ok=True, count=0)


@app.post("/gallery/delete/<eid>")
def gallery_delete(eid):
    if not _authorized():
        return jsonify(error="unauthorized: valid upload token required"), 401
    with _io_lock:
        cfg = _gallery_load()
        kept, removed = [], 0
        for e in cfg.get("entries", []):
            if e["id"] == eid:
                snap = os.path.join(GALLERY_DIR, e.get("snapshot", ""))
                if os.path.exists(snap):
                    try:
                        os.remove(snap)
                    except OSError:
                        pass
                removed += 1
            else:
                kept.append(e)
        cfg["entries"] = kept
        _gallery_save(cfg)
    return jsonify(ok=True, removed=removed, count=len(kept))


GALLERY_SEED_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "gallery_default.png")


def _seed_gallery():
    """Ensure the baked-in default photo is the first gallery entry. Marked
    with seed=True and only added once (tracked in config) so clearing the
    gallery keeps it gone if the user wants."""
    cfg = _gallery_load()
    if cfg.get("seeded"):
        return
    if os.path.exists(GALLERY_SEED_PATH):
        try:
            img = Image.open(GALLERY_SEED_PATH)
            img.load()
            with _io_lock:
                cfg = _gallery_load()
                if cfg.get("seeded"):
                    return
                eid = "5eed0defa017"  # hex so gallery_snapshot's [0-9a-f] gate accepts it
                snap = os.path.join(GALLERY_DIR, eid + ".png")
                img.convert("RGB").save(snap, "PNG", optimize=True)
                cfg["entries"].insert(0, {
                    "id": eid, "kind": "image", "snapshot": eid + ".png",
                    "created": time.time(), "seed": True,
                })
                cfg["seeded"] = True
                _gallery_save(cfg)
        except Exception:
            app.logger.exception("gallery seed failed")


def _start_rotation():
    _seed_gallery()
    cfg = _gallery_load()
    _rotation["interval"] = max(MIN_INTERVAL, int(cfg.get("interval", DEFAULT_INTERVAL)))
    t = threading.Thread(target=_rotation_loop, name="gallery-rotation",
                         daemon=True)
    t.start()


_start_rotation()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
