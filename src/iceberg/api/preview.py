"""Live preview endpoints used by the editors: markdown (report body, with
inline Diamond Model diagrams) and an unsaved Diamond Model diagram."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..models import ACHModel, DiamondModel, Report
from ..rendering.markdown import render_markdown
from ..schemas import (
    ACHPreviewRequest,
    ACHPreviewResponse,
    DiamondPreviewRequest,
    DiamondPreviewResponse,
    PreviewRequest,
    PreviewResponse,
    ReportPreviewRequest,
)
from ..services import ach as ach_service
from ..services import diamond as diamond_service
from ..services import product_html as product_html_service

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
                html=product_html_service.preview_body_html(
                    session, report.notebook_id, body.markdown
                )
            )
    return PreviewResponse(html=render_markdown(body.markdown))


@router.post("/preview/product", response_model=PreviewResponse)
def preview_product(
    body: ReportPreviewRequest, session: SessionDep, _user: CurrentUser
) -> PreviewResponse:
    """Render the full finished product (Key Judgements + body + Key Assumptions
    + Intelligence Gaps) for the report editor's live preview."""
    report = session.get(Report, body.report_id)
    if report is None:
        # Unknown report — fall back to a notebook-less render (no diamond scope).
        return PreviewResponse(html=render_markdown(body.body_md))
    return PreviewResponse(
        html=product_html_service.preview_report_product_html(
            session,
            report.notebook_id,
            body_md=body.body_md,
            key_judgements=body.key_judgements,
            key_assumptions=body.key_assumptions,
            intelligence_gaps=body.intelligence_gaps,
        )
    )


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


@router.post("/preview/ach", response_model=ACHPreviewResponse)
def preview_ach(body: ACHPreviewRequest, _user: CurrentUser) -> ACHPreviewResponse:
    hyps, evs, ratings = ach_service.normalise(
        [r.model_dump() for r in body.hypotheses],
        [r.model_dump() for r in body.evidence],
        body.ratings,
    )
    transient = ACHModel(
        notebook_id=0,
        title=body.title,
        question=body.question,
        hypotheses=hyps,
        evidence=evs,
        ratings=ratings,
    )
    return ACHPreviewResponse(svg=ach_service.render_ach_svg(transient))
