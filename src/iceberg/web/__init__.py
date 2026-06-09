"""Server-rendered portal (Jinja2 + Tailwind + Alpine)."""

from .routes import router as web_router

__all__ = ["web_router"]
