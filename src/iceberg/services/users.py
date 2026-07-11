"""User provisioning helpers for OIDC and the local development bypass.

An email address is profile data, not an external identity key.  OIDC users are
therefore bound exclusively by their issuer/subject pair; a legacy development
account can only gain an OIDC binding through the explicit administrator helper
below.
"""

from sqlalchemy import func
from sqlmodel import Session, select

from ..models import Role, User


class OIDCIdentityError(ValueError):
    """An OIDC identity cannot safely be provisioned.

    ``reason`` is deliberately a short non-sensitive audit value.  Do not put
    IdP claims, email addresses, or subject identifiers in the exception text.
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


def _user_by_email(session: Session, email: str) -> User | None:
    """Find an email owner case-insensitively.

    Database uniqueness is historically case-sensitive on some supported
    backends, while an identity provider's email address should not be used to
    bypass a differently-cased local account.
    """

    return session.exec(
        select(User).where(func.lower(User.email) == email.casefold())
    ).first()


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
    issuer: str | None = None,
    department: str = "",
    job_title: str = "",
    company_name: str = "",
    office_location: str = "",
) -> User:
    """Create or update a user without ever using email to bind OIDC identity.

    OIDC callers must supply both immutable identity components.  A matching
    pair may update its profile and may take an unclaimed new email address.  A
    new pair whose email is already present — including on a subject-less dev
    row — is rejected before any user fields or token versions are changed.

    The local dev-login bypass may only create/update an unbound row.  It cannot
    overwrite an OIDC-bound (or pre-migration subject-only) account.
    """

    issuer = _normalise_identity_value(issuer)
    sub = _normalise_identity_value(sub)
    email = (email or "").strip()
    if not email:
        raise OIDCIdentityError("email is required")
    if bool(issuer) != bool(sub):
        raise OIDCIdentityError("issuer and subject must be supplied together")

    email_owner = _user_by_email(session, email)
    if issuer is not None and sub is not None:
        user = session.exec(
            select(User).where(User.issuer == issuer, User.sub == sub)
        ).first()
        if user is None:
            # Email is intentionally not a fallback lookup.  An administrator
            # must explicitly link a legacy/dev account after verifying it.
            if email_owner is not None:
                raise OIDCIdentityCollisionError("email is already bound")
            user = User(
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
            # A legitimate same-subject email change is fine only when no other
            # local account owns the requested address.
            if email_owner is not None and email_owner.id != user.id:
                raise OIDCIdentityCollisionError("email is already bound")
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
        # The test/dev bypass cannot mutate an account that has ever carried an
        # external identity.  This makes an OIDC migration explicit rather than
        # letting a local email login silently change a production account.
        user = email_owner
        if user is not None and (user.issuer is not None or user.sub is not None):
            raise OIDCIdentityCollisionError("email is already externally bound")
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
) -> User:
    """Explicitly bind a legacy account to a verified OIDC identity.

    This is intentionally separate from login provisioning and must be called
    by an administrator.  It supports a subject-less development account and a
    pre-migration subject-only account only when that existing subject matches.
    It never changes user profile fields or token state.
    """

    issuer = _normalise_identity_value(issuer)
    sub = _normalise_identity_value(sub)
    if issuer is None or sub is None:
        raise LegacyIdentityLinkError("issuer and subject are required")
    if user.issuer is not None:
        raise LegacyIdentityLinkError("user already has an issuer binding")
    if user.sub is not None and user.sub != sub:
        raise LegacyIdentityLinkError("user has a different legacy subject")

    existing = session.exec(
        select(User).where(User.issuer == issuer, User.sub == sub)
    ).first()
    if existing is not None and existing.id != user.id:
        raise LegacyIdentityLinkError("identity belongs to another account")

    user.issuer = issuer
    user.sub = sub
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
