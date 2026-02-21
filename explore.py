import sys, io, re, time, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from playwright.sync_api import sync_playwright

def dismiss_overlays(page):
    """Remove popup overlays (z-9997 to z-9999) but keep content and header."""
    page.evaluate('''() => {
        document.querySelectorAll('*').forEach(el => {
            const style = window.getComputedStyle(el);
            if (style.position === 'fixed') {
                const z = parseInt(style.zIndex) || 0;
                const text = el.textContent || '';
                // Remove overlays (z 9995-9999) but keep step header (z 10000) and content
                if (z >= 9995 && z <= 9999) {
                    el.remove();
                }
                // Also remove full-screen backdrop overlays
                if (el.classList.contains('inset-0') ||
                    (el.className && el.className.includes && el.className.includes('inset-0'))) {
                    el.remove();
                }
            }
        });
    }''')

def get_step_details(page, step_num):
    info = {'step': step_num, 'url': page.url}
    hint_el = page.query_selector('.bg-orange-100')
    if hint_el:
        info['hint'] = hint_el.inner_text().strip()
    body_text = page.inner_text('body')
    info['body_preview'] = body_text[:2000]
    inputs = page.query_selector_all('input')
    info['inputs'] = []
    for inp in inputs:
        info['inputs'].append({
            'type': inp.get_attribute('type'),
            'placeholder': inp.get_attribute('placeholder'),
        })
    info['has_slider'] = len(page.query_selector_all('input[type=range]')) > 0
    info['has_checkbox'] = len(page.query_selector_all('input[type=checkbox]')) > 0
    info['has_select'] = len(page.query_selector_all('select')) > 0
    info['has_textarea'] = len(page.query_selector_all('textarea')) > 0
    info['has_canvas'] = len(page.query_selector_all('canvas')) > 0
    buttons = page.query_selector_all('button')
    info['button_count'] = len(buttons)
    green_btns = []
    for btn in buttons:
        classes = btn.get_attribute('class') or ''
        if 'green' in classes:
            green_btns.append(btn.inner_text().strip()[:50])
    info['green_buttons'] = green_btns
    return info

def find_code(page):
    """Find the 6-character code on the page."""
    html = page.content()
    hint_el = page.query_selector('.bg-orange-100')
    hint_text = hint_el.inner_text() if hint_el else ''

    skip = {'Scroll', 'Reveal', 'Submit', 'Waited', 'Appear', 'Delays', 'Hidden',
            'Cookie', 'Dismiss', 'Accept', 'Verify', 'Enable', 'Toggle', 'Slider',
            'Select', 'Prompt', 'Canvas', 'SCROLL', 'SUBMIT', 'HIDDEN', 'COOKIE',
            'Checke', 'Mathem', 'Puzzel', 'Challe', 'Number', 'Sectio', 'Filler'}

    # Method 1: 6-char code in orange box
    codes = re.findall(r'\b([A-Z0-9]{6})\b', hint_text)
    for c in codes:
        if c not in skip:
            return c

    # Method 2: data-code attrs
    dc = re.findall(r'data-code="([^"]+)"', html)
    if dc:
        return dc[0]

    # Method 3: HTML comments
    comments = re.findall(r'<!--\s*(?:code|Code|CODE)[:\s]*([A-Za-z0-9]{4,8})\s*-->', html)
    if comments:
        return comments[0]

    # Method 4: Any standalone 6-digit code in HTML tags
    all_codes = re.findall(r'>(\d{6})<', html)
    if all_codes:
        return all_codes[0]

    # Method 5: 6-char uppercase in spans
    span_codes = re.findall(r'<span[^>]*>([A-Z0-9]{6})</span>', html)
    for c in span_codes:
        if c not in skip:
            return c

    # Method 6: Body text patterns
    body = page.inner_text('body')
    refs = re.findall(r'(?:code is|code:|Your code|the code)\s*[:\s]*([A-Za-z0-9]{6})', body, re.IGNORECASE)
    if refs:
        return refs[0]

    return None

def try_solve_step(page, step_num):
    old_url = page.url

    # 1. Dismiss overlays
    dismiss_overlays(page)
    page.wait_for_timeout(200)

    # 2. Scroll
    page.evaluate('window.scrollTo(0, 700)')
    page.wait_for_timeout(500)

    # 3. Handle delayed reveals
    hint_el = page.query_selector('.bg-orange-100')
    hint = hint_el.inner_text() if hint_el else ''
    if any(w in hint.lower() for w in ['second', 'delay', 'appear in']):
        wait_match = re.search(r'(\d+)\s*second', hint)
        wait_time = int(wait_match.group(1)) if wait_match else 5
        print(f'  Waiting {wait_time}s...')
        page.wait_for_timeout((wait_time + 1) * 1000)

    # 4. Dismiss overlays again
    dismiss_overlays(page)
    page.wait_for_timeout(200)

    # 5. Find and enter code
    code = find_code(page)
    if code:
        inp = page.query_selector('input[placeholder*="code" i], input[type="text"]')
        if inp:
            inp.fill(code)
            print(f'  Code: {code}')
    else:
        print(f'  No code found!')

    # 6. Submit - use dispatch_event to bypass overlay interception
    buttons = page.query_selector_all('button')
    # Priority: green button with "Submit"
    for btn in buttons:
        classes = btn.get_attribute('class') or ''
        txt = btn.inner_text().strip()
        if 'green' in classes and 'Submit' in txt:
            btn.dispatch_event('click')
            page.wait_for_timeout(800)
            if page.url != old_url:
                return True
    # Fallback: any green button
    for btn in buttons:
        classes = btn.get_attribute('class') or ''
        if 'green' in classes:
            btn.dispatch_event('click')
            page.wait_for_timeout(800)
            if page.url != old_url:
                return True

    return page.url != old_url


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.goto('https://serene-frangipane-7fd25b.netlify.app')
    page.wait_for_load_state('networkidle')
    page.click('button:has-text("START")')
    page.wait_for_timeout(2000)

    all_steps = []
    start_total = time.time()

    for step in range(1, 31):
        current_url = page.url
        step_match = re.search(r'step(\d+)', current_url)
        if not step_match:
            print(f'\n*** No step in URL: {current_url} ***')
            break
        actual_step = int(step_match.group(1))
        if actual_step != step:
            print(f'\n*** Expected step{step} but at step{actual_step} ***')
            step = actual_step  # adjust

        print(f'\n{"="*50}')
        print(f'STEP {step}')
        dismiss_overlays(page)
        page.wait_for_timeout(200)
        info = get_step_details(page, step)
        all_steps.append(info)

        hint = info.get('hint', 'NO HINT')
        print(f'Hint: {hint[:200]}')
        print(f'Inputs: {len(info["inputs"])}, Btns: {info["button_count"]}, Green: {info["green_buttons"]}')
        for k in ['has_slider', 'has_checkbox', 'has_select', 'has_textarea', 'has_canvas']:
            if info[k]:
                print(f'  -> {k}')

        t0 = time.time()
        solved = try_solve_step(page, step)
        elapsed = time.time() - t0

        if solved:
            print(f'  SOLVED in {elapsed:.1f}s')
        else:
            print(f'  FAILED after {elapsed:.1f}s')
            print(f'  URL: {page.url}')
            body = page.inner_text('body')[:500]
            print(f'  Body: {body}')
            html = page.content()
            with open(f'step{step}_failed.html', 'w', encoding='utf-8') as f:
                f.write(html)
            break

        page.wait_for_timeout(300)

    total = time.time() - start_total
    print(f'\n{"="*50}')
    print(f'EXPLORATION COMPLETE')
    print(f'Total time: {total:.1f}s')
    print(f'Steps reached: {len(all_steps)}')
    print(f'Final URL: {page.url}')

    with open('step_catalog.json', 'w', encoding='utf-8') as f:
        json.dump(all_steps, f, indent=2, ensure_ascii=False)

    browser.close()
