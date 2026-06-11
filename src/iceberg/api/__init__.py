"""JSON API routers, mounted under /api."""

from fastapi import APIRouter

from .account import router as account_router
from .feed import router as feed_router
from .notebooks import router as notebooks_router
from .preview import router as preview_router
from .reports import router as reports_router
from .requirements import router as requirements_router
from .search import router as search_router
from .tags import router as tags_router

api_router = APIRouter()
api_router.include_router(notebooks_router)
api_router.include_router(reports_router)
api_router.include_router(requirements_router)
api_router.include_router(feed_router)
api_router.include_router(account_router)
api_router.include_router(preview_router)
api_router.include_router(tags_router)
api_router.include_router(search_router)
