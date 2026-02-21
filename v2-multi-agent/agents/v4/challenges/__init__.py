"""Challenge agent registry — maps challenge type to solve function.

Step 30 (FINAL_STEP) no longer needs separate agents — the FINAL_STEP module
in helpers.py wraps any active agent with /finish navigation automatically.
"""

from agents.v4.challenges import (
    click_reveal,
    scroll,
    hidden_dom,
    timing,
    audio,
    hover,
    decode,
    delayed_reveal,
    delay_memory,
    gesture,
    keyboard_sequence,
    split_parts,
    puzzle_solve,
    calculated,
    video,
    sequence,
    mutation,
    multi_tab,
    drag_drop,
    shadow_dom,
    recursive_iframe,
    websocket,
    service_worker,
    conditional_reveal,
)

# Maps base challenge type → solve(ctx) function
CHALLENGE_AGENTS = {
    # Simple
    'click_reveal': click_reveal.solve,
    'scroll': scroll.solve,
    'hidden_dom': hidden_dom.solve,
    'timing': timing.solve,
    'audio': audio.solve,

    # Medium
    'hover': hover.solve,
    'decode': decode.solve,
    'delayed_reveal': delayed_reveal.solve,
    'delay_memory': delay_memory.solve,
    'gesture': gesture.solve,
    'keyboard_sequence': keyboard_sequence.solve,
    'split_parts': split_parts.solve,
    'puzzle_solve': puzzle_solve.solve,
    'calculated': calculated.solve,
    'video': video.solve,
    'sequence': sequence.solve,
    'sequence_challenge': sequence.solve,
    'mutation': mutation.solve,
    'multi_tab': multi_tab.solve,

    # Complex
    'drag_drop': drag_drop.solve,
    'shadow_dom': shadow_dom.solve,
    'recursive_iframe': recursive_iframe.solve,
    'websocket': websocket.solve,
    'service_worker': service_worker.solve,
    'conditional_reveal': conditional_reveal.solve,
}
