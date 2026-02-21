"""DNA Reasoner — System 2 solver that clusters page elements by computed style.

Scans all visible DOM elements, extracts their "DNA" (computed style fingerprint),
clusters by normalized DNA key, then tries to assemble a 6-char code from each
cluster. Scoring picks the cluster most likely to contain the real code (monospace
bonus, small cluster bonus, known DNA bonus, proximity to code labels).

Uses only visible information (innerText, computed style, bounding box) — no
invisible attribute scanning. See MISSION.md.
"""

import re
from agents.base import Agent
from config import CHARSET
from code_scorer import is_valid_code, is_valid_code_hard, is_static_decoy
from log import log_stage


# --- Enhanced DNA Scraper JS — handles Shadow DOM, inputs, hidden text ---

DETECT_DOM_DNA_JS = """() => {
    const results = [];

    function processElement(el) {
        // Use innerText for visible text (what a human sees), not textContent
        let text = '';
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
            text = el.value || el.placeholder || '';
        } else {
            // Own text nodes only to avoid double-counting from parent elements
            for (const n of el.childNodes) {
                if (n.nodeType === 3) text += n.textContent;
            }
            text = text.trim();
            if (!text && el.childElementCount === 0) {
                text = (el.innerText || '').trim();
            }
        }
        if (!text || text.length > 64) return;

        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();

        results.push({
            text: text,
            tag: el.tagName.toLowerCase(),
            dna: {
                color: style.color,
                backgroundColor: style.backgroundColor,
                fontSize: style.fontSize,
                fontWeight: style.fontWeight,
                fontFamily: style.fontFamily,
                letterSpacing: style.letterSpacing,
                textTransform: style.textTransform,
                textDecorationLine: style.textDecorationLine,
                display: style.display,
                visibility: style.visibility,
                opacity: style.opacity,
                zIndex: style.zIndex,
                pointerEvents: style.pointerEvents
            },
            spatial: {
                x: Math.round(rect.left + rect.width / 2),
                y: Math.round(rect.top + rect.height / 2),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
                visible: rect.width > 0 && rect.height > 0
                         && style.opacity !== '0'
                         && style.visibility !== 'hidden'
                         && style.display !== 'none'
            }
        });
    }

    // 1. Regular DOM traversal
    const elements = document.querySelectorAll('body *:not(script):not(style):not(link)');
    elements.forEach(processElement);

    // 2. Shadow DOM traversal — pierce shadow roots
    function traverseShadow(root) {
        if (!root) return;
        const children = root.querySelectorAll('*');
        children.forEach(el => {
            processElement(el);
            if (el.shadowRoot) traverseShadow(el.shadowRoot);
        });
    }
    elements.forEach(el => {
        if (el.shadowRoot) traverseShadow(el.shadowRoot);
    });

    return results;
}"""


CHARSET_SET = set(CHARSET)


class DNAReasoner(Agent):
    name = "dna_reasoner"

    def __init__(self):
        self.last_winning_dna = None

    def run(self, page, step, version, context=None) -> str | None:
        """System 2: Full DNA analysis pipeline."""
        elements = self.scan(page)
        clusters = self.cluster_by_dna(elements)
        known_dna = (context or {}).get('known_dna')
        code, dna = self.find_code_cluster(clusters, known_dna)
        if code:
            self.last_winning_dna = dna
            log_stage("dna", f"found code {code} via clustering")
        return code

    def scan(self, page) -> list[dict]:
        """Run DETECT_DOM_DNA_JS and return raw element list."""
        try:
            return page.evaluate(DETECT_DOM_DNA_JS)
        except Exception as e:
            log_stage("dna", f"scan error: {e}")
            return []

    # --- Normalization ---

    def _normalize_color(self, color: str) -> str:
        """rgba(0,0,0,1) -> #000000, rgb(255,0,0) -> #ff0000."""
        color = color.strip().lower()
        m = re.match(r'rgba?\((\d+),\s*(\d+),\s*(\d+)', color)
        if m:
            return f"#{int(m.group(1)):02x}{int(m.group(2)):02x}{int(m.group(3)):02x}"
        if color.startswith('#'):
            return color[:7]
        return color

    def _normalize_font_size(self, size: str) -> str:
        """'16.5px' -> '16.5', '1em' -> '1em'."""
        m = re.match(r'([\d.]+)', size)
        return f"{round(float(m.group(1)), 1)}" if m else size

    def _bucket_opacity(self, opacity: str) -> str:
        """Bucket opacity: '1' vs '<1'."""
        try:
            return '1' if float(opacity) >= 1.0 else '<1'
        except (ValueError, TypeError):
            return '1'

    def _make_dna_key(self, dna: dict) -> str:
        """Create NORMALIZED DNA key for clustering.

        Includes: color, backgroundColor, fontSize, fontWeight, fontFamily,
        opacity bucket, textDecorationLine.
        """
        color = self._normalize_color(dna.get('color', ''))
        bg = self._normalize_color(dna.get('backgroundColor', ''))
        size = self._normalize_font_size(dna.get('fontSize', ''))
        weight = dna.get('fontWeight', '')
        family = dna.get('fontFamily', '').split(',')[0].strip().strip('"').strip("'").lower()
        opacity = self._bucket_opacity(dna.get('opacity', '1'))
        deco = dna.get('textDecorationLine', 'none')
        return f"{color}|{bg}|{size}|{weight}|{family}|{opacity}|{deco}"

    # --- Clustering ---

    def cluster_by_dna(self, elements: list[dict]) -> dict[str, list[dict]]:
        """Group visible elements by normalized DNA key."""
        clusters = {}
        for el in elements:
            if not el.get('spatial', {}).get('visible', False):
                continue
            dna = el.get('dna', {})
            key = self._make_dna_key(dna)
            clusters.setdefault(key, []).append(el)
        return clusters

    # --- Code Assembly ---

    def _try_assemble_code(self, cluster_elements: list[dict]) -> str | None:
        """Try to assemble a 6-char code from clustered fragments.

        Bi-directional sort: Y-bucket by fontSize*1.2 (rows), then X (columns).
        Handles scattered code pieces that wrap or are vertically offset.
        """
        # 1. Check if any single element contains a valid 6-char code
        for el in cluster_elements:
            text = el.get('text', '').strip().upper()
            if len(text) == 6 and all(c in CHARSET_SET for c in text):
                return text

        # 2. Row bucketing: group by Y within fontSize*1.2 tolerance
        if not cluster_elements:
            return None

        first_size = cluster_elements[0].get('dna', {}).get('fontSize', '16px')
        m = re.match(r'([\d.]+)', first_size)
        row_height = float(m.group(1)) * 1.2 if m else 20.0

        sorted_by_y = sorted(cluster_elements, key=lambda e: e['spatial']['y'])
        rows = []
        current_row = [sorted_by_y[0]]
        for el in sorted_by_y[1:]:
            if abs(el['spatial']['y'] - current_row[0]['spatial']['y']) <= row_height:
                current_row.append(el)
            else:
                rows.append(current_row)
                current_row = [el]
        rows.append(current_row)

        # Sort within each row by X, then assemble
        chars = []
        for row in rows:
            row.sort(key=lambda e: e['spatial']['x'])
            for el in row:
                text = el.get('text', '').strip().upper()
                for c in text:
                    if c in CHARSET_SET:
                        chars.append(c)

        if len(chars) == 6:
            code = ''.join(chars)
            if all(c in CHARSET_SET for c in code):
                return code

        # 3. Try concatenating all text and extracting a 6-char match
        all_text = ''.join(el.get('text', '') for el in cluster_elements).upper()
        matches = re.findall(f'[{CHARSET}]{{6}}', all_text)
        for match in matches:
            # Use hard validation here — decoy filtering is done in find_code_cluster
            if is_valid_code_hard(match):
                return match

        return None

    # --- Cluster Selection ---

    def find_code_cluster(self, clusters, known_dna=None) -> tuple[str | None, dict | None]:
        """Find cluster most likely containing the real code.

        Scoring: known DNA bonus + monospace bonus + small cluster bonus
                 + proximity to "code"/"solution" labels.

        Uses hard validation (charset + used-in-session) instead of soft
        (which also filters decoys). Decoy override logic allows known-DNA
        matches or strong non-DNA evidence to recover decoy-word codes.
        """
        candidates = []
        used_codes = getattr(self, '_session_used_codes', None)

        for dna_key, elements in clusters.items():
            if len(elements) > 20:
                continue  # Too large = ambient UI text

            # Skip nav/footer regions
            avg_y = sum(e['spatial']['y'] for e in elements) / len(elements)
            if avg_y > 900:
                continue

            # Known DNA bonus
            dna_match_bonus = 0.0
            has_dna_match = False
            if known_dna:
                ref_key = self._make_dna_key(known_dna)
                if dna_key == ref_key:
                    dna_match_bonus = 3.0
                    has_dna_match = True

            code = self._try_assemble_code(elements)
            if not code:
                continue
            # Hard validation only (charset + used-in-session, no decoy filter)
            if not is_valid_code_hard(code, used_codes):
                continue

            score = dna_match_bonus
            first_dna = elements[0].get('dna', {})

            # Monospace bonus
            if self._is_mono_family(first_dna.get('fontFamily', '')):
                score += 1.5

            # Small cluster bonus (focused = good)
            score += max(0, 1.5 - len(elements) / 8)

            # Highlighted background bonus
            bg = first_dna.get('backgroundColor', '')
            if bg and bg != 'rgba(0, 0, 0, 0)' and bg != 'transparent':
                score += 0.5

            # Proximity to "code"/"solution" visible text
            for el in elements:
                el_text = el.get('text', '').lower()
                if any(w in el_text for w in ['code', 'solution', 'answer', 'result']):
                    score += 1.0
                    break

            # Decoy override rule:
            # - If has_dna_match (exact known DNA) → allow decoy through
            # - Otherwise, need strong non-DNA evidence to override
            decoy = is_static_decoy(code)
            if decoy and not has_dna_match:
                non_dna_score = score - dna_match_bonus
                if non_dna_score < 2.5:
                    continue
            if decoy:
                log_stage("dna", f"allowing decoy '{code}' — dna_match={has_dna_match}, score={score:.1f}")

            candidates.append((score, code, dna_key, first_dna))

        if candidates:
            candidates.sort(reverse=True, key=lambda x: x[0])
            _, code, dna_key, dna = candidates[0]
            log_stage("dna", f"best cluster: score={candidates[0][0]:.1f}, "
                             f"code={code}, elements={len(clusters.get(dna_key, []))}")
            return code, dna

        return None, None

    def find_signature_for_code(self, elements: list[dict], known_code: str) -> dict | None:
        """Find the DNA signature of elements containing the known winning code.
        Much more reliable than find_code_cluster() since we know the answer."""
        known_upper = known_code.upper()

        # 1. Check if any single element contains the code
        for el in elements:
            text = el.get('text', '').strip().upper()
            if known_upper in text:
                return el.get('dna')

        # 2. Fallback: check if code chars are spread across elements with same DNA
        clusters = self.cluster_by_dna(elements)
        for dna_key, cluster_elements in clusters.items():
            code = self._try_assemble_code(cluster_elements)
            if code and code.upper() == known_upper:
                return cluster_elements[0].get('dna')

        return None

    def _is_mono_family(self, font_family: str) -> bool:
        mono = ['mono', 'courier', 'consolas', 'menlo', 'fira code', 'source code']
        return any(m in font_family.lower() for m in mono)
