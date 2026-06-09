"""Notebooks, sources and notes — the analyst collection workspace."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import Note, Notebook, Role, Source, utcnow
from ..schemas import (
    NoteCreate,
    NotebookCreate,
    NotebookUpdate,
    RequirementLinks,
    SourceCreate,
)
from ..services.requirements import set_notebook_requirements

router = APIRouter(prefix="/notebooks", tags=["notebooks"])

SessionDep = Annotated[Session, Depends(get_session)]
# Writers: analysts and reviewers (admin passes via require_role).
Writer = Annotated[object, Depends(require_role(Role.ANALYST, Role.REVIEWER))]


def _get_notebook(session: Session, notebook_id: int) -> Notebook:
    notebook = session.get(Notebook, notebook_id)
    if not notebook:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    return notebook


@router.get("")
def list_notebooks(session: SessionDep, _user: CurrentUser) -> list[Notebook]:
    return list(session.exec(select(Notebook).order_by(Notebook.updated_at.desc())).all())


@router.post("", status_code=status.HTTP_201_CREATED)
def create_notebook(
    body: NotebookCreate, session: SessionDep, user: CurrentUser, _w: Writer
) -> Notebook:
    notebook = Notebook(title=body.title, topic=body.topic, owner_id=user.id)
    session.add(notebook)
    session.commit()
    session.refresh(notebook)
    return notebook


@router.get("/{notebook_id}")
def get_notebook(notebook_id: int, session: SessionDep, _user: CurrentUser) -> dict:
    nb = _get_notebook(session, notebook_id)
    return {
        "notebook": nb,
        "sources": nb.sources,
        "notes": nb.notes,
        "reports": nb.reports,
    }


@router.patch("/{notebook_id}")
def update_notebook(
    notebook_id: int,
    body: NotebookUpdate,
    session: SessionDep,
    _w: Writer,
) -> Notebook:
    nb = _get_notebook(session, notebook_id)
    if body.title is not None:
        nb.title = body.title
    if body.topic is not None:
        nb.topic = body.topic
    nb.updated_at = utcnow()
    session.add(nb)
    session.commit()
    session.refresh(nb)
    return nb


@router.delete("/{notebook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notebook(notebook_id: int, session: SessionDep, user: CurrentUser):
    nb = _get_notebook(session, notebook_id)
    if nb.owner_id != user.id and user.role != Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can delete")
    session.delete(nb)
    session.commit()


@router.post("/{notebook_id}/sources", status_code=status.HTTP_201_CREATED)
def add_source(
    notebook_id: int, body: SourceCreate, session: SessionDep, _w: Writer
) -> Source:
    _get_notebook(session, notebook_id)
    source = Source(
        notebook_id=notebook_id,
        title=body.title,
        reference=body.reference,
        summary=body.summary,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@router.delete(
    "/{notebook_id}/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_source(
    notebook_id: int, source_id: int, session: SessionDep, _w: Writer
):
    source = session.get(Source, source_id)
    if not source or source.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    session.delete(source)
    session.commit()


@router.put("/{notebook_id}/requirements")
def update_notebook_requirements(
    notebook_id: int, body: RequirementLinks, session: SessionDep, _w: Writer
) -> dict:
    nb = _get_notebook(session, notebook_id)
    linked = set_notebook_requirements(session, nb, body.requirement_ids)
    return {"requirements": linked}


@router.post("/{notebook_id}/notes", status_code=status.HTTP_201_CREATED)
def add_note(
    notebook_id: int, body: NoteCreate, session: SessionDep, _w: Writer
) -> Note:
    _get_notebook(session, notebook_id)
    note = Note(notebook_id=notebook_id, body_md=body.body_md)
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


@router.delete(
    "/{notebook_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_note(notebook_id: int, note_id: int, session: SessionDep, _w: Writer):
    note = session.get(Note, note_id)
    if not note or note.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    session.delete(note)
    session.commit()
