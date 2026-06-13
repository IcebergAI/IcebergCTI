"""Notebook collection-material persistence shared by the JSON API and the
portal, so the two presentation layers create rows through one code path."""

from fastapi import HTTPException, status
from sqlmodel import Session

from ..models import Note, Notebook, Source


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
) -> Source:
    source = Source(
        notebook_id=notebook.id, title=title, reference=reference, summary=summary
    )
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
