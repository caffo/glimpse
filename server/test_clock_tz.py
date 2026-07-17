"""Self-check for TZ handling: python3 test_clock_tz.py

Runs each case in a subprocess because app.py resolves TZ once at import.
"""
import subprocess
import sys
import textwrap

HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))


def run(tz, snippet):
    """Import app.py with TZ set and evaluate snippet against it."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        cwd=HERE, capture_output=True, text=True,
        env={**__import__("os").environ, "TZ": tz},
    )


# A real IANA name resolves, and the clock renders in that zone.
r = run("America/Sao_Paulo", """
    import app, datetime
    assert str(app.CLOCK_TZ) == "America/Sao_Paulo", app.CLOCK_TZ
    md = app._clock_markdown()
    assert md.startswith("# Right now"), md
    # -3 in standard time; the point is it comes from tzdata, not a hardcoded offset.
    off = datetime.datetime.now(app.CLOCK_TZ).utcoffset().total_seconds() / 3600
    assert off in (-3, -2), off
    print("ok")
""")
assert "ok" in r.stdout, f"Sao_Paulo case failed:\n{r.stdout}\n{r.stderr}"

# DST is tracked, unlike the fixed offset this replaced: Lisbon shifts Jan vs Jul.
r = run("Europe/Lisbon", """
    import app, datetime
    jan = datetime.datetime(2026, 1, 15, 12, tzinfo=app.CLOCK_TZ).utcoffset()
    jul = datetime.datetime(2026, 7, 15, 12, tzinfo=app.CLOCK_TZ).utcoffset()
    assert jan != jul, f"expected DST shift, got {jan} both times"
    print("ok")
""")
assert "ok" in r.stdout, f"DST case failed:\n{r.stdout}\n{r.stderr}"

# Unset TZ falls back to UTC rather than exploding.
r = subprocess.run(
    [sys.executable, "-c", "import app; print(app.CLOCK_TZ)"],
    cwd=HERE, capture_output=True, text=True,
    env={k: v for k, v in __import__("os").environ.items() if k != "TZ"},
)
assert "UTC" in r.stdout, f"default case failed:\n{r.stdout}\n{r.stderr}"

# A typo fails loudly at startup instead of silently serving UTC.
r = run("Mars/Olympus_Mons", "import app")
assert r.returncode != 0, "expected a bad TZ to exit nonzero"
assert "not a known IANA timezone" in r.stderr, r.stderr

print("all ok")
