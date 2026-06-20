"""Shared helpers for the hand-built inline SVG diagrams (Diamond / ACH / ATT&CK).

These diagrams are assembled by string templating (no SVG dependency — same spirit
as the inline ``_glyph.html`` mark). The single hard rule: **all dynamic text must
pass through** :func:`escape` before it reaches the markup, so an author-supplied
value can never inject elements. The font constants keep the diagrams visually
consistent with the portal design system (Archivo / JetBrains Mono).
"""

from __future__ import annotations

from xml.sax.saxutils import escape  # nosec B406 — escapes text for SVG output, never parses XML

__all__ = ["escape", "SANS", "MONO", "wrap_lines", "placard"]

SANS = "Archivo, 'Helvetica Neue', Arial, sans-serif"
MONO = "'JetBrains Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace"


def wrap_lines(text: str, *, max_chars: int = 28, max_lines: int = 3) -> list[str]:
    """Greedy word-wrap with hard-truncation + ellipsis when overflowing."""
    raw = " ".join((text or "").split())
    if not raw:
        return []
    words = raw.split(" ")
    lines: list[str] = []
    cur = ""
    i = 0
    while i < len(words) and len(lines) < max_lines:
        word = words[i]
        if len(word) > max_chars:
            word = word[: max_chars - 1] + "…"
        candidate = (cur + " " + word).strip()
        if len(candidate) <= max_chars or cur == "":
            cur = candidate
            i += 1
        else:
            lines.append(cur)
            cur = ""
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if i < len(words) and lines and not lines[-1].endswith("…"):  # ran out of room
        lines[-1] = lines[-1][: max_chars - 2].rstrip() + " …"
    return lines


def placard(eyebrow: str, message: str, *, height: int = 190, aria_label: str) -> str:
    """An empty-state SVG card: a left-aligned eyebrow label over a centred italic
    message. Used when a matrix has nothing to render yet."""
    msg_y = height / 2 + 15
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 {height}" '
        f'width="600" height="{height}" role="img" aria-label="{escape(aria_label)}">'
        f'<rect x="1" y="1" width="598" height="{height - 2}" rx="14" ry="14" '
        'fill="#fbfdfe" stroke="#e3e9ef" stroke-width="1.5"/>'
        f'<text x="30" y="36" font-family="{MONO}" font-size="10.5" '
        f'font-weight="700" letter-spacing="1.6" fill="#1f6f93">{escape(eyebrow)}</text>'
        f'<text x="300" y="{msg_y:.0f}" text-anchor="middle" font-family="{SANS}" '
        f'font-size="14" font-style="italic" fill="#a6aeb8">{escape(message)}</text>'
        "</svg>"
    )
