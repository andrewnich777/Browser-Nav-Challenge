"""Verification utilities — compare extracted codes against answer key."""

import re
from config import generate_code


def verify_code(extracted: str, step: int, version: int) -> bool:
    expected = generate_code(step, version)
    return extracted.strip().upper() == expected.upper()


def extract_code_from_text(text: str) -> str | None:
    """Try to extract a 6-char code from page text using the known charset."""
    # Look for 6-char sequences matching our charset
    matches = re.findall(r'\b([A-HJ-NP-Z2-9]{6})\b', text)
    return matches[0] if matches else None


def extract_step_from_url(url: str) -> int | None:
    m = re.search(r'step(\d+)', url)
    return int(m.group(1)) if m else None


def extract_version_from_url(url: str) -> int | None:
    m = re.search(r'version=(\d+)', url)
    return int(m.group(1)) if m else None


def is_finish_page(page) -> bool:
    url = page.url
    if '/finish' in url or '/complete' in url or '/done' in url:
        return True
    # Also check if URL has no step (might be home/finish)
    if '/step' not in url and 'netlify' in url:
        try:
            body = page.inner_text('body')[:500].lower()
            # Check for finish indicators
            finish_words = ['congratulat', 'complete', 'finished', 'well done', 'success', 'you did it']
            if any(w in body for w in finish_words):
                return True
            # If no "Step X of 30" text, likely finish page
            if 'step' not in body or 'of 30' not in body:
                return True
        except Exception:
            pass
    return False
