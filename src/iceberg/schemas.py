"""Request bodies for the JSON API (responses serialise models/dicts directly)."""

from pydantic import BaseModel

from .models import (
    DiamondConfidence,
    IntelLevel,
    Priority,
    ProductFormat,
    ReportStatus,
    RequirementStatus,
    TagKind,
    TLP,
)


class NotebookCreate(BaseModel):
    title: str
    topic: str = ""


class NotebookUpdate(BaseModel):
    title: str | None = None
    topic: str | None = None


class SourceCreate(BaseModel):
    title: str
    reference: str = ""
    summary: str = ""


class NoteCreate(BaseModel):
    body_md: str = ""


class ReportCreate(BaseModel):
    notebook_id: int
    title: str
    body_md: str = ""
    intel_level: IntelLevel = IntelLevel.OPERATIONAL
    tlp: TLP = TLP.AMBER


class ReportUpdate(BaseModel):
    title: str | None = None
    body_md: str | None = None
    intel_level: IntelLevel | None = None
    tlp: TLP | None = None


class CitationsUpdate(BaseModel):
    source_ids: list[int]


class TransitionRequest(BaseModel):
    target: ReportStatus


class RenderRequest(BaseModel):
    format: ProductFormat


class PreviewRequest(BaseModel):
    markdown: str = ""
    # When set, the editor's live preview resolves `[[diamond:ID]]` tokens
    # against this report's notebook.
    report_id: int | None = None


class PreviewResponse(BaseModel):
    html: str


class DiamondCreate(BaseModel):
    title: str
    adversary: str = ""
    capability: str = ""
    infrastructure: str = ""
    victim: str = ""
    confidence: DiamondConfidence = DiamondConfidence.MODERATE
    notes: str = ""


class DiamondUpdate(BaseModel):
    title: str | None = None
    adversary: str | None = None
    capability: str | None = None
    infrastructure: str | None = None
    victim: str | None = None
    confidence: DiamondConfidence | None = None
    notes: str | None = None


class DiamondPreviewRequest(BaseModel):
    """Render an unsaved Diamond Model to SVG (the notebook edit-page preview)."""

    title: str = ""
    adversary: str = ""
    capability: str = ""
    infrastructure: str = ""
    victim: str = ""
    confidence: DiamondConfidence = DiamondConfidence.MODERATE


class DiamondPreviewResponse(BaseModel):
    svg: str


class RequirementCreate(BaseModel):
    title: str
    description: str = ""
    intel_level: IntelLevel = IntelLevel.STRATEGIC
    priority: Priority = Priority.MEDIUM


class RequirementUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    intel_level: IntelLevel | None = None
    priority: Priority | None = None


class RequirementStatusUpdate(BaseModel):
    status: RequirementStatus


class RequirementLinks(BaseModel):
    """Set the requirements a report/notebook is linked to."""

    requirement_ids: list[int]


class AttachmentLinks(BaseModel):
    """Set the notebook attachments a report cites."""

    attachment_ids: list[int]


class PreferencesUpdate(BaseModel):
    preferred_intel_level: IntelLevel | None = None


class TagCreate(BaseModel):
    kind: TagKind
    label: str
    external_id: str = ""
    description: str = ""


class TagUpdate(BaseModel):
    label: str | None = None
    external_id: str | None = None
    description: str | None = None
    active: bool | None = None


class TagLinks(BaseModel):
    """Set the taxonomy tags a report is classified with."""

    tag_ids: list[int]
