"""scope_check.py — Validates boundary detection across all 30 steps.

For each step: computes boundary_y, reports what buttons/inputs are inside
vs outside scope. Catches bugs like "puzzle input exists but outside boundary."

Usage:
    python scope_check.py --headed
    python scope_check.py --headed --max-steps 15
"""
import sys
import io
import re
import time
import argparse

from dotenv import load_dotenv
load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

from playwright.sync_api import sync_playwright
from orchestrator import Orchestrator
from config import get_challenge_type, generate_code
from verify import extract_step_from_url, extract_version_from_url, is_finish_page
from page_state import get_challenge_text
from agents.v4.helpers import compute_challenge_scope, SUBMIT_EXCLUDE


def scope_check(headed: bool = True, max_steps: int = 30):
    orch = Orchestrator()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page, version = orch.start_fresh(browser)
        print(f"Started v={version}\n", flush=True)

        results = []

        for step in range(1, max_steps + 1):
            url_step = extract_step_from_url(page.url)
            if url_step is None:
                if is_finish_page(page):
                    print("CHALLENGE COMPLETE!", flush=True)
                break
            if url_step != step:
                step = url_step

            ver = extract_version_from_url(page.url)
            if ver:
                version = ver

            # Setup step (hooks, popups, etc)
            orch._step_setup(page, step, version)
            page.wait_for_timeout(200)

            config_type = get_challenge_type(step, version)
            instruction = get_challenge_text(page, limit=300)

            # Compute scope
            _sel, boundary_y = compute_challenge_scope(page)

            # Get all buttons and inputs
            buttons = page.evaluate(r'''() => {
                return [...document.querySelectorAll('button, [role="button"]')].map(b => {
                    const r = b.getBoundingClientRect();
                    return {
                        text: (b.textContent || '').trim().substring(0, 60),
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        disabled: b.disabled,
                    };
                }).filter(b => b.w > 0 && b.h > 0);
            }''') or []

            inputs = page.evaluate(r'''() => {
                return [...document.querySelectorAll(
                    'input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]), '
                    + 'textarea, [contenteditable="true"]'
                )].map(el => {
                    const r = el.getBoundingClientRect();
                    return {
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        type: el.type || 'textarea',
                        placeholder: el.placeholder || '',
                        maxLength: el.maxLength >= 0 ? el.maxLength : null,
                    };
                }).filter(i => i.w > 0 && i.h > 0);
            }''') or []

            # Classify
            in_btns = [b for b in buttons if b['y'] < boundary_y
                       and b['text'].lower() not in {e.lower() for e in SUBMIT_EXCLUDE}]
            out_btns = [b for b in buttons if b['y'] >= boundary_y
                        and b['text'].lower() not in {e.lower() for e in SUBMIT_EXCLUDE}]
            submit_btns = [b for b in buttons
                           if b['text'].lower() in {e.lower() for e in SUBMIT_EXCLUDE}]

            in_inputs = [i for i in inputs if i['y'] < boundary_y]
            out_inputs = [i for i in inputs if i['y'] >= boundary_y]

            # Warnings
            warnings = []

            # Non-submit buttons outside scope might be challenge buttons
            for b in out_btns:
                t = b['text'].lower()
                # Skip generic UI buttons
                if any(kw in t for kw in ('next step', 'continue', 'submit')):
                    continue
                warnings.append(f"OUT-OF-SCOPE button: '{b['text']}' at y={b['y']}")

            # Non-code inputs outside scope might be challenge inputs
            for inp in out_inputs:
                if inp.get('maxLength') == 6:
                    continue  # code submission field — expected outside scope
                warnings.append(
                    f"OUT-OF-SCOPE input: type={inp['type']} "
                    f"placeholder='{inp['placeholder']}' at y={inp['y']} "
                    f"maxLen={inp.get('maxLength')}")

            # No in-scope buttons at all
            if not in_btns and config_type not in ('scroll', 'delayed_reveal', 'decode',
                                                    'calculated', 'puzzle_solve', 'gesture',
                                                    'keyboard_sequence'):
                warnings.append("NO in-scope buttons (agent may not find anything to click)")

            # Boundary is suspiciously low (< 200px)
            if boundary_y < 200:
                warnings.append(f"boundary_y={boundary_y} is very low — may cut off challenge")

            # No boundary found
            if boundary_y >= 99999:
                warnings.append("No boundary detected — agent will include all elements")

            status = "WARN" if warnings else "OK"
            tag = f"[{status:4s}]"

            print(f"Step {step:2d} (v{version}) [{config_type[:12]:12s}] "
                  f"boundary_y={boundary_y:5d} "
                  f"btns={len(in_btns):2d}in/{len(out_btns):2d}out "
                  f"inputs={len(in_inputs):1d}in/{len(out_inputs):1d}out "
                  f"{tag}", flush=True)

            for w in warnings:
                print(f"         {w}", flush=True)

            results.append({
                'step': step, 'version': version, 'config_type': config_type,
                'boundary_y': boundary_y,
                'in_buttons': len(in_btns), 'out_buttons': len(out_btns),
                'in_inputs': len(in_inputs), 'out_inputs': len(out_inputs),
                'warnings': warnings,
            })

            # Advance: submit correct code
            code = generate_code(step, version)
            current_url = page.url
            success, _reason = orch._submit_and_record(page, code, step, version,
                                                        current_url, 'scope_check')
            if not success:
                orch.run_step(page, step, version)
            page.wait_for_timeout(300)

        # Summary
        warn_count = sum(1 for r in results if r['warnings'])
        print(f"\n{'='*60}")
        print(f"SCOPE CHECK: {len(results)} steps, {warn_count} with warnings")
        if warn_count:
            print(f"\nSteps with warnings:")
            for r in results:
                if r['warnings']:
                    print(f"  Step {r['step']:2d} [{r['config_type'][:12]:12s}]: "
                          f"{'; '.join(r['warnings'][:2])}")
        print(f"{'='*60}")

        browser.close()


def main():
    parser = argparse.ArgumentParser(description="Validate challenge scope boundaries")
    parser.add_argument("--headed", action="store_true", help="Show browser")
    parser.add_argument("--max-steps", type=int, default=30)
    args = parser.parse_args()
    scope_check(headed=args.headed, max_steps=args.max_steps)


if __name__ == "__main__":
    main()
