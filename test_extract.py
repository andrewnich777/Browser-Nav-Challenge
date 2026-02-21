import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from playwright.sync_api import sync_playwright

with open('extract_code.js', 'r') as f:
    js_code = f.read()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.goto('https://serene-frangipane-7fd25b.netlify.app')
    page.wait_for_load_state('networkidle')
    page.click('button:has-text("START")')
    page.wait_for_timeout(2000)

    print('Step 1 URL:', page.url)
    result = page.evaluate(js_code)
    print('Codes found in React state:')
    if isinstance(result, list):
        for r in result[:20]:
            print(f'  {r["path"]}: {r["value"]}')
    else:
        print(result)

    browser.close()
