"""Full-text + faceted report search (JSON)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..models import IntelLevel, ReportStatus, TagKind, TLP
from ..services import search as search_service
from ..services.reports import report_summary

router = APIRouter(tags=["search"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/search")
def search(
    session: SessionDep,
    user: CurrentUser,
    q: str | None = None,
    kind: Annotated[list[TagKind] | None, Query()] = None,
    tag: Annotated[list[int] | None, Query()] = None,
    intel_level: IntelLevel | None = None,
    tlp: TLP | None = None,
    status: ReportStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    results = search_service.search_reports(
        session,
        user=user,
        q=q,
        kinds=kind,
        tag_ids=tag,
        intel_level=intel_level,
        tlp=tlp,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "query": q or "",
        "count": len(results),
        "results": [
            {
                "report": report_summary(r),
                "tags": list(r.tags),
            }
            for r in results
        ],
    }
