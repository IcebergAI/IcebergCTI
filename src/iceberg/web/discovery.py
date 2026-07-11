"""Search, taxonomy & entity-discovery portal routes."""

from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Form,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser
from ..models import (
    AudienceGroup,
    AuditAction,
    AuditCategory,
    IntelLevel,
    Motivation,
    ReportStatus,
    Role,
    Tag,
    TagKind,
    TLP,
    User,
)
from ..services import (
    attack as attack_service,
    audience as audience_service,
    audit,
    maturity as maturity_service,
    search as search_service,
    tags as tag_service,
)
from ..templating import templates
from .common import (
    SessionDep,
    _redirect,
    _require_admin,
    _require_writer,
    router,
)

def _tags_by_kind(tags: list[Tag]) -> dict[TagKind, list[Tag]]:
    grouped: dict[TagKind, list[Tag]] = {k: [] for k in TagKind}
    for t in tags:
        grouped[t.kind].append(t)
    return {k: v for k, v in grouped.items() if v}


def _opt_enum(value: str, enum_cls, field: str):
    """Coerce an HTML <select> value into an optional enum. The search facet form
    always submits its filter selects (an empty ``Any`` option becomes ``?intel_level=``),
    so treat a blank value as "no filter" rather than letting enum validation 422."""
    if not value:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid {field}: {value!r}")


@router.get("/search")
def search_view(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    q: str = "",
    kind: Annotated[list[TagKind], Query()] = [],
    tag: Annotated[list[int], Query()] = [],
    intel_level: Annotated[str, Query()] = "",
    tlp: Annotated[str, Query()] = "",
    status: Annotated[str, Query()] = "",
):
    intel_level = _opt_enum(intel_level, IntelLevel, "intel_level")
    tlp = _opt_enum(tlp, TLP, "tlp")
    status_filter = _opt_enum(status, ReportStatus, "status")
    results = search_service.search_reports(
        session,
        user=user,
        q=q or None,
        kinds=kind or None,
        tag_ids=tag or None,
        intel_level=intel_level,
        tlp=tlp,
        status=status_filter,
    )
    items = [{"report": r, "tags": list(r.tags)} for r in results]
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "user": user,
            "q": q,
            "items": items,
            "facet_tags": _tags_by_kind(tag_service.list_tags(session)),
            "selected_tags": set(tag),
            "selected_kinds": set(kind),
            "intel_level": intel_level,
            "tlp": tlp,
            "status": status_filter,
            "active_tag": None,
        },
    )


@router.get("/matrix")
def matrix_view(request: Request, session: SessionDep, user: CurrentUser):
    """ATT&CK technique-coverage heatmap aggregated across all *visible* reports
    (published-only for stakeholders, via ``search_reports``). Grouped into ATT&CK
    tactic columns; empty state handled in the template."""
    reports = search_service.search_reports(session, user=user, limit=10_000)
    return templates.TemplateResponse(
        request,
        "matrix.html",
        {
            "user": user,
            "matrix": attack_service.coverage_matrix(reports),
            "report_count": len(reports),
        },
    )


@router.get("/maturity")
def maturity_view(request: Request, session: SessionDep, user: CurrentUser):
    """Writer-only CTI program maturity & effectiveness dashboard (backlog H):
    pure aggregation over existing data + an indicative CTI-CMM rollup."""
    _require_writer(user)
    return templates.TemplateResponse(
        request,
        "maturity.html",
        {"user": user, "m": maturity_service.program_maturity(session)},
    )


@router.get("/tags")
def entities_list(request: Request, session: SessionDep, user: CurrentUser):
    """Browse index over named-threat entities (ACTOR/MALWARE/CAMPAIGN), grouped
    by kind and linking to the per-entity profile at /tags/{id}."""
    named = [
        t
        for t in tag_service.list_tags(session, include_inactive=True)
        if t.kind in tag_service.ALIASABLE_KINDS
    ]
    return templates.TemplateResponse(
        request,
        "entities_list.html",
        {"user": user, "entities_by_kind": _tags_by_kind(named)},
    )


@router.get("/tags/{tag_id}")
def tag_detail(
    tag_id: int, request: Request, session: SessionDep, user: CurrentUser
):
    tag = session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    results = search_service.search_reports(session, user=user, tag_ids=[tag_id])
    items = [{"report": r, "tags": list(r.tags)} for r in results]
    # Named-threat kinds (ACTOR/MALWARE/CAMPAIGN) get a proper entity profile page
    # with structured attribution; other kinds keep the plain search drill-down.
    if tag.kind in tag_service.ALIASABLE_KINDS:
        return templates.TemplateResponse(
            request,
            "entity_profile.html",
            {
                "user": user,
                "active_tag": tag,
                "items": items,
                "matrix": attack_service.coverage_matrix(results),
            },
        )
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "user": user,
            "q": "",
            "items": items,
            "facet_tags": _tags_by_kind(tag_service.list_tags(session)),
            "selected_tags": {tag_id},
            "selected_kinds": set(),
            "intel_level": None,
            "tlp": None,
            "status": None,
            "active_tag": tag,
        },
    )


@router.get("/admin/tags")
def admin_tags_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    return templates.TemplateResponse(
        request,
        "admin_tags.html",
        {
            "user": user,
            "tags_by_kind": _tags_by_kind(
                tag_service.list_tags(session, include_inactive=True)
            ),
            "kinds": list(TagKind),
            "motivations": list(Motivation),
        },
    )


@router.get("/admin/audience")
def admin_audience_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    groups = list(session.exec(select(AudienceGroup).order_by(AudienceGroup.name)).all())
    stakeholders = list(
        session.exec(select(User).where(User.role == Role.STAKEHOLDER).order_by(User.display_name)).all()
    )
    return templates.TemplateResponse(
        request,
        "admin_audience.html",
        {"user": user, "groups": groups, "stakeholders": stakeholders},
    )


@router.post("/admin/audience")
def admin_audience_create(
    session: SessionDep,
    user: CurrentUser,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    member_user_ids: Annotated[list[int], Form()] = [],
):
    _require_admin(user)
    slug = tag_service.slugify(name)
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name is required")
    if session.exec(select(AudienceGroup).where(AudienceGroup.slug == slug)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Audience group already exists")
    group = AudienceGroup(name=name.strip(), slug=slug, description=description.strip())
    group.members = [
        stakeholder
        for uid in member_user_ids
        if (stakeholder := session.get(User, uid)) is not None and stakeholder.role == Role.STAKEHOLDER
    ]
    session.add(group)
    session.commit()
    return _redirect("/admin/audience")


def _audience_group_or_404(session: Session, group_id: int) -> AudienceGroup:
    group = session.get(AudienceGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audience group not found")
    return group


@router.post("/admin/audience/{group_id}")
def admin_audience_update(
    group_id: int,
    session: SessionDep,
    user: CurrentUser,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    member_user_ids: Annotated[list[int], Form()] = [],
):
    _require_admin(user)
    group = _audience_group_or_404(session, group_id)
    slug = tag_service.slugify(name)
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name is required")
    existing = session.exec(select(AudienceGroup).where(AudienceGroup.slug == slug)).first()
    if existing is not None and existing.id != group.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Audience group already exists")
    group.name = name.strip()
    group.slug = slug
    group.description = description.strip()
    group.members = [
        stakeholder
        for uid in member_user_ids
        if (stakeholder := session.get(User, uid)) is not None and stakeholder.role == Role.STAKEHOLDER
    ]
    session.add(group)
    session.commit()
    return _redirect("/admin/audience")


@router.post("/admin/audience/{group_id}/delete")
def admin_audience_delete(group_id: int, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    group = _audience_group_or_404(session, group_id)
    audience_service.delete_group(session, group)
    return _redirect("/admin/audience")


def _audit_tag(session, background_tasks, request, user, action, tag):
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=action,
        category=AuditCategory.ADMIN,
        actor=user,
        request=request,
        resource_type="tag",
        resource_id=tag.id,
        detail={"kind": str(tag.kind), "label": tag.label, "active": tag.active},
    )


@router.post("/admin/tags")
def admin_tag_create(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    kind: Annotated[TagKind, Form()],
    label: Annotated[str, Form()],
    external_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    aliases: Annotated[str, Form()] = "",
    suspected_attribution: Annotated[str, Form()] = "",
    motivations: Annotated[list[str], Form()] = [],  # noqa: B006 (FastAPI Form list)
    first_seen: Annotated[str, Form()] = "",
    last_seen: Annotated[str, Form()] = "",
    attack_tactics: Annotated[str, Form()] = "",
):
    _require_admin(user)
    tag = tag_service.create_tag(
        session,
        kind=kind,
        label=label,
        external_id=external_id,
        description=description,
        aliases=tag_service.parse_aliases(aliases),
        suspected_attribution=suspected_attribution,
        motivations=motivations,
        first_seen=first_seen,
        last_seen=last_seen,
        attack_tactics=tag_service.parse_attack_tactics(attack_tactics),
    )
    _audit_tag(session, background_tasks, request, user, AuditAction.TAG_CREATED, tag)
    return _redirect("/admin/tags")


def _get_tag(session: Session, tag_id: int) -> Tag:
    tag = session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    return tag


@router.post("/admin/tags/{tag_id}")
def admin_tag_update(
    tag_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    label: Annotated[str, Form()] = "",
    external_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    aliases: Annotated[str, Form()] = "",
    suspected_attribution: Annotated[str, Form()] = "",
    motivations: Annotated[list[str], Form()] = [],  # noqa: B006 (FastAPI Form list)
    first_seen: Annotated[str, Form()] = "",
    last_seen: Annotated[str, Form()] = "",
    attack_tactics: Annotated[str, Form()] = "",
    active: Annotated[bool, Form()] = False,
):
    _require_admin(user)
    tag = _get_tag(session, tag_id)
    tag = tag_service.update_tag(
        session,
        tag,
        label=label or None,
        external_id=external_id,
        description=description,
        aliases=tag_service.parse_aliases(aliases),
        suspected_attribution=suspected_attribution,
        motivations=motivations,
        first_seen=first_seen,
        last_seen=last_seen,
        attack_tactics=tag_service.parse_attack_tactics(attack_tactics),
        active=active,
    )
    _audit_tag(session, background_tasks, request, user, AuditAction.TAG_UPDATED, tag)
    return _redirect("/admin/tags")


@router.post("/admin/tags/{tag_id}/delete")
def admin_tag_delete(
    tag_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    _require_admin(user)
    tag = _get_tag(session, tag_id)
    detail = {"kind": str(tag.kind), "label": tag.label, "active": tag.active}
    tag_service.delete_tag(session, tag)
    # A rejected delete (for example a merge-lineage tag) must not be recorded
    # as a successful TAG_DELETED event.  Keep the safe metadata before the ORM
    # row is removed so the successful audit still has useful context.
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.TAG_DELETED,
        category=AuditCategory.ADMIN,
        actor=user,
        request=request,
        resource_type="tag",
        resource_id=tag_id,
        detail=detail,
    )
    return _redirect("/admin/tags")
