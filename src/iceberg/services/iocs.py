"""Indicators of compromise (IOCs): notebook-scoped CRUD.

Light-touch, *transient* staging — the authoritative IOC store is external
(MISP). Indicators are recorded manually now; a report cites a subset for its
Indicators appendix (``services/reports.set_ioc_citations``) and pushes them to
MISP as one event (``services/misp.py``).

Single source of truth shared by the JSON API and the portal (like
``services/diamond.py`` / ``services/reports.py``, this module raises
``fastapi.HTTPException`` directly so the rules can't drift between the two
presentation layers).

The ``extract`` function is the **seam for a future LLM/AI phase** that will
auto-suggest indicators from a source's text — it is deliberately unimplemented
here (manual entry only in this FR), mirroring ``FeedItem.content`` as the
"future IOC extraction" seam.
"""

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
# Future LLM/AI phase seam — auto-extraction of indicators from source text.
# Deliberately unimplemented in this FR (manual entry only).
# --------------------------------------------------------------------------- #
def extract(text: str) -> list[dict]:  # pragma: no cover - seam, not yet wired
    """Suggest indicators from free text — the future LLM/AI extraction entry
    point. Returns ``[{"ioc_type": IOCType, "value": str}, ...]``. Not wired in
    this FR; indicators are entered manually."""
    raise NotImplementedError("IOC auto-extraction lands in the LLM/AI phase")
