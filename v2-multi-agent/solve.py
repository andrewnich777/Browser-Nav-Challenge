"""
V4 Multi-Agent Browser Challenge Solver
Usage: python solve.py [--headed] [--max-steps N] [--benchmark --episodes N]
"""
import sys
import io
import time
import argparse

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

from playwright.sync_api import sync_playwright
from orchestrator import Orchestrator
from config import get_challenge_type, FINAL_STEP
from verify import is_finish_page, extract_step_from_url, extract_version_from_url


# ── Visual helpers ──────────────────────────────────────────────────────────

_CHALLENGE_LABELS = {
    'simple': 'Auto-Detect',
    'click_reveal': 'Click Reveal',
    'scroll': 'Scroll',
    'hidden_dom': 'Hidden DOM',
    'delayed_reveal': 'Timed Reveal',
    'delay_memory': 'Memory Flash',
    'hover': 'Hover',
    'decode': 'Decode',
    'timing': 'Timing Capture',
    'split_parts': 'Split Parts',
    'audio': 'Audio',
    'video': 'Video',
    'gesture': 'Gesture Draw',
    'drag_drop': 'Drag & Drop',
    'keyboard_sequence': 'Keyboard',
    'puzzle_solve': 'Math Puzzle',
    'calculated': 'Calculated',
    'multi_tab': 'Multi-Tab',
    'sequence': 'Sequence',
    'conditional_reveal': 'Conditional',
    'shadow_dom': 'Shadow DOM',
    'websocket': 'WebSocket',
    'service_worker': 'Service Worker',
    'mutation': 'Mutation',
    'recursive_iframe': 'Recursive IFrame',
}


def _bar(solved, total=30, width=30):
    """Compact progress bar: [=======>           ] 12/30"""
    filled = int(width * solved / total)
    bar = '=' * max(0, filled - 1) + ('>' if filled > 0 else ' ') + ' ' * (width - filled)
    return f"[{bar}] {solved}/{total}"


def _fmt_time(seconds):
    """Format seconds as M:SS or S.Ss."""
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{int(m)}:{s:04.1f}"
    return f"{seconds:.1f}s"


def _print_header(version):
    print(flush=True)
    print("=" * 64, flush=True)
    print("  Browser Navigation Challenge — Multi-Agent Solver v4", flush=True)
    print(f"  Site version: v{version}  |  Target: 30 steps", flush=True)
    print("=" * 64, flush=True)
    print(flush=True)


def _print_step_result(step, ctype, step_time, total_elapsed, solved, success):
    """Print a single-line step result."""
    label = _CHALLENGE_LABELS.get(ctype, ctype)
    status = "OK" if success else "FAIL"
    bar = _bar(solved)
    time_str = f"{step_time:.1f}s"
    total_str = _fmt_time(total_elapsed)

    if success:
        print(f"  Step {step:2d}  {label:<18s}  {time_str:>6s}  {bar}  ({total_str})",
              flush=True)
    else:
        print(f"  Step {step:2d}  {label:<18s}  {status:>6s}  {bar}  ({total_str})",
              flush=True)


def _print_final_report(solved, max_steps, total_time, step_log, metrics):
    """Print a polished final report."""
    print(flush=True)
    print("=" * 64, flush=True)

    if solved >= max_steps:
        print("  CHALLENGE COMPLETE — All 30 steps solved!", flush=True)
    else:
        print(f"  Run finished — {solved}/{max_steps} steps solved", flush=True)

    print("=" * 64, flush=True)
    print(flush=True)

    # Summary stats
    v4 = metrics.get('v4_hits', 0)
    vision = metrics.get('system2_successes', 0)
    api_calls = metrics.get('vision_calls', 0)
    input_tokens = metrics.get('total_input_tokens', 0)
    output_tokens = metrics.get('total_output_tokens', 0)

    # Cost calculation: Sonnet 4.5 pricing ($3/M input, $15/M output)
    cost = (input_tokens / 1_000_000) * 3.0 + (output_tokens / 1_000_000) * 15.0

    print(f"  Total time:       {_fmt_time(total_time)}", flush=True)
    print(f"  Steps solved:     {solved}/{max_steps}", flush=True)
    print(f"  Median step:      {metrics.get('median_step_time', 0):.1f}s", flush=True)
    print(flush=True)

    # Solver breakdown
    print(f"  Deterministic:    {v4} steps  (zero API cost)", flush=True)
    if vision > 0:
        print(f"  Vision fallback:  {vision} steps  ({api_calls} API calls)", flush=True)
    else:
        print(f"  Vision fallback:  0 steps  (not needed)", flush=True)
    print(flush=True)

    # Token usage & cost
    print(f"  Token usage:      {input_tokens:,} input / {output_tokens:,} output", flush=True)
    print(f"  API calls:        {api_calls}", flush=True)
    print(f"  Estimated cost:   ${cost:.2f}", flush=True)
    print(flush=True)

    # Per-step timing table
    if step_log:
        print("  Step  Type                Time   Method", flush=True)
        print("  " + chr(0x2500) * 4 + "  " + chr(0x2500) * 18 + "  " +
              chr(0x2500) * 5 + "  " + chr(0x2500) * 6, flush=True)

        fastest = min(s['time'] for s in step_log)
        slowest = max(s['time'] for s in step_log)

        for entry in step_log:
            label = _CHALLENGE_LABELS.get(entry['type'], entry['type'])
            t = entry['time']
            solver = "V4" if entry.get('v4') else "AI"
            status = "" if entry.get('success', True) else " FAIL"
            print(f"    {entry['step']:2d}   {label:<18s}  {t:5.1f}s  {solver}{status}",
                  flush=True)

        print("  " + chr(0x2500) * 4 + "  " + chr(0x2500) * 18 + "  " +
              chr(0x2500) * 5 + "  " + chr(0x2500) * 6, flush=True)
        print(f"    Fastest: {fastest:.1f}s  |  Slowest: {slowest:.1f}s  |  "
              f"Total: {_fmt_time(total_time)}", flush=True)

    print(flush=True)

    # Failure report
    failure_report = metrics.get('_failure_report', '')
    if failure_report:
        print(failure_report, flush=True)


# ── Main run logic ──────────────────────────────────────────────────────────

def run_single(args):
    """Run a single episode."""
    orch = Orchestrator()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page, version = orch.start_fresh(browser)

        _print_header(version)

        step = 1
        max_steps = min(args.max_steps, FINAL_STEP)
        solved = 0
        start_time = time.time()
        step_log = []  # Per-step records for final report

        while step <= max_steps:
            step_start = time.time()

            url_step = extract_step_from_url(page.url)
            url_ver = extract_version_from_url(page.url)
            if url_ver:
                version = url_ver

            if url_step is None:
                if is_finish_page(page):
                    solved += (31 - step)
                    break
                print(f"\n  Unexpected URL: {page.url}", flush=True)
                break

            if url_step != step:
                if url_step > step:
                    solved += (url_step - step)
                    step = url_step
                    continue
                else:
                    target_path = f'/step{step}?version={version}'
                    try:
                        page.evaluate(f'''() => {{
                            window.history.pushState({{}}, '', '{target_path}');
                            window.dispatchEvent(new PopStateEvent('popstate'));
                        }}''')
                        page.wait_for_timeout(1000)
                        url_step = extract_step_from_url(page.url)
                        if url_step == step:
                            continue
                    except Exception:
                        pass
                    break

            ctype = get_challenge_type(step, version)
            advanced, agents_used = orch.run_step(page, step, version)
            step_time = time.time() - step_start
            total_elapsed = time.time() - start_time

            is_v4 = 'v4_agent' in agents_used

            if advanced:
                solved += 1
                _print_step_result(step, ctype, step_time, total_elapsed, solved, True)
                step_log.append({
                    'step': step, 'type': ctype, 'time': step_time,
                    'v4': is_v4, 'success': True,
                })

                if step == FINAL_STEP or step == max_steps:
                    break

                step += 1
                page.wait_for_timeout(300)

            else:
                if step == FINAL_STEP and is_finish_page(page):
                    solved += 1
                    _print_step_result(step, ctype, step_time, total_elapsed, solved, True)
                    step_log.append({
                        'step': step, 'type': ctype, 'time': step_time,
                        'v4': False, 'success': True,
                    })
                    break
                _print_step_result(step, ctype, step_time, total_elapsed, solved, False)
                step_log.append({
                    'step': step, 'type': ctype, 'time': step_time,
                    'v4': is_v4, 'success': False,
                })
                step += 1
                continue

        total_time = time.time() - start_time

        # Collect metrics
        m = orch.get_full_metrics()
        failure_report = orch.get_failure_report()
        if failure_report:
            m['_failure_report'] = failure_report

        _print_final_report(solved, max_steps, total_time, step_log, m)

        browser.close()


def run_benchmark(args):
    """Run benchmark harness."""
    from benchmark import run_benchmark as bench, print_report, save_history
    result = bench(
        n_episodes=args.episodes,
        max_steps=args.max_steps,
        headed=args.headed,
    )
    print_report(result)
    save_history(result)
    print(f"\nHistory saved to knowledge/benchmark_history.jsonl", flush=True)


def main():
    parser = argparse.ArgumentParser(description="V4 Multi-Agent Browser Solver")
    parser.add_argument("--headed", action="store_true", help="Show browser")
    parser.add_argument("--max-steps", type=int, default=FINAL_STEP, help=f"Maximum steps to attempt (default: {FINAL_STEP})")
    parser.add_argument("--benchmark", action="store_true", help="Run benchmark harness instead of single episode")
    parser.add_argument("--episodes", type=int, default=3, help="Number of benchmark episodes (default: 3)")
    parser.add_argument("--quiet", action="store_true", help="Suppress internal debug logs (clean output)")
    args = parser.parse_args()

    if args.quiet:
        import config
        config.DEBUG = False

    if args.benchmark:
        run_benchmark(args)
    else:
        run_single(args)


if __name__ == "__main__":
    main()
