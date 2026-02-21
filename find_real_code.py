import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})

    # Intercept validation by patching string comparison
    page.add_init_script(r'''
        // Intercept all string comparisons to find the expected code
        window.__interceptedCodes = [];
        const origIncludes = String.prototype.includes;
        String.prototype.includes = function(search) {
            if (typeof search === 'string' && /^[A-Z0-9]{6}$/.test(search) && /^[A-Z0-9]{6}$/.test(this.toString())) {
                window.__interceptedCodes.push({checked: this.toString(), against: search});
            }
            return origIncludes.call(this, search);
        };
        const origEquals = String.prototype.localeCompare;
        // Also intercept === by wrapping setState-like patterns
    ''')

    page.goto('https://serene-frangipane-7fd25b.netlify.app')
    page.wait_for_load_state('networkidle')
    page.click('button:has-text("START")')
    page.wait_for_timeout(2000)

    print('URL:', page.url)

    # Try submitting a test code to trigger validation
    inp = page.query_selector('input[type="text"]')
    if inp:
        inp.fill('AAAAAA')

    # Click submit
    page.evaluate(r'''() => {
        document.querySelectorAll('button').forEach(btn => {
            if (btn.className.includes('green') && btn.textContent.includes('Submit')) {
                btn.click();
            }
        });
    }''')
    page.wait_for_timeout(1000)

    # Check intercepted codes
    intercepted = page.evaluate('window.__interceptedCodes || []')
    print('Intercepted codes:', intercepted[:10])

    # Also try: look for the validation function in the JS source
    # Get the JS bundle URL
    scripts = page.evaluate(r'''() => {
        return Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
    }''')
    print('Scripts:', scripts)

    browser.close()
