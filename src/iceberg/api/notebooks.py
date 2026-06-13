"""Notebooks, sources, notes and attachments — the analyst collection workspace."""

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, Response
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import (
    Attachment,
    DiamondModel,
    Note,
    Notebook,
    Role,
    Source,
    utcnow,
)
from ..schemas import (
    DiamondCreate,
    DiamondUpdate,
    NoteCreate,
    NotebookCreate,
    NotebookUpdate,
    RequirementLinks,
    SourceCreate,
)
from ..services import attachments as attachment_service
from ..services import diamond as diamond_service
from ..services import notebooks as notebook_service
from ..services.requirements import set_notebook_requirements

router = APIRouter(prefix="/notebooks", tags=["notebooks"])

SessionDep = Annotated[Session, Depends(get_session)]
# Writers: analysts and reviewers (admin passes via require_role).
Writer = Annotated[object, Depends(require_role(Role.ANALYST, Role.REVIEWER))]


_get_notebook = notebook_service.get_or_404


@router.get("")
def list_notebooks(session: SessionDep, _w: Writer) -> list[Notebook]:
    # Raw collection material is writer-only; read-only stakeholders consume
    # finished products (reports/feed/search), never notebooks.
    return list(session.exec(select(Notebook).order_by(Notebook.updated_at.desc())).all())


@router.post("", status_code=status.HTTP_201_CREATED)
def create_notebook(
    body: NotebookCreate, session: SessionDep, user: CurrentUser, _w: Writer
) -> Notebook:
    return notebook_service.create_notebook(
        session, title=body.title, topic=body.topic, owner_id=user.id
    )


@router.get("/{notebook_id}")
def get_notebook(notebook_id: int, session: SessionDep, _w: Writer) -> dict:
    nb = _get_notebook(session, notebook_id)
    return {
        "notebook": nb,
        "sources": nb.sources,
        "notes": nb.notes,
        "attachments": nb.attachments,
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
    # Capture attachment file paths before the DB rows cascade away, then unlink
    # them after the delete so no files are orphaned on disk.
    paths = [attachment_service.attachment_path(a) for a in nb.attachments]
    session.delete(nb)
    session.commit()
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


@router.post("/{notebook_id}/sources", status_code=status.HTTP_201_CREATED)
def add_source(
    notebook_id: int, body: SourceCreate, session: SessionDep, _w: Writer
) -> Source:
    nb = _get_notebook(session, notebook_id)
    return notebook_service.add_source(
        session, nb, title=body.title, reference=body.reference, summary=body.summary
    )


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


@router.post("/{notebook_id}/attachments", status_code=status.HTTP_201_CREATED)
def add_attachment(
    notebook_id: int,
    session: SessionDep,
    _w: Writer,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    summary: Annotated[str, Form()] = "",
) -> Attachment:
    nb = _get_notebook(session, notebook_id)
    return attachment_service.save_upload(
        session, nb, file, title=title, summary=summary
    )


def _get_attachment(
    session: Session, notebook_id: int, attachment_id: int
) -> Attachment:
    att = session.get(Attachment, attachment_id)
    if not att or att.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return att


@router.get("/{notebook_id}/attachments/{attachment_id}/download")
def download_attachment(
    notebook_id: int, attachment_id: int, session: SessionDep, _w: Writer
):
    att = _get_attachment(session, notebook_id, attachment_id)
    path = attachment_service.attachment_path(att)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored file missing")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=att.original_filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete(
    "/{notebook_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_attachment(
    notebook_id: int, attachment_id: int, session: SessionDep, _w: Writer
):
    att = _get_attachment(session, notebook_id, attachment_id)
    attachment_service.delete_attachment(session, att)


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
    nb = _get_notebook(session, notebook_id)
    return notebook_service.add_note(session, nb, body_md=body.body_md)


@router.delete(
    "/{notebook_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_note(notebook_id: int, note_id: int, session: SessionDep, _w: Writer):
    note = session.get(Note, note_id)
    if not note or note.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    session.delete(note)
    session.commit()


# --------------------------------------------------------------------------- #
# Diamond Model assessments
# --------------------------------------------------------------------------- #
@router.post("/{notebook_id}/diamonds", status_code=status.HTTP_201_CREATED)
def add_diamond(
    notebook_id: int, body: DiamondCreate, session: SessionDep, _w: Writer
) -> DiamondModel:
    nb = _get_notebook(session, notebook_id)
    return diamond_service.create_diamond(
        session,
        nb,
        title=body.title,
        adversary=body.adversary,
        capability=body.capability,
        infrastructure=body.infrastructure,
        victim=body.victim,
        confidence=body.confidence,
        notes=body.notes,
    )


@router.patch("/{notebook_id}/diamonds/{diamond_id}")
def update_diamond(
    notebook_id: int,
    diamond_id: int,
    body: DiamondUpdate,
    session: SessionDep,
    _w: Writer,
) -> DiamondModel:
    diamond = diamond_service.get_scoped(session, notebook_id, diamond_id)
    return diamond_service.update_diamond(
        session, diamond, **body.model_dump(exclude_unset=True)
    )


@router.get("/{notebook_id}/diamonds/{diamond_id}/diagram.svg")
def diamond_diagram(
    notebook_id: int, diamond_id: int, session: SessionDep, _w: Writer
):
    diamond = diamond_service.get_scoped(session, notebook_id, diamond_id)
    return Response(
        content=diamond_service.render_diamond_svg(diamond),
        media_type="image/svg+xml",
    )


@router.delete(
    "/{notebook_id}/diamonds/{diamond_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_diamond(
    notebook_id: int, diamond_id: int, session: SessionDep, _w: Writer
):
    diamond = diamond_service.get_scoped(session, notebook_id, diamond_id)
    diamond_service.delete_diamond(session, diamond)
