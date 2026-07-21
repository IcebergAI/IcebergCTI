"""Admin-only Settings & integrations hub (read-only landing page).

The map on top of the deep config pages: one tile per admin-configurable
subsystem, its live status, and a way in. It owns no state and has no save path —
every status pill is derived from the same settings singletons the config pages
themselves edit (``services/effective_config.admin_hub_tiles``), so the hub can
never disagree with the page it links to.
"""

from fastapi import Request

from ..auth.dependencies import CurrentUser
from ..services import effective_config
from ..templating import templates
from .common import SessionDep, _require_admin, router

# Tile groups, in display order (a tile's ``group`` selects its section).
HUB_GROUPS: tuple[str, ...] = ("Outbound integrations", "Governance")


@router.get("/admin")
def admin_home_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    tiles = effective_config.admin_hub_tiles(session)
    return templates.TemplateResponse(
        request,
        "admin_home.html",
        {
            "user": user,
            "groups": [
                (group, [t for t in tiles if t["group"] == group])
                for group in HUB_GROUPS
            ],
        },
    )
