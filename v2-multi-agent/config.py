"""Configuration constants for the browser challenge solver."""

import os

BASE_URL = "https://serene-frangipane-7fd25b.netlify.app"
CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

TYPES_16_20 = ["multi_tab", "gesture", "sequence", "puzzle_solve", "calculated"]
TYPES_21_30 = [
    "shadow_dom", "websocket", "service_worker", "mutation",
    "recursive_iframe", "conditional_reveal", "multi_tab",
    "sequence", "calculated",
]

# Browser settings
VIEWPORT = {"width": 1280, "height": 1024}
HEADLESS = True

# Debug — set False to strip all verbose output
DEBUG = True
STEP_TIMEOUT = 15  # seconds, per step max before giving up (raise for prod)
FINAL_STEP = 30    # last challenge step — /finish navigation auto-appended

# Timing (ms) — keep minimal
POPUP_WAIT = 200
POST_SUBMIT_WAIT = 400
POST_SCROLL_WAIT = 250
STALE_FIX_WAIT = 800


def generate_code(step: int, version: int = 1) -> str:
    o = step + 1
    l = version
    d = (o * 7919 + 12345) * l
    f = (o * 1237 + 67890) * l
    p = (o * 4567 + 98765) * l
    code = ""
    for h in range(6):
        y = (d * (h + 1) + f * (h * 2 + 1) + p * (h * 3 + 2)) % 2147483647 % len(CHARSET)
        code += CHARSET[abs(y)]
    return code


def get_challenge_type(step: int, version: int) -> str:
    if step <= 15:
        return "simple"
    elif step <= 20:
        return TYPES_16_20[(step - 16 + version - 1) % len(TYPES_16_20)]
    else:
        return TYPES_21_30[(step - 21 + version - 1) % len(TYPES_21_30)]


def is_multi_tab(step: int, version: int) -> bool:
    return get_challenge_type(step, version) == "multi_tab"


# Vision API settings
VISION_ENABLED = os.getenv("VISION_ENABLED", "true").lower() == "true"
VISION_MODEL = os.getenv("VISION_MODEL", "claude-sonnet-4-5-20250929")
VISION_MIN_CONFIDENCE = float(os.getenv("VISION_MIN_CONFIDENCE", "0.5"))
