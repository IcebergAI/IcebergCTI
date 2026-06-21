"""Advisory analytic-tradecraft lint helpers."""

import re

from ..help_content import HEDGING_TERMS


def hedging_warnings(*, body_md: str = "", key_judgements: str = "") -> list[dict]:
    """Return non-blocking warnings for vague estimative language.

    The output deliberately carries only location + term + guidance so it can be
    surfaced in the editor without storing or auditing report text.
    """
    warnings: list[dict] = []
    for field, text in (("body_md", body_md or ""), ("key_judgements", key_judgements or "")):
        lowered = text.lower()
        for term in HEDGING_TERMS:
            pattern = r"\b" + re.escape(term.lower()) + r"\b"
            if re.search(pattern, lowered):
                warnings.append(
                    {
                        "field": field,
                        "term": term,
                        "message": (
                            f"'{term}' is vague estimative language; consider a "
                            "probability-yardstick term."
                        ),
                        "href": "/help#estimative-language",
                    }
                )
    return warnings
