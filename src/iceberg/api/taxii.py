"""Read-only TAXII 2.1-shaped API for published STIX report export."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlmodel import Session

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..services import taxii as taxii_service

router = APIRouter(prefix="/taxii2", tags=["taxii"])
SessionDep = Annotated[Session, Depends(get_session)]


def _taxii_response(payload: dict) -> JSONResponse:
    return JSONResponse(payload, media_type=taxii_service.TAXII_MEDIA_TYPE)


def _taxii_query(
    added_after: Annotated[
        str | None,
        Query(
            description=(
                "Return objects whose TAXII date_added is strictly after this "
                "ISO-8601 timestamp, for example 2026-06-01T00:00:00Z."
            ),
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Query(
            gt=0,
            le=taxii_service.MAX_LIMIT,
            description=(
                "Maximum number of records to return. Capped at "
                f"{taxii_service.MAX_LIMIT}; responses with more results include "
                "a next cursor."
            ),
        ),
    ] = None,
    next_token: Annotated[
        str | None,
        Query(
            alias="next",
            description="Opaque pagination cursor returned by the previous page.",
        ),
    ] = None,
    match_types: Annotated[
        list[str] | None,
        Query(
            alias="match[type]",
            description=(
                "Filter by STIX object type. May be repeated or comma-separated, "
                "for example report or threat-actor."
            ),
        ),
    ] = None,
    match_ids: Annotated[
        list[str] | None,
        Query(
            alias="match[id]",
            description=(
                "Filter by exact STIX object id. May be repeated or comma-separated."
            ),
        ),
    ] = None,
) -> taxii_service.TaxiiQuery:
    return taxii_service.build_query(
        added_after=added_after,
        limit=limit,
        next_token=next_token,
        match_types=match_types,
        match_ids=match_ids,
    )


TaxiiQueryDep = Annotated[taxii_service.TaxiiQuery, Depends(_taxii_query)]


@router.get("/")
def api_root(_user: CurrentUser):
    return _taxii_response(taxii_service.api_root())


@router.get("/collections/")
def collections(_user: CurrentUser):
    return _taxii_response(taxii_service.collections())


@router.get("/collections/{collection_id}/")
def collection(collection_id: str, _user: CurrentUser):
    return _taxii_response(taxii_service.collection(collection_id))


@router.get("/collections/{collection_id}/manifest/")
def manifest(
    collection_id: str,
    session: SessionDep,
    user: CurrentUser,
    query: TaxiiQueryDep,
):
    return _taxii_response(taxii_service.manifest(session, user, collection_id, query))


@router.get("/collections/{collection_id}/objects/")
def objects(
    collection_id: str,
    session: SessionDep,
    user: CurrentUser,
    query: TaxiiQueryDep,
):
    return _taxii_response(taxii_service.objects(session, user, collection_id, query))


@router.get("/collections/{collection_id}/objects/{object_id}/")
def object_by_id(
    collection_id: str, object_id: str, session: SessionDep, user: CurrentUser
):
    return _taxii_response(
        taxii_service.object_by_id(session, user, collection_id, object_id)
    )
