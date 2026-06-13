"""Notebook collection-material persistence shared by the JSON API and the
portal, so the two presentation layers create rows through one code path."""

from fastapi import BackgroundTasks, HTTPException, status
from sqlmodel import Session

from ..models import (
    Note,
    Notebook,
    Source,
    SourceCredibility,
    SourceGradingOrigin,
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
    reliability: SourceReliability | None = None,
    credibility: SourceCredibility | None = None,
    grading_rationale: str = "",
    background_tasks: BackgroundTasks | None = None,
) -> Source:
    source = Source(
        notebook_id=notebook.id, title=title, reference=reference, summary=summary
    )
    if reliability or credibility:
        # Manual grade: applied inline (no network).
        source_grading.set_manual_grade(
            source,
            reliability=reliability,
            credibility=credibility,
            rationale=grading_rationale,
        )
    elif background_tasks is not None and source_grading.needs_online_grading(source):
        # Auto-grade that would fetch a page / call an LLM: defer past the
        # response so creating a source never blocks on external network I/O.
        source.grading_origin = SourceGradingOrigin.PENDING
    else:
        # Offline heuristic (or off-request caller): grade inline — it's instant.
        source_grading.auto_grade(source)
    session.add(source)
    session.commit()
    session.refresh(source)
    if source.grading_origin == SourceGradingOrigin.PENDING and background_tasks is not None:
        background_tasks.add_task(source_grading.grade_source_async, source.id)
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
) -> Source:
    if title is not None:
        source.title = title
    if reference is not None:
        source.reference = reference
    if summary is not None:
        source.summary = summary
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
