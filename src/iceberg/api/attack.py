"""MITRE ATT&CK Navigator layer export (backlog A).

Emits a schema-conformant Navigator ``.json`` layer for a single report or a
named-threat entity. Read-only GETs, access-scoped exactly like the rest of the
portal: a report layer goes through ``ensure_visible`` (a stakeholder requesting
an unpublished report's layer gets 404), and an entity layer aggregates only the
reports ``search_reports`` returns for the caller (published-only for
stakeholders). No new data — these derive from existing TECHNIQUE tags.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlmodel import Session

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..models import Report, Tag
from ..services import attack as attack_service
from ..services import search as search_service
from ..services import tags as tag_service
from ..services.reports import ensure_visible

router = APIRouter(prefix="/attack", tags=["attack"])

SessionDep = Annotated[Session, Depends(get_session)]


def _layer_response(layer: dict, filename: str) -> JSONResponse:
    return JSONResponse(
        layer,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/{report_id}/layer")
def report_layer(report_id: int, session: SessionDep, user: CurrentUser):
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    ensure_visible(report, user)
    return _layer_response(
        attack_service.report_layer(report), f"navigator-report-{report_id}.json"
    )


@router.get("/tags/{tag_id}/layer")
def entity_layer(tag_id: int, session: SessionDep, user: CurrentUser):
    tag = session.get(Tag, tag_id)
    # Aggregated layers are for named-threat entities (actor/malware/campaign);
    # other kinds don't have an actor-style technique profile.
    if not tag or tag.kind not in tag_service.ALIASABLE_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entity not found")
    reports = search_service.search_reports(session, user=user, tag_ids=[tag_id])
    return _layer_response(
        attack_service.entity_layer(tag, reports),
        f"navigator-{tag.slug or tag_id}.json",
    )
