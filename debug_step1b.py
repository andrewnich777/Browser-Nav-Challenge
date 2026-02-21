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

    # DON'T dismiss anything yet - just read the page first
    body = page.inner_text('body')
    print('\n--- RAW BODY (first 2000 chars) ---')
    print(body[:2000])

    # Get orange box BEFORE dismissing
    hint_el = page.query_selector('.bg-orange-100')
    if hint_el:
        print('\n--- ORANGE BOX ---')
        print(hint_el.inner_text())

    # Now list ALL fixed overlays
    fixed = page.evaluate(r'''() => {
        const results = [];
        document.querySelectorAll('*').forEach(el => {
            const s = window.getComputedStyle(el);
            if (s.position === 'fixed' && el.offsetHeight > 0) {
                // Find green buttons in this element
                const greenBtns = [];
                el.querySelectorAll('button').forEach(btn => {
                    if (btn.className.includes('green')) {
                        greenBtns.push(btn.textContent.trim().substring(0, 40));
                    }
                });
                results.push({
                    z: s.zIndex,
                    text: el.textContent.substring(0, 120),
                    greenBtns: greenBtns,
                    hasInset: el.classList.contains('inset-0')
                });
            }
        });
        return results;
    }''')

    print('\n--- FIXED OVERLAYS ---')
    for f in fixed:
        print(f'z={f["z"]:>6} inset={f["hasInset"]} green={f["greenBtns"]} | {f["text"][:80]}')

    # Now dismiss popups one by one
    print('\n--- DISMISSING POPUPS ---')
    for i in range(5):
        dismissed = page.evaluate(r'''() => {
            let dismissed = 0;
            document.querySelectorAll('*').forEach(el => {
                const s = window.getComputedStyle(el);
                if (s.position === 'fixed') {
                    const z = parseInt(s.zIndex) || 0;
                    // Only dismiss high-z overlays (not the main content)
                    if (z >= 9995 && z <= 9999) {
                        const btns = el.querySelectorAll('button');
                        for (const btn of btns) {
                            if (btn.className.includes('green')) {
                                btn.click();
                                dismissed++;
                                break;
                            }
                        }
                    }
                }
            });
            return dismissed;
        }''')
        if dismissed:
            print(f'  Round {i+1}: dismissed {dismissed} popup(s)')
            page.wait_for_timeout(500)
        else:
            break

    # Re-read page after dismissing
    body2 = page.inner_text('body')
    print('\n--- BODY AFTER DISMISS ---')
    print(body2[:2000])

    # Check orange box
    hint_el = page.query_selector('.bg-orange-100')
    if hint_el:
        print('\n--- ORANGE BOX AFTER DISMISS ---')
        print(hint_el.inner_text())

    # List all buttons
    buttons = page.query_selector_all('button')
    print(f'\n--- ALL BUTTONS ({len(buttons)}) ---')
    for btn in buttons:
        cls = btn.get_attribute('class') or ''
        is_green = 'green' in cls
        vis = btn.is_visible()
        txt = btn.inner_text().strip()[:50]
        if vis:
            print(f'  {"[GREEN] " if is_green else "        "}{txt}')

    browser.close()
