"""Minting and decoding the Iceberg app JWT.

After either OIDC (Entra) or the dev-login bypass authenticates a user, we mint
our own short-lived JWT. The API reads it from the Authorization header; the
portal stores it in a signed session cookie. This keeps the spec's "all
endpoints authenticated using JWT" uniform across both entry points.
"""

from datetime import timedelta

import jwt

from ..config import get_settings
from ..models import utcnow


def create_access_token(*, user_id: int, email: str, role: str, name: str) -> str:
    settings = get_settings()
    now = utcnow()
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "name": name,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(
        token, settings.secret_key, algorithms=[settings.jwt_algorithm]
    )
