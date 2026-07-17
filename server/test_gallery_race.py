"""Verify the generation guard that stops a slow gallery render from clobbering
a fresh push (e.g. an urgent alert). Run: python3 test_gallery_race.py"""
import os
from PIL import Image

import app

WHITE = lambda: Image.new("1", app.SIZE, 1)
BLACK = lambda: Image.new("1", app.SIZE, 0)

# Preserve any real runtime image so the test is non-destructive.
_backup = open(app.IMAGE_PATH, "rb").read() if os.path.exists(app.IMAGE_PATH) else None
try:
    # A successful commit bumps the generation counter.
    g0 = app._rotation["gen"]
    assert app._commit_image(WHITE(), True) is None
    assert app._rotation["gen"] == g0 + 1

    # The race: a rotation snapshots the gen, then a push commits, then the
    # rotation's stale render tries to land -> it must be dropped, not written.
    snap = app._rotation["gen"]
    assert app._commit_image(WHITE(), True) is None          # the push lands
    before = open(app.IMAGE_PATH, "rb").read()
    assert app._commit_image(BLACK(), True, expected_gen=snap) == "superseded"
    assert open(app.IMAGE_PATH, "rb").read() == before       # push left intact

    # A matching generation still commits normally.
    now = app._rotation["gen"]
    assert app._commit_image(BLACK(), True, expected_gen=now) is None

    print("ok")
finally:
    if _backup is not None:
        with open(app.IMAGE_PATH, "wb") as f:
            f.write(_backup)
