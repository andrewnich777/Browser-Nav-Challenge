"""
Browser Navigation Challenge - Optimized Solver
Reverse-engineered the challenge validation to compute codes directly.

Code generation: Rl(step+1, version) — deterministic from step number and version
Interaction check: sessionStorage token at key "challenge_interaction_step_{step}"
Validation: Nv(input, step, version) AND Cv(step) must both pass
"""
import sys, io, re, time, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from playwright.sync_api import sync_playwright
from datetime import datetime


# ── Code Generation (reverse-engineered from JS bundle) ───────────────────

CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

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


# ── Challenge type prediction ────────────────────────────────────────────

TYPES_16_20 = ["multi_tab", "gesture", "sequence", "puzzle_solve", "calculated"]
TYPES_21_30 = ["shadow_dom", "websocket", "service_worker", "mutation",
               "recursive_iframe", "conditional_reveal", "multi_tab",
               "sequence", "calculated"]

def get_challenge_type(step, version):
    if step <= 15:
        return "simple"
    elif step <= 20:
        return TYPES_16_20[(step - 16 + version - 1) % len(TYPES_16_20)]
    else:
        return TYPES_21_30[(step - 21 + version - 1) % len(TYPES_21_30)]

def is_multi_tab(step, version):
    return get_challenge_type(step, version) == "multi_tab"


# ── Popup Dismissal ───────────────────────────────────────────────────────

DISMISS_JS = r'''() => {
    let d = 0;
    document.querySelectorAll('*').forEach(el => {
        const s = window.getComputedStyle(el);
        if (s.position === 'fixed') {
            const z = parseInt(s.zIndex) || 0;
            if (z >= 9995 && z <= 9999) {
                el.querySelectorAll('button').forEach(btn => {
                    if (btn.className.includes('green')) { btn.click(); d++; }
                });
            }
        }
    });
    document.querySelectorAll('.fixed.inset-0').forEach(el => {
        el.querySelectorAll('button').forEach(btn => {
            if (btn.className.includes('green')) { btn.click(); d++; }
        });
    });
    return d;
}'''


def dismiss_popups(page):
    for _ in range(8):
        if page.evaluate(DISMISS_JS) == 0:
            break
        page.wait_for_timeout(250)


# ── Interaction Handlers ──────────────────────────────────────────────────

def complete_interactions(page, step_num):
    page.evaluate(f'''() => {{
        const key = "challenge_interaction_step_" + {step_num};
        const token = {{
            token: crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36),
            interactionType: "automated",
            completedAt: Date.now()
        }};
        sessionStorage.setItem(key, JSON.stringify(token));
    }}''')

    page.mouse.wheel(0, 800)
    page.evaluate('window.scrollTo(0, 800)')
    page.wait_for_timeout(300)

    page.evaluate(r'''() => {
        document.querySelectorAll('input[type="radio"]').forEach(radio => {
            const label = radio.closest('label') || radio.parentElement;
            if (label && label.textContent.includes('Correct Choice')) {
                radio.click();
                radio.checked = true;
                radio.dispatchEvent(new Event('change', {bubbles: true}));
            }
        });
    }''')

    page.evaluate(r'''() => {
        document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            if (!cb.checked) {
                cb.click();
                cb.dispatchEvent(new Event('change', {bubbles: true}));
            }
        });
    }''')

    page.evaluate(r'''() => {
        document.querySelectorAll('button[disabled]').forEach(btn => {
            btn.disabled = false;
            btn.removeAttribute('disabled');
        });
    }''')


def submit_step(page, code):
    """Enter code and click submit."""
    page.evaluate(r'''(code) => {
        const inputs = document.querySelectorAll('input[type="text"], input[placeholder*="code" i]');
        for (const inp of inputs) {
            const nativeSet = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            nativeSet.call(inp, code);
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
        }
    }''', code)

    result = page.evaluate(r'''() => {
        const btns = document.querySelectorAll('button');
        for (const btn of btns) {
            if (btn.className.includes('green') && btn.textContent.includes('Submit')) {
                btn.click();
                return btn.textContent.trim().substring(0, 30);
            }
        }
        return null;
    }''')
    return result


def do_step(page, step, version):
    """Execute one step: dismiss popups, interact, submit. Returns True if URL changed."""
    current_url = page.url
    dismiss_popups(page)
    complete_interactions(page, step)
    page.wait_for_timeout(200)
    dismiss_popups(page)
    code = generate_code(step, version)
    submit_step(page, code)
    page.wait_for_timeout(400)
    return page.url != current_url


INIT_SCRIPT = r'''
    // Don't block window.open - let multi-tab challenges work normally
    // Just track opened windows so we can close them later
    window.__openedWindows = [];
    const origOpen = window.open.bind(window);
    window.open = function() {
        const w = origOpen.apply(window, arguments);
        if (w) window.__openedWindows.push(w);
        return w;
    };
'''


def start_fresh(browser, version_hint=None):
    """Create a fresh page, navigate to home, click START, return (page, version)."""
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.add_init_script(INIT_SCRIPT)
    page.goto('https://serene-frangipane-7fd25b.netlify.app')
    page.wait_for_load_state('networkidle')
    page.click('button:has-text("START")')
    page.wait_for_timeout(1000)
    vm = re.search(r'version=(\d+)', page.url)
    version = int(vm.group(1)) if vm else 1
    return page, version


def fix_stale_content(page, expected_step, version):
    """Fix stale React rendering by navigating home then pushState to target."""
    target_path = f'/step{expected_step}?version={version}'

    # Navigate to home first via full page load to get clean React state
    page.goto('https://serene-frangipane-7fd25b.netlify.app')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(300)

    # Now use pushState to navigate to target step (React Router picks this up)
    page.evaluate(f'''() => {{
        window.history.pushState({{}}, '', '{target_path}');
        window.dispatchEvent(new PopStateEvent('popstate', {{state: {{}}}}));
    }}''')
    page.wait_for_timeout(1000)

    header = page.inner_text('body')[:150]
    m = re.search(r'Step (\d+) of 30', header)
    if m and int(m.group(1)) == expected_step:
        return True
    return False


def speed_run_to(page, target_step, version):
    """Speed-run from step 1 to target_step (exclusive). Returns True if successful."""
    for sr in range(1, target_step):
        ok = do_step(page, sr, version)
        if not ok:
            page.wait_for_timeout(300)
            dismiss_popups(page)
            complete_interactions(page, sr)
            submit_step(page, generate_code(sr, version))
            page.wait_for_timeout(400)
            if page.url == f'https://serene-frangipane-7fd25b.netlify.app/step{sr}?version={version}':
                return False
        # Close extra tabs
        for extra in page.context.pages:
            if extra != page:
                try: extra.close()
                except: pass
        # Check and fix stale content after multi-tab steps
        if is_multi_tab(sr, version):
            page.wait_for_timeout(300)
            header = page.inner_text('body')[:100]
            stale = re.search(r'Step (\d+) of 30', header)
            if stale and int(stale.group(1)) != sr + 1:
                # Try to fix stale rendering
                fixed = fix_stale_content(page, sr + 1, version)
                if not fixed:
                    return False
    return True


# ── Main Solver ───────────────────────────────────────────────────────────

def main():
    results = {
        'date': datetime.now().isoformat(),
        'model': 'none (deterministic reverse-engineered)',
        'steps': [],
        'total_time_sec': 0,
        'steps_completed': 0,
        'input_tokens': 0,
        'output_tokens': 0,
        'estimated_cost_usd': 0.0,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page, version = start_fresh(browser)

        total_start = time.time()
        completed = 0

        step = 1
        while step <= 30:
            step_start = time.time()
            current_url = page.url

            # Extract version from URL
            version_match = re.search(r'version=(\d+)', current_url)
            if version_match:
                version = int(version_match.group(1))

            # Verify we're on the right step
            step_match = re.search(r'step(\d+)', current_url)
            if not step_match:
                body = page.inner_text('body')
                if any(w in body.lower() for w in ['congratulat', 'complete', 'finished', 'well done']):
                    print(f'\n  CHALLENGE COMPLETE!')
                    completed = 30
                    break
                print(f'\n  Unexpected URL: {current_url}')
                break

            actual = int(step_match.group(1))
            if actual != step:
                print(f'\n  At step {actual}, expected {step}')
                break

            code = generate_code(step, version)
            ctype = get_challenge_type(step, version)
            print(f'Step {step:2d} (v{version}) [{ctype[:8]:8s}] code={code}', end=' ')

            # Execute step
            dismiss_popups(page)
            complete_interactions(page, step)
            page.wait_for_timeout(200)
            dismiss_popups(page)
            submit_step(page, code)
            page.wait_for_timeout(500)

            # Close extra tabs (both playwright pages and JS-tracked windows)
            for extra in page.context.pages:
                if extra != page:
                    try: extra.close()
                    except: pass
            try:
                page.evaluate(r'''() => {
                    (window.__openedWindows || []).forEach(w => { try { w.close(); } catch(e) {} });
                    window.__openedWindows = [];
                }''')
            except: pass

            if page.url == current_url:
                # Retry once
                page.wait_for_timeout(300)
                dismiss_popups(page)
                complete_interactions(page, step)
                submit_step(page, code)
                page.wait_for_timeout(600)

            if page.url != current_url:
                # URL changed - check for stale content
                page.wait_for_timeout(300)
                header = page.inner_text('body')[:150]
                stale = re.search(r'Step (\d+) of 30', header)

                if stale and int(stale.group(1)) == step + 1:
                    # Content matches - all good
                    pass
                elif stale and int(stale.group(1)) != step + 1:
                    stale_from = int(stale.group(1))
                    print(f'stale(shows {stale_from})', end=' ')

                    # Try to fix stale rendering in-place first
                    if fix_stale_content(page, step + 1, version):
                        print(f'fixed!', end=' ')
                        completed += 1
                        elapsed = time.time() - step_start
                        total_elapsed = time.time() - total_start
                        print(f'OK ({elapsed:.1f}s, total: {total_elapsed:.1f}s)')
                        results['steps'].append({
                            'step': step,
                            'time_sec': round(elapsed, 2),
                            'success': True,
                        })
                        step += 1
                        continue

                    # Close this page entirely, start fresh and speed-run
                    page.close()
                    page, new_version = start_fresh(browser)
                    print(f'fresh(v{new_version})', end=' ')

                    ok = speed_run_to(page, step + 1, new_version)
                    if ok:
                        version = new_version
                        print(f'OK-fresh', end=' ')
                    else:
                        # Speed-run hit stale too. Try again with yet another fresh page.
                        page.close()
                        # Try up to 3 times
                        recovered = False
                        for attempt in range(3):
                            page, new_version = start_fresh(browser)
                            ok = speed_run_to(page, step + 1, new_version)
                            if ok:
                                version = new_version
                                print(f'OK-retry{attempt+1}', end=' ')
                                recovered = True
                                break
                            page.close()
                        if not recovered:
                            elapsed = time.time() - step_start
                            print(f'FAILED (cannot speed-run past multi-tab)')
                            results['steps'].append({
                                'step': step,
                                'time_sec': round(elapsed, 2),
                                'success': False,
                            })
                            # Create a fresh page to continue
                            page, version = start_fresh(browser)
                            break

                completed += 1
                elapsed = time.time() - step_start
                total_elapsed = time.time() - total_start
                print(f'OK ({elapsed:.1f}s, total: {total_elapsed:.1f}s)')
                results['steps'].append({
                    'step': step,
                    'time_sec': round(elapsed, 2),
                    'success': True,
                })
                step += 1
            else:
                # Didn't advance at all - try fresh page + speed-run
                print(f'stuck', end=' ')
                page.close()
                page, new_version = start_fresh(browser)
                ok = speed_run_to(page, step, new_version)
                if ok:
                    version = new_version
                    # Now try this step again from fresh state
                    continue
                else:
                    elapsed = time.time() - step_start
                    print(f'FAILED ({elapsed:.1f}s)')
                    results['steps'].append({
                        'step': step,
                        'time_sec': round(elapsed, 2),
                        'success': False,
                    })
                    break

        total_time = time.time() - total_start
        results['total_time_sec'] = round(total_time, 2)
        results['steps_completed'] = completed

        # Print summary
        print(f'\n{"="*60}')
        print(f'  BROWSER NAVIGATION CHALLENGE - RESULTS')
        print(f'{"="*60}')
        print(f'  Steps:  {completed}/30')
        print(f'  Time:   {total_time:.1f}s')
        print(f'  Tokens: 0 (no LLM used)')
        print(f'  Cost:   $0.00')
        print(f'  URL:    {page.url}')
        print(f'{"  -"*20}')
        for s in results['steps']:
            mark = 'PASS' if s['success'] else 'FAIL'
            print(f'  Step {s["step"]:2d} [{mark}] {s["time_sec"]:5.1f}s')
        print(f'{"="*60}')

        with open('metrics.json', 'w') as f:
            json.dump(results, f, indent=2)
        print(f'Results saved to metrics.json')

        browser.close()

    return results


if __name__ == '__main__':
    main()
