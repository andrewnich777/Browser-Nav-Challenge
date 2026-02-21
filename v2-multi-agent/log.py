"""Debug logging — toggle via config.DEBUG. Remove this file when done."""

import time
from config import DEBUG

_t0 = time.time()

# Popup log batching: suppress individual dismiss lines, show summary
_popup_batch: list[str] = []
_popup_batch_active = False


def log(msg: str):
    if DEBUG:
        elapsed = time.time() - _t0
        print(f"  [{elapsed:6.1f}s] {msg}", flush=True)


def log_stage(agent_name: str, detail: str = ""):
    if not DEBUG:
        return
    # Batch popup dismiss logs
    if _popup_batch_active and agent_name == "popup":
        _popup_batch.append(detail)
        return
    elapsed = time.time() - _t0
    suffix = f" — {detail}" if detail else ""
    print(f"  [{elapsed:6.1f}s]   > {agent_name}{suffix}", flush=True)


def start_popup_batch():
    """Start batching popup dismiss logs."""
    global _popup_batch_active
    _popup_batch.clear()
    _popup_batch_active = True


def flush_popup_batch():
    """End batching and log a summary if any popups were dismissed."""
    global _popup_batch_active
    _popup_batch_active = False
    if not _popup_batch or not DEBUG:
        _popup_batch.clear()
        return
    count = sum(1 for d in _popup_batch if d.startswith("dismiss:"))
    _popup_batch.clear()
    if count > 0:
        elapsed = time.time() - _t0
        print(f"  [{elapsed:6.1f}s]   > popup — dismissed {count} overlay(s)", flush=True)
