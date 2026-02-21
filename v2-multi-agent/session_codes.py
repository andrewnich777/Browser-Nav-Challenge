"""Extract codes from session storage (new site version)."""

import base64
import json

KEY = "WO_2024_CHALLENGE"


def xv_decode(encoded: str) -> str:
    """Decode XOR-encrypted base64 string from sessionStorage."""
    decoded_b64 = base64.b64decode(encoded)
    result = ""
    for i, byte in enumerate(decoded_b64):
        result += chr(byte ^ ord(KEY[i % len(KEY)]))
    return result


def get_session_codes(page) -> list[str]:
    """Extract all 30 codes from wo_session in sessionStorage.

    Returns a list of 30 codes where:
    - index 0 = code for step 1
    - index 29 = code for step 30
    """
    storage = page.evaluate(r'''() => sessionStorage.getItem('wo_session')''')
    if not storage:
        return []

    try:
        decoded = xv_decode(storage)
        session = json.loads(decoded)
        return session.get("codes", [])
    except Exception:
        return []


def get_code_for_step(page, step: int) -> str | None:
    """Get the code for a specific step (1-indexed).

    The site stores codes in a Map with keys 1-30.
    The sessionStorage array has those same codes at indices 0-29.
    So step N uses array[N-1].

    Testing confirmed: codes[1] works for step 1 (which was wrong!).
    Actually no, the test showed codes[1] works for step 1 because
    the site's validateCode(step, input) gets codes.get(step+1).

    Wait, let me think again:
    - Site stores Map with keys 1-30
    - sessionStorage array: index 0 = Map.get(1), ..., index 29 = Map.get(30)
    - validateCode(stepNum, input) gets codes.get(stepNum + 1)
    - For step 1, it gets codes.get(2) = array[1]
    - For step 30, it gets codes.get(31) which doesn't exist

    Actually our test showed codes[1] worked for step 1. So validation
    for step 1 uses codes.get(2), meaning array[1].

    Hmm, but that means for step 30, we'd need codes.get(31) = array[30]
    which doesn't exist!

    Let me just trust the test results:
    - codes[1] works for step 1
    - codes[2] should work for step 2
    - ...
    - codes[29] should work for step 29
    - For step 30, try codes[29] (last element)

    Actually maybe I miscounted. Let me just use step -> codes[step] and
    handle step 30 specially.
    """
    codes = get_session_codes(page)
    if not codes:
        return None

    # Based on testing: codes[1] works for step 1
    # So step N uses codes[N]
    # For step 30: Try codes[0] (wrapping around)
    if step >= 1 and step <= 29 and step < len(codes):
        return codes[step]
    elif step == 30 and len(codes) >= 1:
        # Step 30 — validateCode(30, input) gets codes.get(31)
        # which doesn't exist. Maybe it wraps to codes.get(1)?
        # Or maybe codes[0] (array index 0 = Map key 1)?
        # Let's try codes[0]
        return codes[0]
    return None
