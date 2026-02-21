import sys, io, re, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from playwright.sync_api import sync_playwright

def dismiss_popups_green(page):
    """Click all visible green buttons in popup overlays to dismiss them."""
    page.evaluate(r'''() => {
        // Find fixed overlays with z >= 9995 and z < 10000
        document.querySelectorAll('*').forEach(el => {
            const style = window.getComputedStyle(el);
            if (style.position === 'fixed') {
                const z = parseInt(style.zIndex) || 0;
                if (z >= 9995 && z <= 9999) {
                    // Find green buttons inside and click them
                    const btns = el.querySelectorAll('button');
                    btns.forEach(btn => {
                        if (btn.className.includes('green')) {
                            btn.click();
                        }
                    });
                }
            }
        });
        // Also handle inset-0 backdrop overlays - find green buttons
        document.querySelectorAll('.fixed.inset-0').forEach(el => {
            const btns = el.querySelectorAll('button');
            btns.forEach(btn => {
                if (btn.className.includes('green')) {
                    btn.click();
                }
            });
        });
    }''')

def solve_step1(page):
    """Scroll to reveal code, enter it, submit."""
    dismiss_popups_green(page)
    page.wait_for_timeout(300)
    dismiss_popups_green(page)

    page.evaluate('window.scrollTo(0, 700)')
    page.wait_for_timeout(600)

    dismiss_popups_green(page)
    page.wait_for_timeout(200)

    # Find code in orange box
    hint_el = page.query_selector('.bg-orange-100')
    if hint_el:
        hint_text = hint_el.inner_text()
        codes = re.findall(r'\b([A-Z0-9]{6})\b', hint_text)
        skip = {'Scroll', 'Reveal', 'Submit', 'Waited', 'Scrolle'}
        for c in codes:
            if c not in skip:
                print(f'  Code from hint: {c}')
                inp = page.query_selector('input[type="text"]')
                if inp:
                    inp.fill(c)
                break

    # Click green Submit button in main content (not overlay)
    page.evaluate(r'''() => {
        const mainContent = document.querySelector('.max-w-6xl');
        if (mainContent) {
            const btns = mainContent.querySelectorAll('button');
            for (const btn of btns) {
                if (btn.className.includes('green') && btn.textContent.includes('Submit')) {
                    btn.click();
                    return;
                }
            }
        }
        // Fallback: find green submit in whole page but low z-index
        const allBtns = document.querySelectorAll('button');
        for (const btn of allBtns) {
            if (btn.className.includes('green') && btn.textContent.includes('Submit')) {
                const z = parseInt(window.getComputedStyle(btn.closest('[style*="z-index"], [class*="z-"]') || btn).zIndex) || 0;
                if (z < 9000) {
                    btn.click();
                    return;
                }
            }
        }
    }''')

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.goto('https://serene-frangipane-7fd25b.netlify.app')
    page.wait_for_load_state('networkidle')
    page.click('button:has-text("START")')
    page.wait_for_timeout(2000)

    print('Step 1 URL:', page.url)

    # Solve step 1
    solve_step1(page)
    page.wait_for_timeout(2000)
    print('After step 1:', page.url)

    if 'step2' in page.url:
        print('\n=== STEP 2 ===')
        # Don't dismiss anything - just observe
        page.wait_for_timeout(500)

        # Get ALL fixed elements
        fixed = page.evaluate(r'''() => {
            const results = [];
            document.querySelectorAll('*').forEach(el => {
                const s = window.getComputedStyle(el);
                if (s.position === 'fixed' && el.offsetHeight > 0) {
                    results.push({
                        z: s.zIndex,
                        text: el.textContent.substring(0, 150),
                        tag: el.tagName,
                        childBtns: el.querySelectorAll('button').length,
                        classes: (el.className || '').toString().substring(0, 100)
                    });
                }
            });
            return results;
        }''')

        print('\nFixed elements:')
        for f in fixed:
            print(f'  z={f["z"]} btns={f["childBtns"]} {f["text"][:100]}')

        # Dismiss green popups
        dismiss_popups_green(page)
        page.wait_for_timeout(500)
        dismiss_popups_green(page)
        page.wait_for_timeout(500)

        # Now examine content
        body = page.inner_text('body')
        print(f'\nBody text:\n{body[:2000]}')

        # Check inputs, buttons
        inputs = page.query_selector_all('input')
        print(f'\nInputs: {len(inputs)}')
        for inp in inputs:
            print(f'  type={inp.get_attribute("type")} placeholder={inp.get_attribute("placeholder")}')

        buttons = page.query_selector_all('button')
        print(f'\nButtons: {len(buttons)}')
        for btn in buttons[:20]:
            cls = btn.get_attribute('class') or ''
            is_green = 'green' in cls
            print(f'  {"[GREEN] " if is_green else ""}{btn.inner_text().strip()[:50]}')

    browser.close()
