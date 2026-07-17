#!/usr/bin/env python3
"""Prepare an image for the ESP32-S3 e-paper dashboard (200x200, 1-bit).

The firmware does NOT resize and only does a flat luma->Floyd dither, so busy
or midtone-heavy photos turn to noise. This script does the real work:
resize-to-fill 200x200, contrast/brightness, sharpen, then (optionally) dither
to 1-bit so the device displays it almost verbatim.

Usage:
    python3 prepare_image.py INPUT [-o image.png] [options]

INPUT may be a local path or an http(s) URL.

Examples:
    python3 prepare_image.py photo.jpg -o image.png            # 1-bit, ready to serve
    python3 prepare_image.py photo.jpg -o image.png --grayscale # let the device dither
    python3 prepare_image.py https://…/pic.png --contrast 1.3 --brightness 1.0

Requires Pillow:  pip install Pillow
"""
import argparse
import io
import sys
import urllib.request

from PIL import Image, ImageOps, ImageEnhance, ImageFilter

SIZE = 200  # panel is 200x200


def load(src):
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=15) as r:
            return Image.open(io.BytesIO(r.read()))
    return Image.open(src)


def prepare(src, size, contrast, brightness, sharpen, autocontrast, one_bit):
    img = load(src).convert("RGB")

    # Resize to FILL the panel (cover), then center-crop — no distortion, no
    # accidental top-left clipping like the firmware's raw crop.
    img = ImageOps.fit(img, (size, size), Image.LANCZOS, centering=(0.5, 0.5))

    g = img.convert("L")  # ITU-R 601-2 luma, same coefficients as the firmware
    if autocontrast:
        g = ImageOps.autocontrast(g, cutoff=2)
    if contrast != 1.0:
        g = ImageEnhance.Contrast(g).enhance(contrast)
    if brightness != 1.0:
        g = ImageEnhance.Brightness(g).enhance(brightness)
    if sharpen > 0:
        g = g.filter(ImageFilter.UnsharpMask(radius=2, percent=sharpen, threshold=2))

    if one_bit:
        return g.convert("1")  # Pillow uses Floyd-Steinberg, threshold 128
    return g


def main():
    p = argparse.ArgumentParser(description="Prepare an image for the 200x200 1-bit e-paper.")
    p.add_argument("input", help="image path or http(s) URL")
    p.add_argument("-o", "--output", default="image.png", help="output PNG (default image.png)")
    p.add_argument("--grayscale", action="store_true",
                   help="output 8-bit grayscale and let the device dither (default: 1-bit)")
    p.add_argument("--contrast", type=float, default=1.6, help="contrast multiplier (default 1.6)")
    p.add_argument("--brightness", type=float, default=1.15, help="brightness multiplier (default 1.15)")
    p.add_argument("--sharpen", type=int, default=150, help="unsharp percent, 0 to disable (default 150)")
    p.add_argument("--no-autocontrast", dest="autocontrast", action="store_false",
                   help="skip the autocontrast stretch")
    p.add_argument("--size", type=int, default=SIZE, help="square output size (default 200)")
    args = p.parse_args()

    try:
        out = prepare(args.input, args.size, args.contrast, args.brightness,
                      args.sharpen, args.autocontrast, one_bit=not args.grayscale)
    except Exception as e:
        sys.exit("error: %s" % e)

    # bits=1 keeps the 1-bit PNG tiny; harmless for grayscale.
    out.save(args.output, "PNG", optimize=True, bits=1 if out.mode == "1" else 8)
    print("wrote %s (%s, %dx%d)" % (args.output, out.mode, out.width, out.height))


if __name__ == "__main__":
    main()
