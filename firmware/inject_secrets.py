"""PlatformIO pre-script: inject .env config as compile-time build flags.

Reads config from the process environment (so `source .env` works) or, failing
that, directly from a .env file in the project root.

  REQUIRED_KEYS        -> quoted C strings (e.g. -DWIFI_SSID="net"); build aborts if missing
  NUMERIC_KEYS         -> injected verbatim (e.g. -DUPDATE_INTERVAL=300); optional, has default
  OPTIONAL_STRING_KEYS -> quoted C strings, only when set (e.g. -DURGENT_URL="...")
"""
import os

Import("env")  # noqa: F821  (provided by PlatformIO/SCons)

REQUIRED_KEYS = ("WIFI_SSID", "WIFI_PASSWORD", "IMAGE_URL")
NUMERIC_KEYS = ("UPDATE_INTERVAL", "URGENT_VOLUME")
# Optional string flags: injected as quoted C strings only when present. URGENT_URL
# gates the whole sound-alert feature — leave it unset to build the plain dashboard.
OPTIONAL_STRING_KEYS = ("URGENT_URL",)


def load_dotenv(path):
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


dotenv = load_dotenv(os.path.join(env.subst("$PROJECT_DIR"), ".env"))


def lookup(key):
    return os.environ.get(key) or dotenv.get(key)


# Required config — fail fast with a clear message rather than building a useless
# firmware. (firmware.ino also #errors on these, this is just a friendlier abort.)
missing = [k for k in REQUIRED_KEYS if not lookup(k)]
if missing:
    print("\n*** Missing required config: %s" % ", ".join(missing))
    print("*** Copy .env.example to .env and fill it in, or export the vars.\n")
    env.Exit(1)

for key in REQUIRED_KEYS:
    value = lookup(key)
    # StringifyMacro handles quoting/escaping so the C string is well-formed.
    env.Append(CPPDEFINES=[(key, env.StringifyMacro(value))])
    print("config: injected %s" % key)

for key in NUMERIC_KEYS:
    value = lookup(key)
    if value:
        env.Append(CPPDEFINES=[(key, value)])  # numeric -> no quotes
        print("config: injected %s=%s" % (key, value))
    else:
        print("config: %s not set, using firmware.ino fallback" % key)

for key in OPTIONAL_STRING_KEYS:
    value = lookup(key)
    if value:
        env.Append(CPPDEFINES=[(key, env.StringifyMacro(value))])
        print("config: injected %s" % key)
    else:
        print("config: %s not set, feature disabled" % key)
