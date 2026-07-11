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
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    page = search_service.search_page(
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
        "count": len(page.results),
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "results": [
            {
                "report": report_summary(r),
                "tags": list(r.tags),
            }
            for r in page.results
        ],
    }
