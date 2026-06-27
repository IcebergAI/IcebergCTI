"""Indicators of compromise (IOCs): notebook-scoped CRUD.

Light-touch, *transient* staging — the authoritative IOC store is external
(MISP). Indicators are recorded manually now; a report cites a subset for its
Indicators appendix (``services/reports.set_ioc_citations``) and pushes them to
MISP as one event (``services/misp.py``).

Single source of truth shared by the JSON API and the portal (like
``services/diamond.py`` / ``services/reports.py``, this module raises
``fastapi.HTTPException`` directly so the rules can't drift between the two
presentation layers).

``normalise_candidates`` is the IOC half of the **AI extraction** path (FR #95):
the governed ``ioc_extract`` task (``services/ai.py`` + ``api/ai.py``) turns a
source's text into candidate rows, and this module refangs + constrains them to
the curated :class:`IOCType` set before the analyst promotes a subset via
``create_ioc``. The extraction itself reads content already in the notebook —
there is no server-side fetcher.
"""

import re

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..models import IOC, IOCType, Notebook, utcnow


def get_scoped(session: Session, notebook_id: int, ioc_id: int) -> IOC:
    """Fetch an IOC, 404-ing if it isn't in the given notebook (scoping)."""
    ioc = session.get(IOC, ioc_id)
    if not ioc or ioc.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Indicator not found")
    return ioc


def list_for_notebook(session: Session, notebook_id: int) -> list[IOC]:
    return list(
        session.exec(
            select(IOC)
            .where(IOC.notebook_id == notebook_id)
            .order_by(col(IOC.created_at))
        ).all()
    )


def create_ioc(
    session: Session,
    notebook: Notebook,
    *,
    ioc_type: IOCType = IOCType.DOMAIN,
    value: str,
    description: str = "",
    source_id: int | None = None,
) -> IOC:
    """Create an indicator under a notebook.

    A ``source_id`` is accepted only when it names a source in the *same*
    notebook (provenance can't cross a notebook boundary); anything else is
    dropped to ``None``."""
    value = (value or "").strip()
    if not value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Indicator value is required")
    ioc = IOC(
        notebook_id=notebook.id,
        ioc_type=ioc_type,
        value=value,
        description=description,
        source_id=_scoped_source_id(session, notebook.id, source_id),
    )
    session.add(ioc)
    session.commit()
    session.refresh(ioc)
    return ioc


def update_ioc(session: Session, ioc: IOC, **fields) -> IOC:
    """Apply non-None fields (a ``source_id`` is re-validated against the notebook)."""
    if "source_id" in fields:
        fields["source_id"] = _scoped_source_id(
            session, ioc.notebook_id, fields["source_id"]
        )
    if (value := fields.get("value")) is not None and not value.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Indicator value is required")
    for key, value in fields.items():
        if value is not None and hasattr(ioc, key):
            setattr(ioc, key, value.strip() if isinstance(value, str) else value)
    ioc.updated_at = utcnow()
    session.add(ioc)
    session.commit()
    session.refresh(ioc)
    return ioc


def delete_ioc(session: Session, ioc: IOC) -> None:
    session.delete(ioc)
    session.commit()


def _scoped_source_id(
    session: Session, notebook_id: int, source_id: int | None
) -> int | None:
    """Return ``source_id`` only if it names a source in this notebook, else None."""
    if not source_id:
        return None
    from ..models import Source

    src = session.get(Source, source_id)
    return source_id if src and src.notebook_id == notebook_id else None


# --------------------------------------------------------------------------- #
# AI extraction (FR #95) — normalise candidate indicators suggested by the
# governed ``ioc_extract`` task into clean, MISP-pushable rows. The text->candidate
# step happens in the AI backend (services/ai.py); this is the IOC-domain half.
# --------------------------------------------------------------------------- #
_DEFANG_SUBS = (
    (re.compile(r"^h(?:xx|XX)p", re.IGNORECASE), "http"),  # hxxp[s] -> http[s]
    (re.compile(r"[\[(){}]\s*\.\s*[\])}]"), "."),  # [.] (.) {.} -> .
    (re.compile(r"[\[(){}]\s*:\s*[\])}]"), ":"),  # [:] -> :
    (re.compile(r"[\[(){}]\s*(?:@|at)\s*[\])}]", re.IGNORECASE), "@"),  # [at] [@] -> @
    (re.compile(r"[\[(){}]\s*dot\s*[\])}]", re.IGNORECASE), "."),  # [dot] -> .
)


def refang(value: str) -> str:
    """Normalise common defanged indicator forms (``hxxp://1[.]2[.]3[.]4`` →
    ``http://1.2.3.4``). Pure string work — no network, no parsing of arbitrary
    text. Unknown input is returned stripped but otherwise untouched."""
    value = (value or "").strip()
    for pattern, repl in _DEFANG_SUBS:
        value = pattern.sub(repl, value)
    return value


def normalise_candidates(raw: list[dict]) -> list[dict]:
    """Clean AI-suggested indicator candidates into promotable rows.

    For each ``{"ioc_type", "value", "description"}`` row: coerce ``ioc_type`` to
    a valid :class:`IOCType` (dropping non-conforming types so the result stays
    MISP-pushable), :func:`refang` + strip the value (dropping blanks), keep an
    optional string ``description``, and dedupe on ``(ioc_type, value)``. Order
    is preserved (first occurrence wins)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            ioc_type = IOCType(row.get("ioc_type"))
        except ValueError:
            continue
        value = refang(str(row.get("value") or ""))
        if not value:
            continue
        key = (ioc_type.value, value)
        if key in seen:
            continue
        seen.add(key)
        description = row.get("description") or ""
        out.append(
            {
                "ioc_type": ioc_type.value,
                "value": value,
                "description": str(description).strip(),
            }
        )
    return out
