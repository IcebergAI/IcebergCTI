"""Render markdown to sanitized HTML for the live preview and portal display.

Reports are authored by analysts but the rendered HTML is still passed through
nh3 (the ammonia sanitizer) so any inline HTML/script in markdown can never
execute in a viewer's browser.
"""

import nh3
from markdown_it import MarkdownIt

_md = (
    MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    .enable("table")
    .enable("strikethrough")
)


def render_markdown(text: str) -> str:
    raw_html = _md.render(text or "")
    return nh3.clean(raw_html)
