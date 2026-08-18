"""Controlled-taxonomy tags: listing for everyone, curation for admins only."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import Session

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import AuditAction, AuditCategory, Role, Tag, TagKind, User
from ..schemas import (
    TagCreate,
    TagMergeRequest,
    TagRenameRequest,
    TagRenameUndoRequest,
    TagUpdate,
)
from ..services import audit
from ..services import tags as tag_service

router = APIRouter(prefix="/tags", tags=["tags"])

SessionDep = Annotated[Session, Depends(get_session)]
# Curation is admin-only; the taxonomy is a controlled vocabulary.
Admin = Annotated[User, Depends(require_role(Role.ADMIN))]


def _audit_tag(session, background_tasks, request, admin, action, tag):
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=action,
        category=AuditCategory.ADMIN,
        actor=admin,
        request=request,
        resource_type="tag",
        resource_id=tag.id,
        detail={"kind": str(tag.kind), "label": tag.label, "active": tag.active},
    )


def _audit_tag_detail(tag: Tag) -> dict:
    """Capture safe tag metadata before a hard delete expires its ORM row."""
    return {"kind": str(tag.kind), "label": tag.label, "active": tag.active}


def _get_tag(session: Session, tag_id: int) -> Tag:
    tag = session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    return tag


@router.get("")
def list_tags(
    session: SessionDep,
    _user: CurrentUser,
    kind: TagKind | None = None,
    q: str | None = None,
    include_inactive: bool = False,
) -> list[Tag]:
    return tag_service.list_tags(
        session, kind=kind, q=q, include_inactive=include_inactive
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_tag(
    body: TagCreate,
    session: SessionDep,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Tag:
    tag = tag_service.create_tag(
        session,
        kind=body.kind,
        label=body.label,
        external_id=body.external_id,
        description=body.description,
        aliases=body.aliases,
        suspected_attribution=body.suspected_attribution,
        motivations=body.motivations,
        first_seen=body.first_seen,
        last_seen=body.last_seen,
        attack_tactics=body.attack_tactics,
    )
    _audit_tag(session, background_tasks, request, admin, AuditAction.TAG_CREATED, tag)
    session.refresh(tag)  # the audit commit expires the instance before serialisation
    return tag


@router.patch("/{tag_id}")
def update_tag(
    tag_id: int,
    body: TagUpdate,
    session: SessionDep,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Tag:
    tag = _get_tag(session, tag_id)
    tag = tag_service.update_tag(
        session,
        tag,
        label=body.label,
        external_id=body.external_id,
        description=body.description,
        aliases=body.aliases,
        suspected_attribution=body.suspected_attribution,
        motivations=body.motivations,
        first_seen=body.first_seen,
        last_seen=body.last_seen,
        attack_tactics=body.attack_tactics,
        active=body.active,
    )
    _audit_tag(session, background_tasks, request, admin, AuditAction.TAG_UPDATED, tag)
    session.refresh(tag)  # the audit commit expires the instance before serialisation
    return tag


def _impact_json(impact) -> dict:
    return {
        "tag_id": impact.tag.id,
        "current_label": impact.tag.label,
        "new_label": impact.new_label,
        "new_slug": impact.new_slug,
        "conflicts": impact.conflicts,
        "blocked": impact.blocked,
        "unchanged": impact.unchanged,
        "alias_added": impact.alias_added,
        "draft_reports": impact.draft_reports,
        "published_reports": impact.published_reports,
        "frozen_snapshots": impact.frozen_snapshots,
        "subscriptions": impact.subscriptions,
        "policy_rules": impact.policy_rules,
        "records_to_rewrite": impact.records_to_rewrite,
    }


@router.post("/{tag_id}/rename/preview")
def preview_rename(
    tag_id: int, body: TagRenameRequest, session: SessionDep, _a: Admin
) -> dict:
    """Dry-run a rename: what references the term, and what would block it."""

    return _impact_json(
        tag_service.rename_impact(session, _get_tag(session, tag_id), body.new_label)
    )


@router.post("/{tag_id}/rename")
def rename_tag(
    tag_id: int,
    body: TagRenameRequest,
    session: SessionDep,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Rename a term. Its previous name stays resolvable as an audited alias."""

    tag = _get_tag(session, tag_id)
    previous = tag.label
    impact = tag_service.rename_tag(session, tag, body.new_label)
    if not impact.unchanged:
        audit.record_and_emit(
            session,
            background_tasks=background_tasks,
            action=AuditAction.TAG_RENAMED,
            category=AuditCategory.ADMIN,
            actor=admin,
            request=request,
            resource_type="tag",
            resource_id=tag.id,
            detail={
                "previous_label": previous,
                "new_label": impact.new_label,
                "alias_added": previous,
                "published_reports": impact.published_reports,
                "frozen_snapshots": impact.frozen_snapshots,
                "subscriptions": impact.subscriptions,
            },
        )
        session.refresh(tag)
    return _impact_json(impact)


@router.post("/{tag_id}/rename/undo")
def undo_rename(
    tag_id: int,
    body: TagRenameUndoRequest,
    session: SessionDep,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Tag:
    """Reverse a rename that has not yet been relied on."""

    tag = _get_tag(session, tag_id)
    superseded = tag.label
    tag = tag_service.undo_rename(session, tag, body.previous_label)
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.TAG_RENAME_REVERTED,
        category=AuditCategory.ADMIN,
        actor=admin,
        request=request,
        resource_type="tag",
        resource_id=tag.id,
        detail={"reverted_from": superseded, "restored_label": tag.label},
    )
    session.refresh(tag)
    return tag


@router.post("/{source_tag_id}/merge")
def merge_tag(
    source_tag_id: int,
    body: TagMergeRequest,
    session: SessionDep,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Consolidate a duplicate taxonomy term into an active canonical term.

    Both report classifications and stakeholder subscriptions are moved in one
    transaction.  The source is retired and retained as lineage instead of
    deleted, while its label/aliases become aliases on the target.
    """
    source = _get_tag(session, source_tag_id)
    target = _get_tag(session, body.target_tag_id)
    result = tag_service.merge_tags(session, source=source, target=target)
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.TAG_MERGED,
        category=AuditCategory.ADMIN,
        actor=admin,
        request=request,
        resource_type="tag",
        resource_id=result.source.id,
        detail={
            "source_tag_id": result.source.id,
            "source_label": result.source.label,
            "target_tag_id": result.target.id,
            "target_label": result.target.label,
            "report_links_moved": result.report_links_moved,
            "report_links_deduplicated": result.report_links_deduplicated,
            "subscriptions_moved": result.subscriptions_moved,
            "subscriptions_deduplicated": result.subscriptions_deduplicated,
        },
    )
    # The audit commit expires the instances before FastAPI serialises them.
    session.refresh(result.source)
    session.refresh(result.target)
    return {
        "source": result.source,
        "target": result.target,
        "report_links_moved": result.report_links_moved,
        "report_links_deduplicated": result.report_links_deduplicated,
        "subscriptions_moved": result.subscriptions_moved,
        "subscriptions_deduplicated": result.subscriptions_deduplicated,
    }


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag(
    tag_id: int,
    session: SessionDep,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
):
    tag = _get_tag(session, tag_id)
    detail = _audit_tag_detail(tag)
    tag_service.delete_tag(session, tag)
    # Validate/delete first: a refused delete must not leave a false-positive
    # TAG_DELETED audit event.  ``detail`` was captured while the ORM row lived.
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.TAG_DELETED,
        category=AuditCategory.ADMIN,
        actor=admin,
        request=request,
        resource_type="tag",
        resource_id=tag_id,
        detail=detail,
    )
