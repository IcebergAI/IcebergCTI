"""Diamond Model & ACH analytic-model portal routes."""

import json
from typing import Annotated

from fastapi import (
    Form,
    Request,
)

from ..auth.dependencies import CurrentUser
from ..models import (
    ACHCellRating,
    DiamondConfidence,
    IntelLevel,
    TLP,
)
from ..services import (
    ach as ach_service,
    diamond as diamond_service,
)
from ..services.reports import (
    create_report as create_report_record,
)
from ..templating import templates
from .common import (
    SessionDep,
    _get_notebook,
    _redirect,
    _require_writer,
    router,
)

@router.post("/notebooks/{notebook_id}/diamonds")
def add_diamond(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    adversary: Annotated[str, Form()] = "",
    capability: Annotated[str, Form()] = "",
    infrastructure: Annotated[str, Form()] = "",
    victim: Annotated[str, Form()] = "",
    confidence: Annotated[DiamondConfidence, Form()] = DiamondConfidence.MODERATE,
    notes: Annotated[str, Form()] = "",
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    diamond = diamond_service.create_diamond(
        session,
        nb,
        title=title,
        adversary=adversary,
        capability=capability,
        infrastructure=infrastructure,
        victim=victim,
        confidence=confidence,
        notes=notes,
    )
    return _redirect(f"/notebooks/{notebook_id}/diamonds/{diamond.id}/edit")


@router.get("/notebooks/{notebook_id}/diamonds/{diamond_id}/edit")
def diamond_edit(
    notebook_id: int,
    diamond_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    diamond = diamond_service.get_scoped(session, notebook_id, diamond_id)
    return templates.TemplateResponse(
        request,
        "diamond_edit.html",
        {
            "user": user,
            "notebook": nb,
            "diamond": diamond,
            "confidences": list(DiamondConfidence),
            "preview_svg": diamond_service.render_diamond_svg(diamond),
        },
    )


@router.post("/notebooks/{notebook_id}/diamonds/{diamond_id}")
def diamond_save(
    notebook_id: int,
    diamond_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    adversary: Annotated[str, Form()] = "",
    capability: Annotated[str, Form()] = "",
    infrastructure: Annotated[str, Form()] = "",
    victim: Annotated[str, Form()] = "",
    confidence: Annotated[DiamondConfidence, Form()] = DiamondConfidence.MODERATE,
    notes: Annotated[str, Form()] = "",
):
    _require_writer(user)
    diamond = diamond_service.get_scoped(session, notebook_id, diamond_id)
    diamond_service.update_diamond(
        session,
        diamond,
        title=title,
        adversary=adversary,
        capability=capability,
        infrastructure=infrastructure,
        victim=victim,
        confidence=confidence,
        notes=notes,
    )
    return _redirect(f"/notebooks/{notebook_id}/diamonds/{diamond_id}/edit")


@router.post("/notebooks/{notebook_id}/diamonds/{diamond_id}/delete")
def diamond_delete(
    notebook_id: int, diamond_id: int, session: SessionDep, user: CurrentUser
):
    _require_writer(user)
    diamond = diamond_service.get_scoped(session, notebook_id, diamond_id)
    diamond_service.delete_diamond(session, diamond)
    return _redirect(f"/notebooks/{notebook_id}#diamonds")


# --------------------------------------------------------------------------- #
# ACH (Analysis of Competing Hypotheses) matrices
# --------------------------------------------------------------------------- #
def _parse_ach_matrix(matrix: str) -> tuple[list, list, dict]:
    """Decode the editor's hidden ``matrix`` JSON field into the trio the
    service normalises. Bad/empty JSON degrades to an empty matrix."""
    try:
        data = json.loads(matrix or "{}")
    except (ValueError, TypeError):
        return [], [], {}
    if not isinstance(data, dict):
        return [], [], {}
    hyps = data.get("hypotheses") or []
    evs = data.get("evidence") or []
    ratings = data.get("ratings") or {}
    return (
        hyps if isinstance(hyps, list) else [],
        evs if isinstance(evs, list) else [],
        ratings if isinstance(ratings, dict) else {},
    )


@router.post("/notebooks/{notebook_id}/ach")
def add_ach(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    ach = ach_service.create_ach(session, nb, title=title)
    return _redirect(f"/notebooks/{notebook_id}/ach/{ach.id}/edit")


@router.get("/notebooks/{notebook_id}/ach/{ach_id}/edit")
def ach_edit(
    notebook_id: int,
    ach_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    ach = ach_service.get_scoped(session, notebook_id, ach_id)
    return templates.TemplateResponse(
        request,
        "ach_edit.html",
        {
            "user": user,
            "notebook": nb,
            "ach": ach,
            "ratings": list(ACHCellRating),
            "preview_svg": ach_service.render_ach_svg(ach),
        },
    )


@router.post("/notebooks/{notebook_id}/ach/{ach_id}")
def ach_save(
    notebook_id: int,
    ach_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    question: Annotated[str, Form()] = "",
    matrix: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    _require_writer(user)
    ach = ach_service.get_scoped(session, notebook_id, ach_id)
    hyps, evs, ratings = _parse_ach_matrix(matrix)
    ach_service.update_ach(
        session,
        ach,
        title=title,
        question=question,
        hypotheses=hyps,
        evidence=evs,
        ratings=ratings,
        notes=notes,
    )
    return _redirect(f"/notebooks/{notebook_id}/ach/{ach_id}/edit")


@router.post("/notebooks/{notebook_id}/ach/{ach_id}/delete")
def ach_delete(
    notebook_id: int, ach_id: int, session: SessionDep, user: CurrentUser
):
    _require_writer(user)
    ach = ach_service.get_scoped(session, notebook_id, ach_id)
    ach_service.delete_ach(session, ach)
    return _redirect(f"/notebooks/{notebook_id}#ach")


@router.post("/notebooks/{notebook_id}/reports")
def create_report(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    intel_level: Annotated[IntelLevel, Form()] = IntelLevel.OPERATIONAL,
    tlp: Annotated[TLP, Form()] = TLP.AMBER,
):
    _require_writer(user)
    report = create_report_record(
        session,
        notebook_id=notebook_id,
        title=title,
        author_id=user.id,
        intel_level=intel_level,
        tlp=tlp,
    )
    return _redirect(f"/reports/{report.id}/edit")


