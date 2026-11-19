# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.1] - 2026-11-19

### Added

- This changelog.
- A prebuilt firmware image attached to the GitHub release. It is built with the
  placeholder values from `firmware/.env.example`, so it does not join a
  network. For a working device, build from source with your own
  `firmware/.env`.

## [0.10.0] - 2026-11-19

First public release, tagged `v0.10`.

### Added

- ESP32-S3 firmware for the Waveshare 1.54" e-paper board: it wakes, fetches a
  PNG from one URL, shows it with a battery percentage overlay, and goes back to
  deep sleep.
- Optional urgent sound alert: the device plays an embedded chime when the
  server flags the current image as urgent.
- Flask server that turns an uploaded image, Markdown, plain text, a FLUX
  prompt, an LLM answer or live weather into one 200×200 1-bit PNG at a fixed
  URL.
- Rotating gallery of saved pushes, with lock, unlock and interval control.
- `glimpse` shell CLI that pushes to the server.

[0.10.1]: https://github.com/caffo/glimpse/compare/v0.10...v0.10.1
[0.10.0]: https://github.com/caffo/glimpse/releases/tag/v0.10
