"""Click-reveal challenge: find and click a reveal/show/discover button."""

from agents.v4.context import StepCtx
from agents.v4.helpers import click_button_by_text, wait_for_code_mutation


def solve(ctx: StepCtx) -> str | None:
    keywords = ['reveal', 'show', 'discover', 'uncover', 'unlock', 'display',
                'click me', 'click here', 'open']
    boundary_y = ctx.boundary_y or 99999
    if click_button_by_text(ctx.page, keywords, boundary_y):
        ctx.page.wait_for_timeout(400)
        return wait_for_code_mutation(ctx.page, 2000)
    return None
