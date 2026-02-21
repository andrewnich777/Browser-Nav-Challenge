"""batch_diagnose.py — Run diagnose on specific challenge types across steps 1-15.

Scans steps 1-15 to detect which challenge types appear (they're all "simple" in config
but get detected at runtime). Then runs full diagnose on the first occurrence of each
target type.

Usage:
    python batch_diagnose.py --types drag_drop audio gesture sequence shadow_dom recursive_iframe
"""

import sys
import io
import time
import argparse

from dotenv import load_dotenv
load_dotenv()

try:
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
except Exception:
    pass

from playwright.sync_api import sync_playwright
from orchestrator import Orchestrator
from config import get_challenge_type, generate_code
from verify import extract_step_from_url, extract_version_from_url
from page_state import get_challenge_text
from agents.v4 import CHALLENGE_AGENTS, StepCtx
from agents.v4.helpers import compute_challenge_scope
from diagnose import diagnose_step


def scan_steps_1_15(orch, page, version):
    """Fast-scan steps 1-15 to find which challenge types are detected."""
    type_map = {}
    for step in range(1, 16):
        current = extract_step_from_url(page.url)
        if current != step:
            break

        instruction = get_challenge_text(page, limit=500)
        scope_sel, boundary_y = compute_challenge_scope(page)
        detected = orch._detect_type_for_v4(page, step, version, instruction)
        type_map[step] = detected
        print(f"  step {step}: detected={detected}", flush=True)

        # Fast-forward with deterministic code
        code = generate_code(step, version)
        current_url = page.url
        orch._step_setup(page, step, version)
        orch._submit_and_record(page, code, step, version, current_url, 'scan')
        page.wait_for_timeout(300)

        next_step = extract_step_from_url(page.url)
        if next_step is None or next_step <= step:
            break

    return type_map


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--types", nargs="+", required=True,
                        help="Challenge types to diagnose")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    target_types = set(args.types)

    # Phase 1: Scan steps 1-15 to find target types
    print("=== PHASE 1: Scanning steps 1-15 for target types ===", flush=True)
    orch = Orchestrator()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page, version = orch.start_fresh(browser)
        print(f"Started v={version}", flush=True)

        type_map = scan_steps_1_15(orch, page, version)
        browser.close()

    # Also add known steps 16-30
    for step in range(16, 31):
        ctype = get_challenge_type(step, version)
        if ctype not in type_map.values():
            type_map[step] = ctype

    print(f"\n=== TYPE MAP (v={version}) ===", flush=True)
    for step, ctype in sorted(type_map.items()):
        marker = " <<<" if ctype in target_types else ""
        print(f"  step {step}: {ctype}{marker}", flush=True)

    # Phase 2: Diagnose first occurrence of each target type
    steps_to_diagnose = {}
    for step, ctype in sorted(type_map.items()):
        if ctype in target_types and ctype not in steps_to_diagnose:
            steps_to_diagnose[ctype] = step

    print(f"\n=== PHASE 2: Diagnosing {len(steps_to_diagnose)} types ===", flush=True)
    for ctype, step in sorted(steps_to_diagnose.items(), key=lambda x: x[1]):
        print(f"\n{'#'*70}")
        print(f"# DIAGNOSING: {ctype} at step {step} (v={version})")
        print(f"{'#'*70}", flush=True)
        diagnose_step(step, headed=False, skip_to=step, run_agent=True)

    # Report missing types
    found = set(steps_to_diagnose.keys())
    missing = target_types - found
    if missing:
        print(f"\n=== TYPES NOT FOUND IN v={version}: {missing} ===", flush=True)
        print("These types only appear in other versions or were not detected.", flush=True)


if __name__ == "__main__":
    main()
