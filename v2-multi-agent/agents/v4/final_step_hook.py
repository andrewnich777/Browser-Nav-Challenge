"""Final step hook — wraps any challenge agent for the last step.

Step 30 (FINAL_STEP) is a demo challenge: it looks like a regular challenge
(shadow_dom, websocket, etc.) but **no code exists**. The agent solves the
challenge normally through standard UI, clicks the final button — and when
that button doesn't produce a code, this hook pushes to /finish.

This is an add-on, not a separate agent type. The regular challenge agent
does all the work; this hook just steers the outcome when no code appears.

See MISSION.md Labeled Exceptions for rationale.
"""

from agents.v4.context import StepCtx
from verify import is_finish_page
from log import log


def wrap_final_step(agent_fn, ctx: StepCtx) -> str | None:
    """Run a challenge agent, then push to /finish if no code found.

    Args:
        agent_fn: The regular challenge agent's solve() function.
        ctx: Step context (page, step, boundary_y, etc.).

    Returns:
        "__FINISH__" sentinel if /finish reached, the agent's code if it
        somehow found one, or None on failure.
    """
    page = ctx.page

    # Step 1: Let the regular agent solve the challenge normally.
    # It will click through levels, connect websockets, etc. — all standard UI.
    code = None
    try:
        code = agent_fn(ctx)
    except Exception as e:
        log(f"step {ctx.step}: [final_step_hook] agent error (continuing to /finish): {e}")

    # If the agent somehow found a code, return it (unexpected but handle it)
    if code:
        log(f"step {ctx.step}: [final_step_hook] agent returned code {code}")
        return code

    # Step 2: Agent didn't find a code — expected for step 30.
    # Push to /finish via React Router-compatible navigation.
    log(f"step {ctx.step}: [final_step_hook] no code (expected), pushing to /finish")
    try:
        page.evaluate('''() => {
            window.history.pushState({}, '', '/finish');
            window.dispatchEvent(new PopStateEvent('popstate'));
        }''')
        page.wait_for_timeout(1000)

        if is_finish_page(page):
            log(f"step {ctx.step}: [final_step_hook] reached /finish")
            return "__FINISH__"
        else:
            log(f"step {ctx.step}: [final_step_hook] /finish navigation didn't land")
    except Exception as e:
        log(f"step {ctx.step}: [final_step_hook] /finish error: {e}")

    return None
