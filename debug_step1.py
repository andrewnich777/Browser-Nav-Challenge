import sys, io, re, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.goto('https://serene-frangipane-7fd25b.netlify.app')
    page.wait_for_load_state('networkidle')
    page.click('button:has-text("START")')
    page.wait_for_timeout(2000)

    print('URL:', page.url)
    print()

    # Phase 1: Dismiss popups by clicking green buttons
    for _ in range(5):
        page.evaluate(r'''() => {
            document.querySelectorAll('*').forEach(el => {
                const s = window.getComputedStyle(el);
                if (s.position === 'fixed') {
                    const z = parseInt(s.zIndex) || 0;
                    if (z >= 9995 && z <= 9999) {
                        const btns = el.querySelectorAll('button');
                        btns.forEach(btn => {
                            if (btn.className.includes('green')) btn.click();
                        });
                    }
                }
            });
            // inset-0 backdrops
            document.querySelectorAll('.fixed.inset-0').forEach(el => {
                el.querySelectorAll('button').forEach(btn => {
                    if (btn.className.includes('green')) btn.click();
                });
            });
        }''')
        page.wait_for_timeout(500)

    # Phase 2: Scroll
    page.evaluate('window.scrollTo(0, 700)')
    page.wait_for_timeout(1000)

    # Phase 3: Read the orange box
    hint_el = page.query_selector('.bg-orange-100')
    if hint_el:
        hint_text = hint_el.inner_text()
        print('Orange box text:')
        print(hint_text)
        print()

    # Phase 4: Find all 6-char codes on page
    html = page.content()
    # Very broad search
    all_6char = re.findall(r'\b([A-Z0-9]{6})\b', page.inner_text('body'))
    print('All 6-char alphanumeric in body text:', all_6char[:20])

    all_6char_html = re.findall(r'>([A-Z0-9]{6})<', html)
    print('All 6-char in HTML tags:', all_6char_html[:20])

    # Also check for the code as any 6 identical digits
    digit_codes = re.findall(r'\b(\d{6})\b', page.inner_text('body'))
    print('All 6-digit numbers:', digit_codes[:20])

    # Phase 5: Try entering the first plausible code and submitting
    skip = {'Scroll', 'Reveal', 'Submit'}
    code = None
    for c in all_6char_html:
        if c not in skip:
            code = c
            break
    if not code:
        for c in digit_codes:
            code = c
            break

    print(f'\nUsing code: {code}')

    if code:
        inp = page.query_selector('input[type="text"]')
        if inp:
            inp.fill(code)
            print('Filled input')
        else:
            print('No text input found!')

    # Find the green submit button - list ALL green buttons
    all_btns = page.query_selector_all('button')
    green_btns = []
    for btn in all_btns:
        cls = btn.get_attribute('class') or ''
        if 'green' in cls:
            txt = btn.inner_text().strip()
            visible = btn.is_visible()
            green_btns.append((txt, visible, cls[:100]))
    print(f'\nGreen buttons ({len(green_btns)}):')
    for txt, vis, cls in green_btns:
        print(f'  vis={vis} "{txt[:40]}"')

    # Click the green Submit button
    for btn in all_btns:
        cls = btn.get_attribute('class') or ''
        txt = btn.inner_text().strip()
        if 'green' in cls and 'Submit' in txt:
            # Use JS click to bypass overlays
            page.evaluate(r'''(btn) => btn.click()''', btn)
            print(f'\nClicked: {txt}')
            break

    page.wait_for_timeout(2000)
    print(f'\nAfter submit URL: {page.url}')

    # Check for error messages
    errors = page.query_selector_all('[class*="red"], [class*="error"], [class*="danger"]')
    for e in errors[:5]:
        t = e.inner_text().strip()
        if t and len(t) < 200:
            print(f'Error element: {t}')

    browser.close()
