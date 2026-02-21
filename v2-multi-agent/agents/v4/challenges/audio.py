"""Audio challenge: play audio, wait for completion, click Complete, extract code.

Key insight: audio takes variable time (2-6s). Must POLL for completion instead
of fixed waits. Check for "played"/"play again" text or enabled Complete button.

Reliability: After clicking Play, verify audio.paused === false. If the play click
didn't register (popup interference, coordinate miss), retry with JS el.click()
and audio.play() as fallbacks.

IMPORTANT: "Complete Challenge" may be a <p> tag (not <button>) on some versions.
Must use get_by_text() or querySelectorAll('*') — not just button selectors.
"""

from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, js_click_button_by_text, query_buttons_in_scope,
    wait_for_code_mutation, extract_code, SUBMIT_EXCLUDE, get_progress_fraction,
)
from log import log


def _ensure_audio_playing(page, boundary_y: int) -> bool:
    """Click Play and verify audio actually started. Retries with fallbacks.

    Returns True if audio is playing (paused === false or no <audio> element).
    """
    play_keywords = ['play audio', 'play', 'listen', 'start audio', 'start', 'audio']

    # Tier 1: Playwright mouse click
    click_button_by_text(page, play_keywords, boundary_y)
    page.wait_for_timeout(300)

    # Check if audio started
    audio_info = page.evaluate('''() => {
        const audio = document.querySelector('audio');
        if (!audio) return {exists: false, playing: true};
        return {exists: true, playing: !audio.paused,
                currentTime: audio.currentTime, duration: audio.duration};
    }''')
    if audio_info.get('playing'):
        log(f"  [audio] audio element exists={audio_info.get('exists')}")
        return True

    # Tier 2: JS el.click() on play button (bypasses overlay issues)
    log("  [audio] play click missed, retrying with JS click")
    js_click_button_by_text(page, play_keywords, boundary_y)
    page.wait_for_timeout(300)

    playing = page.evaluate('''() => {
        const audio = document.querySelector('audio');
        if (!audio) return true;
        return !audio.paused;
    }''')
    if playing:
        return True

    # Tier 3: Direct audio.play() — standard DOM API, works on any website
    log("  [audio] JS click missed, trying audio.play()")
    try:
        page.evaluate('''() => {
            const audio = document.querySelector('audio');
            if (audio) audio.play();
        }''')
        page.wait_for_timeout(300)
        playing = page.evaluate('''() => {
            const audio = document.querySelector('audio');
            if (!audio) return true;
            return !audio.paused;
        }''')
        if playing:
            return True
    except Exception:
        pass

    return False


def _poll_audio_completion(page, boundary_y: int, max_ms: int = 15000) -> str:
    """Wait for audio to finish playing via RAF polling (~16ms).

    Returns 'played', 'complete_ready', or 'timeout'.

    Checks broadly: text patterns (many wordings), audio.ended, audio.paused
    after play, AND any element containing "Complete" (not just <button>).
    """
    try:
        result = page.wait_for_function(
            r'''() => {
                const text = document.body.innerText.toLowerCase();
                // Broad text detection — covers many possible wordings
                if (text.includes('audio played') || text.includes('played!') ||
                    text.includes('play again') || text.includes('listened') ||
                    text.includes('audio complete') || text.includes('audio finished') ||
                    text.includes('the real code is')) return 'played';
                const audio = document.querySelector('audio');
                if (audio && audio.ended) return 'played';
                // Audio finished but .ended not set (paused after playing)
                if (audio && audio.paused && audio.currentTime > 0) return 'played';
                if (audio && audio.currentTime > 0 && audio.duration > 0 &&
                    audio.currentTime >= audio.duration - 0.1) return 'played';
                // Check ALL elements for "Complete" — not just <button>
                // "Complete Challenge" may be a <p>, <div>, <span>, etc.
                const all = document.querySelectorAll(
                    'button, [role="button"], p, div, span');
                for (const el of all) {
                    const t = (el.innerText || '').trim().toLowerCase();
                    if (t === 'complete challenge' || t === 'complete') {
                        const r = el.getBoundingClientRect();
                        if (r.width > 10 && r.height > 10) return 'complete_ready';
                    }
                }
                return null;
            }''',
            polling='raf',
            timeout=max_ms,
        )
        return result.json_value() or 'timeout'
    except Exception:
        return 'timeout'


def _click_complete_any_element(page, boundary_y: int) -> bool:
    """Click "Complete Challenge" / "Complete" regardless of element type.

    Uses Playwright get_by_text (matches any element), then falls back to
    querySelectorAll('*') with el.click(). Returns True if clicked.
    """
    # Strategy 1: Playwright get_by_text — works on any element type
    for text in ['Complete Challenge', 'Complete']:
        try:
            loc = page.get_by_text(text, exact=True).first
            bbox = loc.bounding_box(timeout=500)
            if bbox and (bbox['y'] + bbox['height'] / 2) < boundary_y:
                loc.click(timeout=1000)
                log(f"  [audio] clicked '{text}' via get_by_text")
                return True
        except Exception:
            continue

    # Strategy 2: JS el.click() on ANY element with EXACT "Complete" text
    # Must use exact match — t.includes('complete') matches instruction paragraphs
    clicked = page.evaluate(f'''(boundaryY) => {{
        const all = document.querySelectorAll(
            'button, [role="button"], p, div, span, a, label');
        for (const el of all) {{
            const t = (el.innerText || '').trim().toLowerCase();
            if (t !== 'complete challenge' && t !== 'complete' &&
                t !== 'done' && t !== 'finish') continue;
            const r = el.getBoundingClientRect();
            if (r.width < 10 || r.height < 10) continue;
            if (r.top + r.height/2 > boundaryY) continue;
            el.click();
            return t;
        }}
        return null;
    }}''', boundary_y)
    if clicked:
        log(f"  [audio] JS-clicked '{clicked}' (any element)")
        return True

    # Strategy 3: Original button-only search as final fallback
    complete_keywords = ['complete challenge', 'complete', 'done', 'finish',
                         'reveal', 'show code']
    js_clicked = js_click_button_by_text(page, complete_keywords, boundary_y)
    if js_clicked:
        log(f"  [audio] clicked '{js_clicked}' button")
        return True
    if click_button_by_text(page, complete_keywords, boundary_y):
        log(f"  [audio] clicked complete via mouse")
        return True

    return False


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Click Play Audio with verification and retry
    playing = _ensure_audio_playing(page, boundary_y)
    log(f"step {ctx.step}: [audio] play verified={playing}")

    # Check if audio element exists
    has_audio_el = page.evaluate('() => !!document.querySelector("audio")')
    if not has_audio_el:
        # Audio element may load dynamically after Play click — wait briefly
        log(f"step {ctx.step}: [audio] no <audio> element yet, waiting 1s for dynamic load")
        page.wait_for_timeout(1000)
        has_audio_el = page.evaluate('() => !!document.querySelector("audio")')
        if has_audio_el:
            log(f"step {ctx.step}: [audio] <audio> appeared after delay")
        else:
            log(f"step {ctx.step}: [audio] NO_AUDIO_ELEMENT — using text-based poll")

    # Poll for audio completion via text detection (works with or without <audio> element).
    # Text-based checks: "audio played", "played!", "play again", "Complete Challenge", etc.
    state = _poll_audio_completion(page, boundary_y)
    log(f"step {ctx.step}: [audio] audio state: {state}")

    # If timed out, dump diagnostic info for debugging
    if state == 'timeout':
        diag = page.evaluate('''() => {
            const audio = document.querySelector('audio');
            const audioState = audio ? {
                paused: audio.paused, ended: audio.ended,
                currentTime: audio.currentTime, duration: audio.duration,
                src: (audio.src || '').substring(0, 80)
            } : 'NO_AUDIO_ELEMENT';
            // Check for "Complete" text in any element
            let completeEls = [];
            for (const el of document.querySelectorAll('*')) {
                const t = (el.innerText || '').trim();
                if (t.toLowerCase().includes('complete') && t.length < 30) {
                    completeEls.push({tag: el.tagName, text: t,
                        w: Math.round(el.getBoundingClientRect().width)});
                    if (completeEls.length >= 5) break;
                }
            }
            // Get text near audio area (first 500 chars of challenge area)
            const text = document.body.innerText.substring(0, 500);
            return {audio: audioState, completeEls, textSnip: text};
        }''')
        log(f"step {ctx.step}: [audio] TIMEOUT diag: audio={diag.get('audio')}")
        log(f"step {ctx.step}: [audio] TIMEOUT complete elements: {diag.get('completeEls')}")
        log(f"step {ctx.step}: [audio] TIMEOUT text: {diag.get('textSnip', '')[:200]}")

        # Check if audio is still playing and give more time
        still_playing = page.evaluate('''() => {
            const audio = document.querySelector('audio');
            return audio && !audio.paused && !audio.ended;
        }''')
        if still_playing:
            log(f"step {ctx.step}: [audio] still playing, waiting 5s more")
            state = _poll_audio_completion(page, boundary_y, max_ms=5000)
            log(f"step {ctx.step}: [audio] audio state after extra wait: {state}")

    # Check if code appeared during playback
    code = extract_code(page)
    if code:
        return code

    # Click Complete — search ANY element type (not just buttons)
    _click_complete_any_element(page, boundary_y)
    page.wait_for_timeout(500)

    code = extract_code(page) or wait_for_code_mutation(page, 2000)
    if code:
        return code

    # Fallback: scroll down to check for code below viewport (only if no progress)
    if get_progress_fraction(page) < 0.5:
        try:
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(300)
            code = extract_code(page)
            if code:
                return code
            page.mouse.wheel(0, -300)
        except Exception:
            pass

    # Last resort: wait longer and try Complete again
    if state == 'timeout':
        page.wait_for_timeout(2000)
        _click_complete_any_element(page, boundary_y)
        page.wait_for_timeout(500)
        return extract_code(page) or wait_for_code_mutation(page, 2000)

    return None
