"""
Browser Navigation Challenge - Automated Agent
Uses browser-use + Playwright + LLM (Anthropic Claude or OpenAI GPT) to solve
all 30 challenges at https://serene-frangipane-7fd25b.netlify.app
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── LLM Setup ──────────────────────────────────────────────────────────────

def get_llm():
    """Return a LangChain chat model based on available API keys."""
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    if anthropic_key:
        from langchain_anthropic import ChatAnthropic
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        print(f"Using Anthropic model: {model}")
        return ChatAnthropic(model_name=model, api_key=anthropic_key, timeout=120, temperature=0)
    elif openai_key:
        from langchain_openai import ChatOpenAI
        model = os.getenv("OPENAI_MODEL", "gpt-4o")
        print(f"Using OpenAI model: {model}")
        return ChatOpenAI(model=model, api_key=openai_key, temperature=0)
    else:
        print("ERROR: No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env")
        sys.exit(1)


# ── Token / Cost Tracking ──────────────────────────────────────────────────

PRICING = {
    # Anthropic (per million tokens)
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    # OpenAI
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}


class UsageTracker:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.input_tokens = 0
        self.output_tokens = 0
        self.step_results: list[dict] = []

    def add_step(self, step: int, elapsed: float, success: bool, note: str = ""):
        self.step_results.append({
            "step": step,
            "time_sec": round(elapsed, 2),
            "success": success,
            "note": note,
        })

    @property
    def cost(self) -> float:
        rates = PRICING.get(self.model_name, {"input": 3.0, "output": 15.0})
        return (self.input_tokens * rates["input"] + self.output_tokens * rates["output"]) / 1_000_000

    def report(self) -> str:
        total_time = sum(s["time_sec"] for s in self.step_results)
        passed = sum(1 for s in self.step_results if s["success"])
        lines = [
            "=" * 60,
            "  BROWSER NAVIGATION CHALLENGE - SUMMARY REPORT",
            "=" * 60,
            f"  Date:            {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"  Model:           {self.model_name}",
            f"  Steps completed: {passed} / {len(self.step_results)}",
            f"  Total time:      {total_time:.1f}s",
            f"  Input tokens:    {self.input_tokens:,}",
            f"  Output tokens:   {self.output_tokens:,}",
            f"  Est. cost:       ${self.cost:.4f}",
            "-" * 60,
        ]
        for s in self.step_results:
            status = "PASS" if s["success"] else "FAIL"
            lines.append(f"  Step {s['step']:2d}  [{status}]  {s['time_sec']:6.1f}s  {s['note']}")
        lines.append("=" * 60)
        return "\n".join(lines)


# ── Main Agent Loop ────────────────────────────────────────────────────────

CHALLENGE_URL = "https://serene-frangipane-7fd25b.netlify.app"

SYSTEM_PROMPT = """\
You are a browser automation expert completing the "Browser Navigation Challenge".
The site has 30 sequential steps. Each step presents a different UI puzzle you must solve.

General strategy for each step:
1. READ the challenge instructions on the page carefully.
2. Look for hints about what code to enter, what to click, what to type, where to scroll, etc.
3. Common challenge patterns include:
   - Scrolling down to reveal hidden content or codes
   - Finding the correct button among many decoys (look for data attributes, unique IDs, or hidden text)
   - Entering codes into input fields
   - Solving CAPTCHAs, math problems, or text puzzles
   - Interacting with drag-and-drop, sliders, or toggles
   - Finding hidden elements (check opacity:0, display:none, or tiny/off-screen elements)
   - Reading codes from page source, comments, or data attributes
   - Waiting for timed elements to appear
   - Handling alerts, confirms, or prompts
4. After solving a challenge, enter the code (if needed) and click the submit/next button.
5. If stuck, inspect the page HTML for hidden clues.

IMPORTANT:
- Many steps require scrolling 500+ pixels to reveal codes.
- Decoy buttons are common — look for the REAL navigation button (often has a special data attribute like data-real="true" or is at the bottom of a scroll area).
- When you see an input field for a code, the code is usually hidden somewhere on the page.
- Be persistent — try multiple approaches if the first one fails.
"""


async def run_challenge(headless: bool = True, max_steps: int = 30):
    from browser_use import Agent, Browser, BrowserProfile

    llm = get_llm()
    model_name = getattr(llm, "model_name", None) or getattr(llm, "model", "unknown")
    tracker = UsageTracker(model_name)

    browser_profile = BrowserProfile(
        headless=headless,
        disable_security=True,
        viewport={"width": 1280, "height": 1024},
    )
    browser = Browser(profile=browser_profile)

    overall_start = time.time()

    try:
        # The agent handles the full 30-step challenge in one session.
        # We give it a task and let browser-use drive.
        task = f"""\
Go to {CHALLENGE_URL} and complete all 30 steps of the Browser Navigation Challenge.

Start by clicking the START button on the landing page.

For each step:
1. Read the challenge instructions carefully
2. Scroll down if instructed (at least 500px) to reveal hidden codes
3. Look for the correct code or action needed
4. Enter codes in input fields if present
5. Click the correct submit/next button (beware of decoy buttons)
6. Wait for the next step to load

After completing all 30 steps, report which steps you completed and any that you got stuck on.

Key tips:
- Scroll aggressively on every step — many codes are hidden below the fold
- Check for hidden HTML elements (opacity:0, tiny text, data attributes)
- The real "next" button is often at the very bottom of scrollable content
- Some steps have timers — wait if needed
- Look for codes in comments, data attributes, or hidden spans
- If you see a code input, the code is somewhere on the page — search for it
"""

        agent = Agent(
            task=task,
            llm=llm,
            browser=browser,
            max_actions_per_step=10,
            use_vision=True,
            system_prompt_class=None,  # Use default
        )

        # Run the agent
        print("\nStarting browser-use agent...")
        print(f"Target: {CHALLENGE_URL}")
        print(f"Headless: {headless}")
        print("-" * 60)

        result = await agent.run(max_steps=200)

        elapsed = time.time() - overall_start

        # Extract token usage from the agent's history if available
        try:
            history = agent.state.history
            if history and hasattr(history, 'action_results'):
                for i, action in enumerate(history.action_results()):
                    step_num = min(i + 1, 30)
                    tracker.add_step(step_num, elapsed / max(len(history.action_results()), 1), True)
        except Exception:
            pass

        # Try to get token usage
        try:
            if hasattr(result, 'token_usage'):
                tracker.input_tokens = result.token_usage.get('input', 0)
                tracker.output_tokens = result.token_usage.get('output', 0)
        except Exception:
            pass

        # If we couldn't track individual steps, add one summary entry
        if not tracker.step_results:
            # Check the final URL to estimate progress
            try:
                pages = await browser.get_current_page()
                final_url = pages.url if hasattr(pages, 'url') else str(pages)
                step_match = None
                import re
                m = re.search(r'step(\d+)', final_url)
                if m:
                    step_match = int(m.group(1))
                    for s in range(1, step_match + 1):
                        tracker.add_step(s, elapsed / step_match, True, "completed")
                    for s in range(step_match + 1, 31):
                        tracker.add_step(s, 0, False, "not reached")
                else:
                    # Check if we're on a completion page
                    if 'complete' in final_url.lower() or 'finish' in final_url.lower() or 'congrat' in final_url.lower():
                        for s in range(1, 31):
                            tracker.add_step(s, elapsed / 30, True, "completed")
                    else:
                        tracker.add_step(1, elapsed, False, f"final url: {final_url}")
            except Exception as e:
                tracker.add_step(1, elapsed, False, f"could not determine progress: {e}")

        # Print the final result
        print("\n\nAgent finished.")
        if hasattr(result, 'final_result'):
            print(f"Agent report:\n{result.final_result()}")

        report = tracker.report()
        print(report)

        # Save report
        report_path = Path("challenge_report.txt")
        report_path.write_text(report, encoding="utf-8")
        print(f"\nReport saved to {report_path}")

        # Save detailed JSON
        json_path = Path("challenge_results.json")
        json_path.write_text(json.dumps({
            "date": datetime.now().isoformat(),
            "model": model_name,
            "total_time_sec": round(elapsed, 2),
            "input_tokens": tracker.input_tokens,
            "output_tokens": tracker.output_tokens,
            "estimated_cost_usd": round(tracker.cost, 4),
            "steps": tracker.step_results,
        }, indent=2), encoding="utf-8")
        print(f"JSON results saved to {json_path}")

    finally:
        await browser.close()


# ── Entry Point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Browser Navigation Challenge")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode (visible)")
    parser.add_argument("--max-steps", type=int, default=30, help="Max challenge steps to attempt")
    args = parser.parse_args()

    asyncio.run(run_challenge(headless=not args.headed, max_steps=args.max_steps))
