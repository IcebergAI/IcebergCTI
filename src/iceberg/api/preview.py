"""Live markdown preview endpoint used by the report editor."""

from fastapi import APIRouter

from ..auth.dependencies import CurrentUser
from ..rendering.markdown import render_markdown
from ..schemas import PreviewRequest, PreviewResponse

router = APIRouter(tags=["preview"])


@router.post("/preview", response_model=PreviewResponse)
def preview(body: PreviewRequest, _user: CurrentUser) -> PreviewResponse:
    return PreviewResponse(html=render_markdown(body.markdown))
