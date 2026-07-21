"""Server-rendered portal (Jinja2 + Tailwind + Alpine).

The portal is one ``APIRouter`` (``common.router``) populated by domain-grouped
route modules. Importing those modules here registers their routes on the shared
router via their ``@router`` decorators; ``web_router`` is the assembled result.
"""

from . import (  # noqa: F401 — imported for their @router route registrations
    admin_audit,
    admin_feeds,
    admin_misp,
    admin_oidc,
    admin_proxy,
    admin_webhook,
    analytics,
    discovery,
    feed,
    feeds,
    notebooks,
    reports,
    requirements,
)
from .common import router as web_router

__all__ = ["web_router"]
