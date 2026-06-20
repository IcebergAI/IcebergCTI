"""Analysis of Competing Hypotheses (Heuer) matrices: CRUD, the inconsistency
scoring, SVG matrix generation, and the inline-token rendering that embeds a
matrix into a report.

Single source of truth shared by the JSON API and the portal (like
``services/diamond.py`` it raises ``fastapi.HTTPException`` directly so the rules
can't drift between the two presentation layers).

Embedding is **by token, not by a link table**: an analyst writes ``[[ach:ID]]``
in a report's markdown body and the renderer swaps it for the matrix. The same
``render_ach_svg`` output feeds three surfaces — the web report view, the live
preview, and the Typst PDF (Typst renders SVG natively). All dynamic text is
XML-escaped when the SVG is built, so an author can never inject markup through a
hypothesis / evidence / question field.

ACH's discriminating idea is *inconsistency*: evidence never confirms a
hypothesis, it only weakens the ones it contradicts, so the hypothesis carrying
the **fewest weighted inconsistencies** is the most tenable. The matrix stores
hypotheses / evidence as rows with **stable string ids** and ``ratings`` keyed
``"{hid}:{eid}"`` so deleting a row never silently re-keys the matrix.
"""

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..embeds import ACH_TOKEN_RE  # noqa: F401 — re-exported for callers
from ..models import ACHCellRating, ACHModel, Notebook, Report, utcnow
from ..rendering.svg import MONO as _MONO, SANS as _SANS, escape, wrap_lines as _wrap_lines
from ..rendering.svg import placard as _svg_placard


def referenced_ids(text: str) -> list[int]:
    """The ACH ids referenced in a body, in first-appearance order."""
    seen: list[int] = []
    for m in ACH_TOKEN_RE.finditer(text or ""):
        i = int(m.group(1))
        if i not in seen:
            seen.append(i)
    return seen


# --------------------------------------------------------------------------- #
# Matrix normalisation: stable ids, dropped blanks, pruned orphan ratings.
# --------------------------------------------------------------------------- #
def _normalise_rows(rows: list | None, prefix: str) -> list[dict]:
    """Coerce raw hypothesis/evidence rows into ``[{"id", "text"}]``: drop
    empty-text rows, keep a valid unique existing id, else allocate the next
    ``{prefix}{n}`` (above the max existing) so new rows never collide."""
    rows = rows or []
    max_n = 0
    for row in rows:
        rid = row.get("id") if isinstance(row, dict) else None
        if isinstance(rid, str) and rid.startswith(prefix) and rid[len(prefix):].isdigit():
            max_n = max(max_n, int(rid[len(prefix):]))
    out: list[dict] = []
    used: set[str] = set()
    counter = max_n
    for row in rows:
        if isinstance(row, dict):
            text = str(row.get("text", "")).strip()
            rid = row.get("id")
        else:  # tolerate a plain string row
            text, rid = str(row).strip(), None
        if not text:
            continue
        valid = (
            isinstance(rid, str)
            and rid.startswith(prefix)
            and rid[len(prefix):].isdigit()
            and rid not in used
        )
        if not valid:
            counter += 1
            rid = f"{prefix}{counter}"
        used.add(rid)
        out.append({"id": rid, "text": text})
    return out


def normalise(
    hypotheses: list | None, evidence: list | None, ratings: dict | None
) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Return a consistent ``(hypotheses, evidence, ratings)`` triple: rows get
    stable ids, ratings are coerced to valid enum values with ``NEUTRAL`` (the
    default) and orphan keys (no longer referencing a live row) dropped."""
    hyps = _normalise_rows(hypotheses, "h")
    evs = _normalise_rows(evidence, "e")
    live = {f"{h['id']}:{e['id']}" for h in hyps for e in evs}
    clean: dict[str, str] = {}
    for key, value in (ratings or {}).items():
        if key not in live:
            continue
        try:
            rating = ACHCellRating(str(value).strip().upper())
        except ValueError:
            continue
        if rating is ACHCellRating.NEUTRAL:
            continue  # neutral is the implicit default — don't persist it
        clean[key] = rating.value
    return hyps, evs, clean


# --------------------------------------------------------------------------- #
# Scoring: the analytic payload — least-inconsistent hypothesis is most tenable.
# --------------------------------------------------------------------------- #
_INCONSISTENCY_WEIGHT = {
    ACHCellRating.INCONSISTENT: 1,
    ACHCellRating.STRONGLY_INCONSISTENT: 2,
}


def _rating(ach: ACHModel, hid: str, eid: str) -> ACHCellRating:
    raw = (ach.ratings or {}).get(f"{hid}:{eid}")
    try:
        return ACHCellRating(raw) if raw else ACHCellRating.NEUTRAL
    except ValueError:
        return ACHCellRating.NEUTRAL


def inconsistency_score(ach: ACHModel) -> dict[str, int]:
    """Per-hypothesis sum of inconsistency weights over its evidence column."""
    scores: dict[str, int] = {}
    for h in ach.hypotheses or []:
        hid = h.get("id")
        total = 0
        for e in ach.evidence or []:
            total += _INCONSISTENCY_WEIGHT.get(_rating(ach, hid, e.get("id")), 0)
        scores[hid] = total
    return scores


def leading_hypothesis_ids(ach: ACHModel) -> list[str]:
    """The hypothesis id(s) with the minimum inconsistency score (most tenable)."""
    scores = inconsistency_score(ach)
    if not scores:
        return []
    lo = min(scores.values())
    return [hid for hid, s in scores.items() if s == lo]


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def get_scoped(session: Session, notebook_id: int, ach_id: int) -> ACHModel:
    """Fetch an ACH matrix, 404-ing if it isn't in the given notebook (scoping)."""
    ach = session.get(ACHModel, ach_id)
    if not ach or ach.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ACH analysis not found")
    return ach


def create_ach(
    session: Session,
    notebook: Notebook,
    *,
    title: str,
    question: str = "",
    hypotheses: list | None = None,
    evidence: list | None = None,
    ratings: dict | None = None,
    notes: str = "",
) -> ACHModel:
    hyps, evs, rts = normalise(hypotheses, evidence, ratings)
    ach = ACHModel(
        notebook_id=notebook.id,
        title=title,
        question=question,
        hypotheses=hyps,
        evidence=evs,
        ratings=rts,
        notes=notes,
    )
    session.add(ach)
    session.commit()
    session.refresh(ach)
    return ach


def update_ach(session: Session, ach: ACHModel, **fields) -> ACHModel:
    """Apply non-None fields then re-normalise the matrix so ids stay stable and
    no orphan ratings survive a removed hypothesis/evidence row."""
    for key, value in fields.items():
        if value is not None and hasattr(ach, key):
            setattr(ach, key, value)
    ach.hypotheses, ach.evidence, ach.ratings = normalise(
        ach.hypotheses, ach.evidence, ach.ratings
    )
    ach.updated_at = utcnow()
    session.add(ach)
    session.commit()
    session.refresh(ach)
    return ach


def delete_ach(session: Session, ach: ACHModel) -> None:
    session.delete(ach)
    session.commit()


def _scoped_by_id(session: Session, notebook_id: int, text: str) -> dict[int, ACHModel]:
    """The ACH matrices referenced by ``text`` that belong to ``notebook_id``."""
    ids = referenced_ids(text)
    if not ids:
        return {}
    rows = session.exec(
        select(ACHModel).where(
            ACHModel.notebook_id == notebook_id,
            col(ACHModel.id).in_(ids),
        )
    ).all()
    return {a.id: a for a in rows}


def referenced_ach(session: Session, report: Report) -> list[ACHModel]:
    """ACH matrices embedded in a report's body, scoped to its notebook, body order."""
    found = _scoped_by_id(session, report.notebook_id, report.body_md)
    return [found[i] for i in referenced_ids(report.body_md) if i in found]


def scoped_ach_svg(session: Session, notebook_id: int, text: str) -> dict[int, str]:
    """Map of ACH id -> rendered SVG for the matrices referenced by ``text`` and
    owned by ``notebook_id`` (consumed by services/product_html.py + the PDF)."""
    return {
        i: render_ach_svg(a)
        for i, a in _scoped_by_id(session, notebook_id, text).items()
    }


# --------------------------------------------------------------------------- #
# SVG matrix
# --------------------------------------------------------------------------- #
# Glyph + fill + ink per rating. Both the glyph AND the colour encode the rating
# (never colour alone) — same accessibility discipline as the diamond confidence
# pip-meter. Consistency reads cool/supportive, inconsistency warm/caution.
_RATING_STYLE: dict[ACHCellRating, tuple[str, str, str]] = {
    ACHCellRating.STRONGLY_CONSISTENT: ("++", "#cfe9df", "#1d7a57"),
    ACHCellRating.CONSISTENT: ("+", "#e6f3ee", "#2a8a68"),
    ACHCellRating.NEUTRAL: ("·", "#f1f4f7", "#9aa3ad"),
    ACHCellRating.INCONSISTENT: ("−", "#f8e6da", "#b56a2c"),
    ACHCellRating.STRONGLY_INCONSISTENT: ("−−", "#f4cdbb", "#a8431a"),
    ACHCellRating.NOT_APPLICABLE: ("n/a", "#eceff2", "#aab2bb"),
}

_LEADING_FILL = "#eef6f2"  # tint behind the most-tenable (lowest-score) column
_LEADING_INK = "#1f6f93"

# Layout geometry (px). Width/height are derived from the matrix dimensions —
# the key departure from the diamond's fixed canvas.
_MARGIN = 24
_TOP = 96  # title / eyebrow band
_LEFT_W = 280  # evidence-label column
_COL_W = 128  # one hypothesis column
_HEADER_H = 86  # hypothesis header row
_ROW_H = 64  # one evidence row
_SUMMARY_H = 56
_FOOTER = 30


def _placard(message: str) -> str:
    """An empty-matrix SVG — meaningful when there are no hypotheses/evidence yet."""
    return _svg_placard(
        "ANALYSIS OF COMPETING HYPOTHESES",
        message,
        height=200,
        aria_label="Empty ACH matrix",
    )


def render_ach_svg(ach: ACHModel) -> str:
    """Render a self-contained SVG of an ACH matrix (hypotheses × evidence).

    Carries its own title, an inconsistency-score summary row with the leading
    (most-tenable) hypothesis flagged, a provenance footer, and an accessible
    name + description (``<title>`` / ``<desc>``) ranking the hypotheses. All
    dynamic text is XML-escaped, so a field can never inject markup.
    """
    hyps = ach.hypotheses or []
    evs = ach.evidence or []
    if not hyps or not evs:
        return _placard("Add at least one hypothesis and one piece of evidence.")

    n_h, n_e = len(hyps), len(evs)
    width = _MARGIN * 2 + _LEFT_W + n_h * _COL_W
    height = _TOP + _HEADER_H + n_e * _ROW_H + _SUMMARY_H + _FOOTER + _MARGIN
    gx, gy = _MARGIN, _TOP
    grid_x = gx + _LEFT_W  # x where hypothesis columns start

    scores = inconsistency_score(ach)
    leading = set(leading_hypothesis_ids(ach))
    sid = ach.id if getattr(ach, "id", None) is not None else "x"

    title = (
        _wrap_lines(ach.question or ach.title, max_chars=72, max_lines=1)[0]
        if (ach.question or ach.title).strip()
        else "Untitled analysis"
    )
    # accessible description: hypotheses ranked by inconsistency (asc)
    ranked = sorted(hyps, key=lambda h: scores.get(h.get("id"), 0))
    desc = "; ".join(
        f"{h.get('text', '').strip() or 'unnamed'}: "
        f"{scores.get(h.get('id'), 0)} inconsistency"
        for h in ranked
    )
    updated = ""
    stamp = getattr(ach, "updated_at", None)
    if stamp is not None:
        try:
            updated = stamp.strftime("%Y-%m-%d")
        except Exception:  # pragma: no cover - defensive
            updated = ""

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-labelledby="ach-t-{sid} ach-d-{sid}">',
        f'<title id="ach-t-{sid}">Analysis of Competing Hypotheses — '
        f"{escape(title)}</title>",
        f'<desc id="ach-d-{sid}">{escape(desc)}.</desc>',
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="14" '
        'ry="14" fill="#fbfdfe" stroke="#e3e9ef" stroke-width="1.5"/>',
        f'<text x="{gx}" y="40" font-family="{_MONO}" font-size="10.5" '
        'font-weight="700" letter-spacing="1.6" fill="#1f6f93">'
        "ANALYSIS OF COMPETING HYPOTHESES</text>",
        f'<text x="{gx}" y="66" font-family="{_SANS}" font-size="18" '
        f'font-weight="800" fill="#23272f">{escape(title)}</text>',
        f'<text x="{gx}" y="{gy + 14}" font-family="{_MONO}" font-size="9.5" '
        'font-weight="700" letter-spacing="0.8" fill="#8a939c">EVIDENCE ↓ · '
        "HYPOTHESES →</text>",
    ]

    # leading-column tints (drawn first, behind everything)
    for j, h in enumerate(hyps):
        if h.get("id") in leading:
            cx = grid_x + j * _COL_W
            parts.append(
                f'<rect x="{cx}" y="{gy}" width="{_COL_W}" '
                f'height="{_HEADER_H + n_e * _ROW_H + _SUMMARY_H}" '
                f'fill="{_LEADING_FILL}"/>'
            )

    # hypothesis headers
    for j, h in enumerate(hyps):
        cx = grid_x + j * _COL_W
        is_lead = h.get("id") in leading
        ink = _LEADING_INK if is_lead else "#5a6672"
        parts.append(
            f'<text x="{cx + _COL_W / 2:.0f}" y="{gy + 18}" text-anchor="middle" '
            f'font-family="{_MONO}" font-size="10" font-weight="700" '
            f'letter-spacing="0.6" fill="{ink}">H{j + 1}</text>'
        )
        ty = gy + 38
        for line in _wrap_lines(h.get("text", ""), max_chars=18, max_lines=2):
            parts.append(
                f'<text x="{cx + _COL_W / 2:.0f}" y="{ty}" text-anchor="middle" '
                f'font-family="{_SANS}" font-size="11" fill="#2b2f3a">'
                f"{escape(line)}</text>"
            )
            ty += 14
        if is_lead:
            parts.append(
                f'<text x="{cx + _COL_W / 2:.0f}" y="{gy + _HEADER_H - 6}" '
                f'text-anchor="middle" font-family="{_MONO}" font-size="8" '
                f'font-weight="700" letter-spacing="0.8" fill="{_LEADING_INK}">'
                "▲ LEADING</text>"
            )

    body_top = gy + _HEADER_H
    # evidence rows + cells
    for i, e in enumerate(evs):
        ry = body_top + i * _ROW_H
        # row separator
        parts.append(
            f'<line x1="{gx}" y1="{ry}" x2="{gx + _LEFT_W + n_h * _COL_W}" '
            f'y2="{ry}" stroke="#edf1f5" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{gx + 4}" y="{ry + 18}" font-family="{_MONO}" '
            f'font-size="9.5" font-weight="700" fill="#9aa3ad">E{i + 1}</text>'
        )
        ty = ry + 18
        for line in _wrap_lines(e.get("text", ""), max_chars=40, max_lines=3):
            parts.append(
                f'<text x="{gx + 30}" y="{ty}" font-family="{_SANS}" '
                f'font-size="11.5" fill="#2b2f3a">{escape(line)}</text>'
            )
            ty += 15
        for j, h in enumerate(hyps):
            rating = _rating(ach, h.get("id"), e.get("id"))
            glyph, fill, ink = _RATING_STYLE[rating]
            cx = grid_x + j * _COL_W
            parts.append(
                f'<rect x="{cx + 8}" y="{ry + 10}" width="{_COL_W - 16}" '
                f'height="{_ROW_H - 20}" rx="7" ry="7" fill="{fill}"/>'
            )
            parts.append(
                f'<text x="{cx + _COL_W / 2:.0f}" y="{ry + _ROW_H / 2 + 5:.0f}" '
                f'text-anchor="middle" font-family="{_MONO}" font-size="15" '
                f'font-weight="700" fill="{ink}">{escape(glyph)}</text>'
            )

    # summary row — inconsistency score per hypothesis
    sy = body_top + n_e * _ROW_H
    parts.append(
        f'<line x1="{gx}" y1="{sy}" x2="{gx + _LEFT_W + n_h * _COL_W}" '
        f'y2="{sy}" stroke="#cdd6df" stroke-width="1.4"/>'
    )
    parts.append(
        f'<text x="{gx + 4}" y="{sy + 33}" font-family="{_MONO}" font-size="10" '
        'font-weight="700" letter-spacing="0.6" fill="#5a6672">'
        "INCONSISTENCY SCORE</text>"
    )
    for j, h in enumerate(hyps):
        cx = grid_x + j * _COL_W
        is_lead = h.get("id") in leading
        ink = _LEADING_INK if is_lead else "#3a414b"
        parts.append(
            f'<text x="{cx + _COL_W / 2:.0f}" y="{sy + 36}" text-anchor="middle" '
            f'font-family="{_MONO}" font-size="18" font-weight="800" fill="{ink}">'
            f"{scores.get(h.get('id'), 0)}</text>"
        )

    # footer
    fy = height - _MARGIN
    parts.append(
        f'<text x="{gx}" y="{fy}" font-family="{_MONO}" font-size="9" '
        'letter-spacing="0.4" fill="#9aa3ad">LEAST INCONSISTENT = MOST '
        "TENABLE</text>"
    )
    if updated:
        parts.append(
            f'<text x="{width - _MARGIN}" y="{fy}" text-anchor="end" '
            f'font-family="{_MONO}" font-size="9" letter-spacing="0.4" '
            f'fill="#9aa3ad">UPDATED {escape(updated)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
