"""Request bodies for the JSON API (responses serialise models/dicts directly)."""

from datetime import date

from pydantic import BaseModel, model_validator

from .models import (
    AnalyticConfidence,
    DiamondConfidence,
    IntelLevel,
    IOCType,
    Motivation,
    Priority,
    ProductFormat,
    ProductUsefulness,
    ReportStatus,
    RequirementKind,
    RequirementStatus,
    RfiSatisfaction,
    SourceCredibility,
    SourceReliability,
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
    reliability: SourceReliability | None = None
    credibility: SourceCredibility | None = None
    grading_rationale: str = ""

    @model_validator(mode="after")
    def _grade_pair(self) -> "SourceCreate":
        if bool(self.reliability) != bool(self.credibility):
            raise ValueError("Reliability and credibility must be set together")
        return self


class SourceUpdate(BaseModel):
    title: str | None = None
    reference: str | None = None
    summary: str | None = None


class SourceGradeUpdate(BaseModel):
    reliability: SourceReliability | None = None
    credibility: SourceCredibility | None = None
    grading_rationale: str = ""

    @model_validator(mode="after")
    def _grade_pair(self) -> "SourceGradeUpdate":
        if bool(self.reliability) != bool(self.credibility):
            raise ValueError("Reliability and credibility must be set together")
        return self


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
    key_judgements: str | None = None
    key_assumptions: str | None = None
    intelligence_gaps: str | None = None
    analytic_confidence: AnalyticConfidence | None = None
    intel_level: IntelLevel | None = None
    tlp: TLP | None = None


class CitationsUpdate(BaseModel):
    source_ids: list[int]


class IOCCreate(BaseModel):
    ioc_type: IOCType = IOCType.DOMAIN
    value: str
    description: str = ""
    source_id: int | None = None


class IOCUpdate(BaseModel):
    ioc_type: IOCType | None = None
    value: str | None = None
    description: str | None = None
    source_id: int | None = None


class IOCCitationsUpdate(BaseModel):
    """Set the notebook indicators a report cites (Indicators appendix + MISP)."""

    ioc_ids: list[int]


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


class ReportPreviewRequest(BaseModel):
    """Live-preview the whole finished product — body (diamonds resolved against
    the report's notebook) plus the ICD 203 judgement scaffolding — from the
    editor's unsaved field values."""

    report_id: int
    body_md: str = ""
    key_judgements: str = ""
    key_assumptions: str = ""
    intelligence_gaps: str = ""


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


class ACHRow(BaseModel):
    """One hypothesis or evidence row. ``id`` is the stable per-row id the editor
    manages; it is allocated server-side (``h1``/``e1`` …) when absent."""

    id: str | None = None
    text: str = ""


class ACHCreate(BaseModel):
    title: str
    question: str = ""
    hypotheses: list[ACHRow] = []
    evidence: list[ACHRow] = []
    ratings: dict[str, str] = {}  # {"h1:e1": "INCONSISTENT", ...}
    notes: str = ""


class ACHUpdate(BaseModel):
    title: str | None = None
    question: str | None = None
    hypotheses: list[ACHRow] | None = None
    evidence: list[ACHRow] | None = None
    ratings: dict[str, str] | None = None
    notes: str | None = None


class ACHPreviewRequest(BaseModel):
    """Render an unsaved ACH matrix to SVG (the notebook edit-page preview)."""

    title: str = ""
    question: str = ""
    hypotheses: list[ACHRow] = []
    evidence: list[ACHRow] = []
    ratings: dict[str, str] = {}


class ACHPreviewResponse(BaseModel):
    svg: str


class RequirementCreate(BaseModel):
    title: str
    description: str = ""
    intel_level: IntelLevel = IntelLevel.STRATEGIC
    priority: Priority = Priority.MEDIUM
    kind: RequirementKind = RequirementKind.RFI
    # PIR-only; ignored (blanked) by the service for GIR/RFI.
    decision_context: str = ""
    review_by: date | None = None


class RequirementUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    intel_level: IntelLevel | None = None
    priority: Priority | None = None
    kind: RequirementKind | None = None
    decision_context: str | None = None
    review_by: date | None = None


class RequirementStatusUpdate(BaseModel):
    status: RequirementStatus


class RequirementLinks(BaseModel):
    """Set the requirements a report/notebook is linked to."""

    requirement_ids: list[int]


class FeedbackSubmit(BaseModel):
    """A stakeholder's feedback on a disseminated product (backlog D)."""

    usefulness: ProductUsefulness
    requirement_id: int | None = None
    satisfaction: RfiSatisfaction | None = None
    comment: str = ""


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
    aliases: list[str] = []
    suspected_attribution: str = ""
    motivations: list[Motivation] = []
    first_seen: str = ""
    last_seen: str = ""


class TagUpdate(BaseModel):
    label: str | None = None
    external_id: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    suspected_attribution: str | None = None
    motivations: list[Motivation] | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    active: bool | None = None


class TagLinks(BaseModel):
    """Set the taxonomy tags a report is classified with."""

    tag_ids: list[int]
