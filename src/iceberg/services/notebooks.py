"""Notebook collection-material persistence shared by the JSON API and the
portal, so the two presentation layers create rows through one code path."""

from fastapi import HTTPException, status
from sqlmodel import Session

from ..models import (
    TLP,
    Note,
    Notebook,
    Source,
    SourceCredibility,
    SourceReliability,
)
from . import source_grading


def get_or_404(session: Session, notebook_id: int) -> Notebook:
    notebook = session.get(Notebook, notebook_id)
    if not notebook:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    return notebook


def create_notebook(
    session: Session, *, title: str, topic: str, owner_id: int
) -> Notebook:
    notebook = Notebook(title=title, topic=topic, owner_id=owner_id)
    session.add(notebook)
    session.commit()
    session.refresh(notebook)
    return notebook


def add_source(
    session: Session,
    notebook: Notebook,
    *,
    title: str,
    reference: str = "",
    summary: str = "",
    content_md: str = "",
    tlp: TLP = TLP.AMBER,
    reliability: SourceReliability | None = None,
    credibility: SourceCredibility | None = None,
    grading_rationale: str = "",
) -> Source:
    source = Source(
        notebook_id=notebook.id,
        title=title,
        reference=reference,
        summary=summary,
        content_md=content_md,
        tlp=tlp,
    )
    if reliability or credibility:
        source_grading.set_manual_grade(
            source,
            reliability=reliability,
            credibility=credibility,
            rationale=grading_rationale,
        )
    else:
        # Offline heuristic grade — instant, no network.
        source_grading.auto_grade(source)
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def get_source_or_404(session: Session, notebook_id: int, source_id: int) -> Source:
    source = session.get(Source, source_id)
    if not source or source.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    return source


def update_source(
    session: Session,
    source: Source,
    *,
    title: str | None = None,
    reference: str | None = None,
    summary: str | None = None,
    content_md: str | None = None,
    tlp: TLP | None = None,
) -> Source:
    if title is not None:
        source.title = title
    if reference is not None:
        source.reference = reference
    if summary is not None:
        source.summary = summary
    if content_md is not None:
        source.content_md = content_md
    if tlp is not None:
        source.tlp = tlp
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def add_note(session: Session, notebook: Notebook, *, body_md: str = "") -> Note:
    note = Note(notebook_id=notebook.id, body_md=body_md)
    session.add(note)
    session.commit()
    session.refresh(note)
    return note
