"""Base agent interface."""

from abc import ABC, abstractmethod


class Agent(ABC):
    """Base class for all challenge agents."""

    name: str = "base"

    @abstractmethod
    def run(self, page, step: int, version: int) -> bool:
        """Execute the agent's action. Returns True if successful."""
        ...

    # ── DOM Signature (used by subclasses via inheritance) ────────────

    def _get_dom_signature(self, page) -> str:
        """Lightweight DOM signature: element count + tag distribution + text hash."""
        try:
            return page.evaluate(r'''() => {
                const tags = {};
                const els = document.querySelectorAll('body *:not(script):not(style)');
                els.forEach(el => {
                    const t = el.tagName;
                    tags[t] = (tags[t] || 0) + 1;
                });
                const textLen = (document.body?.innerText || '').length;
                const keys = Object.keys(tags).sort();
                return keys.map(k => k + ':' + tags[k]).join(',') + '|' + textLen;
            }''')
        except Exception:
            return ''

    def _compute_change_score(self, before: str, after: str) -> float:
        """Quick structural change score 0.0-1.0."""
        if before == after:
            return 0.0
        if not before or not after:
            return 1.0
        try:
            b_parts = before.split('|')
            a_parts = after.split('|')
            b_tags = dict(p.split(':') for p in b_parts[0].split(',') if ':' in p) if b_parts[0] else {}
            a_tags = dict(p.split(':') for p in a_parts[0].split(',') if ':' in p) if a_parts[0] else {}
            b_len = int(b_parts[1]) if len(b_parts) > 1 else 0
            a_len = int(a_parts[1]) if len(a_parts) > 1 else 0

            # Tag distribution change
            all_tags = set(b_tags) | set(a_tags)
            if all_tags:
                tag_diff = sum(abs(int(b_tags.get(t, 0)) - int(a_tags.get(t, 0))) for t in all_tags)
                tag_total = sum(int(b_tags.get(t, 0)) for t in all_tags) + sum(int(a_tags.get(t, 0)) for t in all_tags)
                tag_score = tag_diff / max(tag_total, 1)
            else:
                tag_score = 0.0

            # Text length change
            len_score = abs(a_len - b_len) / max(a_len, b_len, 1)

            return min(1.0, tag_score * 0.6 + len_score * 0.4)
        except Exception:
            return 0.5  # Assume some change on parse error

    def __repr__(self):
        return f"<Agent:{self.name}>"
