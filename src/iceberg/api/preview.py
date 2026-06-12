"""Live preview endpoints used by the editors: markdown (report body, with
inline Diamond Model diagrams) and an unsaved Diamond Model diagram."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..models import DiamondModel, Report
from ..rendering.markdown import render_markdown
from ..schemas import (
    DiamondPreviewRequest,
    DiamondPreviewResponse,
    PreviewRequest,
    PreviewResponse,
)
from ..services import diamond as diamond_service

router = APIRouter(tags=["preview"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.post("/preview", response_model=PreviewResponse)
def preview(
    body: PreviewRequest, session: SessionDep, _user: CurrentUser
) -> PreviewResponse:
    if body.report_id is not None:
        report = session.get(Report, body.report_id)
        if report is not None:
            return PreviewResponse(
                html=diamond_service.preview_body_html(
                    session, report.notebook_id, body.markdown
                )
            )
    return PreviewResponse(html=render_markdown(body.markdown))


@router.post("/preview/diamond", response_model=DiamondPreviewResponse)
def preview_diamond(
    body: DiamondPreviewRequest, _user: CurrentUser
) -> DiamondPreviewResponse:
    transient = DiamondModel(
        notebook_id=0,
        title=body.title,
        adversary=body.adversary,
        capability=body.capability,
        infrastructure=body.infrastructure,
        victim=body.victim,
        confidence=body.confidence,
    )
    return DiamondPreviewResponse(svg=diamond_service.render_diamond_svg(transient))
