"""Code entry agent — enters code into input and clicks Submit."""

from agents.base import Agent
from config import DEBUG
from log import log_stage


class CodeEntryAgent(Agent):
    name = "code_entry"

    def run(self, page, step: int, version: int, code: str = None) -> bool:
        """Enter the code and submit. Code must be provided."""
        if not code:
            log_stage("code_entry", "no code provided")
            return False

        if DEBUG:
            log_stage("code_entry", f"entering code={code}")

        # Wait for input to appear if not present
        for _ in range(5):
            if page.query_selector('input[placeholder*="code" i]'):
                break
            page.wait_for_timeout(200)

        # Fill using native setter + events
        page.evaluate(r'''(code) => {
            const inp = document.querySelector('input[placeholder*="code" i]');
            if (!inp) return;
            const nativeSet = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            nativeSet.call(inp, code);
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
        }''', code)

        # Small wait for React
        page.wait_for_timeout(100)

        # Click the form's submit button (or nearby submit button)
        result = page.evaluate(r'''() => {
            const inp = document.querySelector('input[placeholder*="code" i]');
            if (!inp) return {error: 'no input'};

            const form = inp.closest('form');
            let submitBtn = null;

            // Try to find submit button in form first
            if (form) {
                submitBtn = form.querySelector('button[type="submit"]');
                if (!submitBtn) {
                    const btns = form.querySelectorAll('button');
                    for (const btn of btns) {
                        if (btn.textContent.toLowerCase().includes('submit')) {
                            submitBtn = btn;
                            break;
                        }
                    }
                }
            }

            // If no form or no button in form, look for nearby submit button
            if (!submitBtn) {
                // Look in parent containers
                let container = inp.parentElement;
                for (let i = 0; i < 5 && container; i++) {
                    const btns = container.querySelectorAll('button');
                    for (const btn of btns) {
                        const text = btn.textContent.toLowerCase();
                        if (text.includes('submit') || text.includes('enter') || text.includes('go')) {
                            submitBtn = btn;
                            break;
                        }
                    }
                    if (submitBtn) break;
                    container = container.parentElement;
                }
            }

            // Last resort: any button with "submit" text on page
            if (!submitBtn) {
                const allBtns = document.querySelectorAll('button');
                for (const btn of allBtns) {
                    const text = btn.textContent.toLowerCase();
                    if (text.includes('submit code') || text === 'submit') {
                        submitBtn = btn;
                        break;
                    }
                }
            }

            if (!submitBtn) return {error: 'no submit button', hasForm: !!form};

            // Check if button is disabled
            const isDisabled = submitBtn.disabled ||
                submitBtn.classList.contains('disabled') ||
                submitBtn.getAttribute('aria-disabled') === 'true';

            // Click it
            submitBtn.click();

            // Also try form.submit() as backup
            if (form && isDisabled) {
                try { form.submit(); } catch(e) {}
            }

            return {
                clicked: true,
                inputValue: inp.value,
                btnDisabled: isDisabled,
                btnText: submitBtn.textContent.trim(),
                hasForm: !!form
            };
        }''')

        # If no submit button found, try pressing Enter on the input
        if isinstance(result, dict) and result.get('error') == 'no submit button':
            log_stage("code_entry", "no button, trying Enter key")
            inp = page.query_selector('input[placeholder*="code" i]')
            if inp:
                inp.press('Enter')
                result = {'clicked': True, 'method': 'enter_key'}

        # If button was disabled, forcibly enable it and try again
        if isinstance(result, dict) and result.get('btnDisabled'):
            log_stage("code_entry", "button disabled, forcing enable")
            page.evaluate(r'''() => {
                document.querySelectorAll('button[disabled], button.disabled').forEach(btn => {
                    btn.disabled = false;
                    btn.removeAttribute('disabled');
                    btn.classList.remove('disabled');
                    btn.removeAttribute('aria-disabled');
                });
            }''')
            page.wait_for_timeout(100)
            # Re-click the button
            page.evaluate(r'''() => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    if (btn.textContent.toLowerCase().includes('submit')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }''')
            result['forcedEnable'] = True

        if DEBUG:
            log_stage("code_entry", f"submit result: {result}")

        return result.get("clicked", False) if isinstance(result, dict) else False
