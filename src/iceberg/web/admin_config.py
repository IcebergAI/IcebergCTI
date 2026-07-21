"""Admin-only effective-configuration console (read-only).

Shows the resolved runtime config — every operationally meaningful setting, where
its value came from (database / environment / built-in default), the prod-guard
validation state, and feature-capability tiles. There is **no** save path and no
model of its own: it introspects ``Settings`` + the DB settings rows via
``services/effective_config.py``. Secrets are surfaced only as a set/not-set pill.
"""

from fastapi import Request

from ..auth.dependencies import CurrentUser
from ..services import effective_config
from ..templating import templates
from .common import SessionDep, _require_admin, router


@router.get("/admin/config")
def admin_config_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    return templates.TemplateResponse(
        request,
        "admin_config.html",
        {"user": user, **effective_config.snapshot(session)},
    )
