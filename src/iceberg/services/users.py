"""User provisioning helpers (used by both OIDC callback and dev login)."""

from sqlmodel import Session, select

from ..models import Role, User


def upsert_user(
    session: Session,
    *,
    sub: str | None,
    email: str,
    display_name: str,
    role: Role,
) -> User:
    """Find a user by OIDC subject or email, creating/updating as needed.

    Role is refreshed from the identity provider on every login so that IdP
    group/app-role changes take effect. Matching prefers ``sub`` and falls back
    to ``email`` (e.g. a dev-login user later signing in via Entra).
    """

    user: User | None = None
    if sub:
        user = session.exec(select(User).where(User.sub == sub)).first()
    if user is None:
        user = session.exec(select(User).where(User.email == email)).first()

    if user is None:
        user = User(sub=sub, email=email, display_name=display_name, role=role)
        session.add(user)
    else:
        if sub and not user.sub:
            user.sub = sub
        user.display_name = display_name
        user.role = role
        session.add(user)

    session.commit()
    session.refresh(user)
    return user
