"""Inline-embed token syntax — the single source of truth for the `[[…]]` tokens
an analyst writes in a report body to embed an analytic artefact.

Both the HTML assembler (``services/product_html.py``) and the PDF renderer
(``rendering/typst.py``) resolve these tokens, so the patterns live here in one
neutral, dependency-free module rather than being declared (and risking drift) in
each layer. ``[[diamond:ID]]`` / ``[[figure:ID]]`` / ``[[ach:ID]]`` carry a row id;
the bare ``[[attack]]`` derives its content from the report's own technique tags.
"""

import re

DIAMOND_TOKEN_RE = re.compile(r"\[\[diamond:(\d+)\]\]")
FIGURE_TOKEN_RE = re.compile(r"\[\[figure:(\d+)\]\]")
ACH_TOKEN_RE = re.compile(r"\[\[ach:(\d+)\]\]")
ATTACK_TOKEN_RE = re.compile(r"\[\[attack\]\]")
