"""User provisioning helpers for OIDC and the local development bypass.

An email address is profile data, not an external identity key. OIDC users are
bound exclusively by their ``(auth_provider, issuer, subject)`` triple — email is
**not** globally unique, so the same person may exist under two providers. A
legacy/dev account can only gain an OIDC binding through the explicit
administrator helper below. Provisioning is fail-closed: a subject that already
belongs to a *different* provider is refused (cross-provider spoof guard), and a
local dev login can never overwrite an externally-bound account.
"""

from sqlalchemy import func
from sqlmodel import Session, select

from ..models import Role, User


class OIDCIdentityError(ValueError):
    """An OIDC identity cannot safely be provisioned.

    ``reason`` is deliberately a short non-sensitive audit value. Do not put IdP
    claims, email addresses, or subject identifiers in the exception text.
    """

    reason = "invalid_identity"


class OIDCIdentityCollisionError(OIDCIdentityError):
    """The identity or email is already bound to another local account."""

    reason = "identity_collision"


class LegacyIdentityLinkError(OIDCIdentityError):
    """An administrator attempted an unsafe legacy identity link."""

    reason = "legacy_link_rejected"


def _normalise_identity_value(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _is_bound(user: User) -> bool:
    """Whether a row carries any external identity (OIDC or a legacy subject)."""
    return (
        user.auth_provider is not None
        or user.issuer is not None
        or user.sub is not None
    )


def _email_owners(session: Session, email: str) -> list[User]:
    """All rows owning an email, case-insensitively (email is not unique)."""
    return list(
        session.exec(
            select(User).where(func.lower(User.email) == email.casefold())
        ).all()
    )


def _email_changed(user: User, email: str) -> bool:
    """Whether ``email`` is a different address from the one the row holds.

    The collision guard runs only on an actual change: an unchanged address takes
    nothing from anyone, and checking it unconditionally would lock a legitimate
    user out of a pre-existing (externally created) duplicate-email state.
    """
    return (user.email or "").casefold() != email.casefold()


def _reject_unbound_email_owner(
    session: Session, email: str, *, exclude: User | None = None
) -> None:
    """Refuse an email an unbound legacy/dev row already owns.

    Cross-provider co-ownership is the designed case ("the same person under two
    IdPs"), so a *bound* co-owner is fine. An **unbound** owner is not: that row
    can only gain an OIDC binding through the explicit administrator helper, so
    letting a login take its address would silently shadow it. Enforced on both
    the creation and the update path — an IdP-side email change is self-service
    at most providers, which otherwise makes it a two-step bypass (#276).
    """

    for owner in _email_owners(session, email):
        if exclude is not None and owner.id == exclude.id:
            continue
        if not _is_bound(owner):
            raise OIDCIdentityCollisionError("email is bound to a local account")


def _refresh_profile(
    user: User,
    *,
    email: str,
    display_name: str,
    role: Role,
    department: str,
    job_title: str,
    company_name: str,
    office_location: str,
) -> None:
    """Copy trusted, non-identity profile claims after all collisions pass."""

    user.email = email
    user.display_name = display_name
    user.role = role
    user.department = department
    user.job_title = job_title
    user.company_name = company_name
    user.office_location = office_location


def upsert_user(
    session: Session,
    *,
    sub: str | None,
    email: str,
    display_name: str,
    role: Role,
    auth_provider: str | None = None,
    issuer: str | None = None,
    department: str = "",
    job_title: str = "",
    company_name: str = "",
    office_location: str = "",
) -> User:
    """Create or update a user keyed on ``(auth_provider, issuer, sub)``.

    OIDC callers must supply all three identity components together. A matching
    triple may update its profile, including a new email — but never to an address
    an unbound legacy/dev row owns. A new triple is refused if its
    ``(issuer, sub)`` already belongs to a *different* provider (spoof guard) or
    if the email is owned by an unbound legacy/dev row (which an administrator
    must explicitly link first).

    The local dev-login bypass (no identity components) may only create/update an
    unbound row; it can never overwrite an externally-bound account.
    """

    auth_provider = _normalise_identity_value(auth_provider)
    issuer = _normalise_identity_value(issuer)
    sub = _normalise_identity_value(sub)
    email = (email or "").strip()
    if not email:
        raise OIDCIdentityError("email is required")

    identity_parts = [auth_provider, issuer, sub]
    is_oidc = any(part is not None for part in identity_parts)
    if is_oidc and not all(part is not None for part in identity_parts):
        raise OIDCIdentityError("provider, issuer and subject must be supplied together")

    if is_oidc:
        user = session.exec(
            select(User).where(
                User.auth_provider == auth_provider,
                User.issuer == issuer,
                User.sub == sub,
            )
        ).first()
        if user is None:
            # Adopt a pre-multi-provider Entra identity: before multi-provider,
            # OIDC rows carried (issuer, sub) with no auth_provider. The migration
            # backfills these to "entra"; this covers any that predate/escape the
            # backfill so an existing Entra user is never locked out.
            legacy = session.exec(
                select(User).where(
                    User.auth_provider == None,  # noqa: E711 — SQL NULL, not `is`
                    User.issuer == issuer,
                    User.sub == sub,
                )
            ).first()
            if legacy is not None and auth_provider == "entra":
                if _email_changed(legacy, email):
                    _reject_unbound_email_owner(session, email, exclude=legacy)
                legacy.auth_provider = auth_provider
                user = legacy
                _refresh_profile(
                    user,
                    email=email,
                    display_name=display_name,
                    role=role,
                    department=department,
                    job_title=job_title,
                    company_name=company_name,
                    office_location=office_location,
                )
            else:
                # Spoof guard: the same (issuer, sub) must not already belong to a
                # different provider.
                clash = session.exec(
                    select(User).where(User.issuer == issuer, User.sub == sub)
                ).first()
                if clash is not None:
                    raise OIDCIdentityCollisionError("identity bound to another provider")
                _reject_unbound_email_owner(session, email)
                user = User(
                    auth_provider=auth_provider,
                    issuer=issuer,
                    sub=sub,
                    email=email,
                    display_name=display_name,
                    role=role,
                    department=department,
                    job_title=job_title,
                    company_name=company_name,
                    office_location=office_location,
                )
        else:
            # The triple is already ours, but the email may be new — an address
            # an unbound row owns is still off-limits (#276).
            if _email_changed(user, email):
                _reject_unbound_email_owner(session, email, exclude=user)
            _refresh_profile(
                user,
                email=email,
                display_name=display_name,
                role=role,
                department=department,
                job_title=job_title,
                company_name=company_name,
                office_location=office_location,
            )
    else:
        owners = _email_owners(session, email)
        # The dev bypass cannot mutate — or shadow — an externally-bound account.
        if any(_is_bound(owner) for owner in owners):
            raise OIDCIdentityCollisionError("email is externally bound")
        user = owners[0] if owners else None
        if user is None:
            user = User(
                email=email,
                display_name=display_name,
                role=role,
                department=department,
                job_title=job_title,
                company_name=company_name,
                office_location=office_location,
            )
        else:
            _refresh_profile(
                user,
                email=email,
                display_name=display_name,
                role=role,
                department=department,
                job_title=job_title,
                company_name=company_name,
                office_location=office_location,
            )

    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def link_legacy_oidc_identity(
    session: Session,
    *,
    user: User,
    issuer: str,
    sub: str,
    auth_provider: str = "entra",
) -> User:
    """Explicitly bind a legacy account to a verified OIDC identity.

    Intentionally separate from login provisioning; must be called by an
    administrator. Supports a subject-less dev account and a pre-migration
    subject-only account only when that existing subject matches. It never
    changes user profile fields or token state.
    """

    issuer = _normalise_identity_value(issuer)
    sub = _normalise_identity_value(sub)
    auth_provider = _normalise_identity_value(auth_provider)
    if issuer is None or sub is None or auth_provider is None:
        raise LegacyIdentityLinkError("provider, issuer and subject are required")
    if user.issuer is not None:
        raise LegacyIdentityLinkError("user already has an issuer binding")
    if user.sub is not None and user.sub != sub:
        raise LegacyIdentityLinkError("user has a different legacy subject")

    existing = session.exec(
        select(User).where(User.issuer == issuer, User.sub == sub)
    ).first()
    if existing is not None and existing.id != user.id:
        raise LegacyIdentityLinkError("identity belongs to another account")

    user.auth_provider = auth_provider
    user.issuer = issuer
    user.sub = sub
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
