"""Notebooks, sources, notes and attachments — the analyst collection workspace."""

from typing import Annotated
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlmodel import Session, col, select

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import (
    ACHModel,
    Attachment,
    AuditAction,
    AuditCategory,
    DiamondModel,
    Figure,
    IOC,
    Note,
    Notebook,
    Role,
    RenderedProduct,
    Source,
    utcnow,
)
from ..schemas import (
    ACHCreate,
    ACHUpdate,
    DiamondCreate,
    DiamondUpdate,
    IOCCreate,
    IOCUpdate,
    NoteCreate,
    NotebookCreate,
    NotebookUpdate,
    RequirementLinks,
    SourceCreate,
    SourceGradeUpdate,
    SourceUpdate,
    TaxiiPullRequest,
    MispPullRequest,
)
from ..services import ach as ach_service
from ..services import attachments as attachment_service
from ..services import audit
from ..services import diamond as diamond_service
from ..services import demo as demo_service
from ..services import figures as figure_service
from ..services import iocs as ioc_service
from ..services import inbound
from ..services import notebooks as notebook_service
from ..services import source_grading
from ..services import storage
from ..services import storage_deletions
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
        "figures": nb.figures,
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
def delete_notebook(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    request: Request,
    background_tasks: BackgroundTasks,
):
    nb = _get_notebook(session, notebook_id)
    if nb.owner_id != user.id and user.role != Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can delete")
    demo_service.ensure_not_reset_managed(nb)
    report_ids = [report.id for report in nb.reports if report.id is not None]
    rendered = list(
        session.exec(
            select(RenderedProduct).where(col(RenderedProduct.report_id).in_(report_ids))
        ).all()
    ) if report_ids else []
    detail = {
        "reports": len(report_ids),
        "attachments": len(nb.attachments),
        "figures": len(nb.figures),
        "rendered_products": len(rendered),
    }
    for item in nb.attachments:
        storage_deletions.enqueue(session, item, "attachment")
    for item in nb.figures:
        storage_deletions.enqueue(session, item, "figure")
    for item in rendered:
        storage_deletions.enqueue(session, item, "render")
    event = audit.record(
        session,
        action=AuditAction.NOTEBOOK_DELETED,
        category=AuditCategory.DATA_ACCESS,
        actor=user,
        request=request,
        resource_type="notebook",
        resource_id=notebook_id,
        detail=detail,
        commit=False,
    )
    session.delete(nb)
    session.commit()
    storage_deletions.schedule_worker(background_tasks)
    audit.schedule_emit(session, event, background_tasks)


@router.post("/{notebook_id}/sources", status_code=status.HTTP_201_CREATED)
def add_source(
    notebook_id: int,
    body: SourceCreate,
    session: SessionDep,
    _w: Writer,
) -> Source:
    nb = _get_notebook(session, notebook_id)
    return notebook_service.add_source(
        session,
        nb,
        title=body.title,
        reference=body.reference,
        summary=body.summary,
        content_md=body.content_md,
        tlp=body.tlp,
        reliability=body.reliability,
        credibility=body.credibility,
        grading_rationale=body.grading_rationale,
    )


@router.patch("/{notebook_id}/sources/{source_id}")
def update_source(
    notebook_id: int,
    source_id: int,
    body: SourceUpdate,
    session: SessionDep,
    _w: Writer,
) -> Source:
    source = notebook_service.get_source_or_404(session, notebook_id, source_id)
    return notebook_service.update_source(
        session,
        source,
        title=body.title,
        reference=body.reference,
        summary=body.summary,
        content_md=body.content_md,
        tlp=body.tlp,
    )


@router.put("/{notebook_id}/sources/{source_id}/grade")
def update_source_grade(
    notebook_id: int,
    source_id: int,
    body: SourceGradeUpdate,
    session: SessionDep,
    _w: Writer,
) -> Source:
    source = notebook_service.get_source_or_404(session, notebook_id, source_id)
    source_grading.set_manual_grade(
        source,
        reliability=body.reliability,
        credibility=body.credibility,
        rationale=body.grading_rationale,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@router.post("/{notebook_id}/sources/{source_id}/auto-grade")
def auto_grade_source(
    notebook_id: int,
    source_id: int,
    session: SessionDep,
    _w: Writer,
) -> dict:
    source = notebook_service.get_source_or_404(session, notebook_id, source_id)
    outcome = source_grading.regrade_source(source)
    session.add(source)
    session.commit()
    session.refresh(source)
    return {"source": source, "applied": outcome.applied, "reason": outcome.reason}


@router.delete(
    "/{notebook_id}/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_source(
    notebook_id: int, source_id: int, session: SessionDep, _w: Writer
):
    source = notebook_service.get_source_or_404(session, notebook_id, source_id)
    session.delete(source)
    session.commit()


@router.post("/{notebook_id}/imports/taxii")
def import_taxii(
    notebook_id: int,
    body: TaxiiPullRequest,
    session: SessionDep,
    _w: Writer,
) -> dict[str, int]:
    return inbound.pull_taxii(session, _get_notebook(session, notebook_id), body.url)


@router.post("/{notebook_id}/imports/misp")
def import_misp(
    notebook_id: int,
    body: MispPullRequest,
    session: SessionDep,
    _w: Writer,
) -> dict[str, int]:
    return inbound.pull_misp(
        session, _get_notebook(session, notebook_id), body.event_uuid
    )


def _audit_file(
    session,
    background_tasks,
    request,
    user,
    action,
    *,
    notebook_id,
    item,
    commit=True,
):
    """Record a sensitive-file access (attachment / figure) audit event."""
    kwargs = dict(
        action=action,
        category=AuditCategory.DATA_ACCESS,
        actor=user,
        request=request,
        resource_type=item.__class__.__name__.lower(),
        resource_id=item.id,
        detail={
            "notebook_id": notebook_id,
            "filename": item.original_filename,
            "content_type": item.content_type,
        },
    )
    if commit:
        return audit.record_and_emit(
            session, background_tasks=background_tasks, **kwargs
        )
    return audit.record(session, commit=False, **kwargs)


@router.post("/{notebook_id}/attachments", status_code=status.HTTP_201_CREATED)
def add_attachment(
    notebook_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    summary: Annotated[str, Form()] = "",
) -> Attachment:
    nb = _get_notebook(session, notebook_id)
    att = attachment_service.save_upload(
        session, nb, file, title=title, summary=summary
    )
    _audit_file(
        session, background_tasks, request, user,
        AuditAction.ATTACHMENT_UPLOADED, notebook_id=notebook_id, item=att,
    )
    session.refresh(att)  # the audit commit expires the instance before serialisation
    return att


def _get_attachment(
    session: Session, notebook_id: int, attachment_id: int
) -> Attachment:
    att = session.get(Attachment, attachment_id)
    if not att or att.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return att


@router.get("/{notebook_id}/attachments/{attachment_id}/download")
def download_attachment(
    notebook_id: int,
    attachment_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
):
    att = _get_attachment(session, notebook_id, attachment_id)
    try:
        content = attachment_service.attachment_bytes(session, att)
    except storage.ObjectMissing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored file missing")
    except storage.StorageError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Stored file verification failed"
        )
    _audit_file(
        session, background_tasks, request, user,
        AuditAction.ATTACHMENT_DOWNLOADED, notebook_id=notebook_id, item=att,
    )
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                "attachment; filename*=utf-8''" + quote(att.original_filename, safe="")
            ),
        },
    )


@router.delete(
    "/{notebook_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_attachment(
    notebook_id: int,
    attachment_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
):
    att = _get_attachment(session, notebook_id, attachment_id)
    event = _audit_file(
        session, background_tasks, request, user,
        AuditAction.ATTACHMENT_DELETED, notebook_id=notebook_id, item=att,
        commit=False,
    )
    attachment_service.delete_attachment(
        session, att, background_tasks=background_tasks
    )
    audit.schedule_emit(session, event, background_tasks)


# --------------------------------------------------------------------------- #
# Figures (uploaded images embedded inline into reports via [[figure:ID]]).
# Writer-only collection material; the published report embeds the bytes as a
# data-URI, so report viewers never need this endpoint.
# --------------------------------------------------------------------------- #
@router.post("/{notebook_id}/figures", status_code=status.HTTP_201_CREATED)
def add_figure(
    notebook_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
) -> Figure:
    nb = _get_notebook(session, notebook_id)
    fig = figure_service.save_upload(session, nb, file, title=title)
    _audit_file(
        session, background_tasks, request, user,
        AuditAction.FIGURE_UPLOADED, notebook_id=notebook_id, item=fig,
    )
    session.refresh(fig)  # the audit commit expires the instance before serialisation
    return fig


def _get_figure(session: Session, notebook_id: int, figure_id: int) -> Figure:
    fig = session.get(Figure, figure_id)
    if not fig or fig.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Figure not found")
    return fig


@router.get("/{notebook_id}/figures/{figure_id}/raw")
def figure_raw(
    notebook_id: int, figure_id: int, session: SessionDep, _w: Writer
):
    """Serve a figure's bytes inline (for the notebook + editor thumbnails)."""
    fig = _get_figure(session, notebook_id, figure_id)
    try:
        content = figure_service.figure_bytes(session, fig)
    except storage.ObjectMissing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored file missing")
    except storage.StorageError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Stored file verification failed"
        )
    return Response(
        content,
        media_type=fig.content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                "inline; filename*=utf-8''" + quote(fig.original_filename, safe="")
            ),
        },
    )


@router.delete(
    "/{notebook_id}/figures/{figure_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_figure(
    notebook_id: int,
    figure_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
):
    fig = _get_figure(session, notebook_id, figure_id)
    event = _audit_file(
        session, background_tasks, request, user,
        AuditAction.FIGURE_DELETED, notebook_id=notebook_id, item=fig,
        commit=False,
    )
    figure_service.delete_figure(session, fig, background_tasks=background_tasks)
    audit.schedule_emit(session, event, background_tasks)


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


# --------------------------------------------------------------------------- #
# ACH (Analysis of Competing Hypotheses) matrices
# --------------------------------------------------------------------------- #
@router.post("/{notebook_id}/ach", status_code=status.HTTP_201_CREATED)
def add_ach(
    notebook_id: int, body: ACHCreate, session: SessionDep, _w: Writer
) -> ACHModel:
    nb = _get_notebook(session, notebook_id)
    return ach_service.create_ach(
        session,
        nb,
        title=body.title,
        question=body.question,
        hypotheses=[r.model_dump() for r in body.hypotheses],
        evidence=[r.model_dump() for r in body.evidence],
        ratings=body.ratings,
        notes=body.notes,
    )


@router.patch("/{notebook_id}/ach/{ach_id}")
def update_ach(
    notebook_id: int,
    ach_id: int,
    body: ACHUpdate,
    session: SessionDep,
    _w: Writer,
) -> ACHModel:
    ach = ach_service.get_scoped(session, notebook_id, ach_id)
    return ach_service.update_ach(
        session, ach, **body.model_dump(exclude_unset=True)
    )


@router.get("/{notebook_id}/ach/{ach_id}/matrix.svg")
def ach_matrix(notebook_id: int, ach_id: int, session: SessionDep, _w: Writer):
    ach = ach_service.get_scoped(session, notebook_id, ach_id)
    return Response(
        content=ach_service.render_ach_svg(ach),
        media_type="image/svg+xml",
    )


@router.delete(
    "/{notebook_id}/ach/{ach_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_ach(notebook_id: int, ach_id: int, session: SessionDep, _w: Writer):
    ach = ach_service.get_scoped(session, notebook_id, ach_id)
    ach_service.delete_ach(session, ach)


# --------------------------------------------------------------------------- #
# Indicators of compromise (light-touch IOC staging; pushed to MISP via a report)
# --------------------------------------------------------------------------- #
def _audit_ioc(session, background_tasks, request, user, action, *, ioc):
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=action,
        category=AuditCategory.DATA_ACCESS,
        actor=user,
        request=request,
        resource_type="ioc",
        resource_id=ioc.id,
        detail={"notebook_id": ioc.notebook_id, "ioc_type": str(ioc.ioc_type)},
    )


@router.get("/{notebook_id}/iocs")
def list_iocs(notebook_id: int, session: SessionDep, _w: Writer) -> list[IOC]:
    _get_notebook(session, notebook_id)
    return ioc_service.list_for_notebook(session, notebook_id)


@router.post("/{notebook_id}/iocs", status_code=status.HTTP_201_CREATED)
def add_ioc(
    notebook_id: int,
    body: IOCCreate,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
) -> IOC:
    nb = _get_notebook(session, notebook_id)
    ioc = ioc_service.create_ioc(
        session,
        nb,
        ioc_type=body.ioc_type,
        value=body.value,
        description=body.description,
        source_id=body.source_id,
        tlp=body.tlp,
    )
    _audit_ioc(
        session, background_tasks, request, user, AuditAction.IOC_CREATED, ioc=ioc
    )
    session.refresh(ioc)  # the audit commit expires the instance before serialisation
    return ioc


@router.patch("/{notebook_id}/iocs/{ioc_id}")
def update_ioc(
    notebook_id: int,
    ioc_id: int,
    body: IOCUpdate,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
) -> IOC:
    ioc = ioc_service.get_scoped(session, notebook_id, ioc_id)
    ioc = ioc_service.update_ioc(session, ioc, **body.model_dump(exclude_unset=True))
    _audit_ioc(
        session, background_tasks, request, user, AuditAction.IOC_UPDATED, ioc=ioc
    )
    session.refresh(ioc)
    return ioc


@router.delete(
    "/{notebook_id}/iocs/{ioc_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_ioc(
    notebook_id: int,
    ioc_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
):
    ioc = ioc_service.get_scoped(session, notebook_id, ioc_id)
    _audit_ioc(
        session, background_tasks, request, user, AuditAction.IOC_DELETED, ioc=ioc
    )
    ioc_service.delete_ioc(session, ioc)
