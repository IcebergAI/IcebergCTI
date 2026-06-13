"""SQLModel domain models and enums for Iceberg.

Kept in a single module so cross-model relationships resolve without circular
imports. User/Notebook/Source/Note/Report (+ link tables) form the authoring
core; Requirement drives stakeholder intake and the analyst tasking board.
"""

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Role(StrEnum):
    ADMIN = "ADMIN"
    ANALYST = "ANALYST"
    REVIEWER = "REVIEWER"
    STAKEHOLDER = "STAKEHOLDER"


class IntelLevel(StrEnum):
    STRATEGIC = "STRATEGIC"
    TACTICAL = "TACTICAL"
    OPERATIONAL = "OPERATIONAL"


class TLP(StrEnum):
    """TLP 2.0 markings. Stored by name; display label via :func:`tlp_label`."""

    RED = "RED"
    AMBER_STRICT = "AMBER_STRICT"
    AMBER = "AMBER"
    GREEN = "GREEN"
    CLEAR = "CLEAR"


_TLP_LABELS = {
    TLP.RED: "TLP:RED",
    TLP.AMBER_STRICT: "TLP:AMBER+STRICT",
    TLP.AMBER: "TLP:AMBER",
    TLP.GREEN: "TLP:GREEN",
    TLP.CLEAR: "TLP:CLEAR",
}


def tlp_label(tlp: TLP) -> str:
    return _TLP_LABELS[TLP(tlp)]


# Restrictiveness ordering (higher = more sensitive) used for dissemination
# routing: a report is auto-disseminated only when it is at or below the
# configured maximum TLP.
_TLP_RESTRICTIVENESS = {
    TLP.RED: 4,
    TLP.AMBER_STRICT: 3,
    TLP.AMBER: 2,
    TLP.GREEN: 1,
    TLP.CLEAR: 0,
}


def tlp_rank(tlp: TLP) -> int:
    return _TLP_RESTRICTIVENESS[TLP(tlp)]


def is_disseminable(report_tlp: TLP, max_tlp: TLP) -> bool:
    """True if a report's TLP is no more restrictive than the broadcast ceiling."""
    return tlp_rank(report_tlp) <= tlp_rank(max_tlp)


class ReportStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"


class ProductFormat(StrEnum):
    FULL = "FULL"
    EXEC_BRIEF = "EXEC_BRIEF"
    ONE_PAGER = "ONE_PAGER"


class Priority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RequirementStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    SATISFIED = "SATISFIED"
    CLOSED = "CLOSED"


class TagKind(StrEnum):
    """Facets of the controlled CTI taxonomy. Threat actors, campaigns and
    malware are org-curated; TECHNIQUE carries a MITRE ATT&CK id in
    ``external_id``; SECTOR/TOPIC are controlled vocabularies."""

    ACTOR = "ACTOR"
    CAMPAIGN = "CAMPAIGN"
    MALWARE = "MALWARE"
    TECHNIQUE = "TECHNIQUE"
    SECTOR = "SECTOR"
    TOPIC = "TOPIC"


class DiamondConfidence(StrEnum):
    """Analytic confidence in a Diamond Model assessment."""

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class SourceReliability(StrEnum):
    """Admiralty/NATO source reliability rating."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"


class SourceCredibility(StrEnum):
    """Admiralty/NATO information credibility rating."""

    CONFIRMED = "1"
    PROBABLY_TRUE = "2"
    POSSIBLY_TRUE = "3"
    DOUBTFULLY_TRUE = "4"
    IMPROBABLE = "5"
    CANNOT_BE_JUDGED = "6"


class SourceGradingOrigin(StrEnum):
    UNGRADED = "UNGRADED"
    AUTO = "AUTO"
    MANUAL = "MANUAL"


_SOURCE_RELIABILITY_LABELS = {
    SourceReliability.A: "Completely reliable",
    SourceReliability.B: "Usually reliable",
    SourceReliability.C: "Fairly reliable",
    SourceReliability.D: "Not usually reliable",
    SourceReliability.E: "Unreliable",
    SourceReliability.F: "Cannot be judged",
}

_SOURCE_CREDIBILITY_LABELS = {
    SourceCredibility.CONFIRMED: "Confirmed",
    SourceCredibility.PROBABLY_TRUE: "Probably true",
    SourceCredibility.POSSIBLY_TRUE: "Possibly true",
    SourceCredibility.DOUBTFULLY_TRUE: "Doubtfully true",
    SourceCredibility.IMPROBABLE: "Improbable",
    SourceCredibility.CANNOT_BE_JUDGED: "Cannot be judged",
}


def source_reliability_label(reliability: SourceReliability) -> str:
    return _SOURCE_RELIABILITY_LABELS[SourceReliability(reliability)]


def source_credibility_label(credibility: SourceCredibility) -> str:
    return _SOURCE_CREDIBILITY_LABELS[SourceCredibility(credibility)]


def source_grade_label(
    reliability: SourceReliability | None, credibility: SourceCredibility | None
) -> str:
    if not reliability or not credibility:
        return "Ungraded"
    return f"{SourceReliability(reliability).value}{SourceCredibility(credibility).value}"


# --------------------------------------------------------------------------- #
# Link tables
# --------------------------------------------------------------------------- #
class ReportSource(SQLModel, table=True):
    """Sources from a notebook that a report explicitly cites."""

    report_id: int | None = Field(
        default=None, foreign_key="report.id", ondelete="CASCADE", primary_key=True
    )
    source_id: int | None = Field(
        default=None, foreign_key="source.id", ondelete="CASCADE", primary_key=True
    )


class NotebookRequirement(SQLModel, table=True):
    """Traceability: a notebook addresses a stakeholder requirement."""

    notebook_id: int | None = Field(
        default=None, foreign_key="notebook.id", ondelete="CASCADE", primary_key=True
    )
    requirement_id: int | None = Field(
        default=None,
        foreign_key="requirement.id",
        ondelete="CASCADE",
        primary_key=True,
    )


class ReportRequirement(SQLModel, table=True):
    """Traceability: a report satisfies a stakeholder requirement."""

    report_id: int | None = Field(
        default=None, foreign_key="report.id", ondelete="CASCADE", primary_key=True
    )
    requirement_id: int | None = Field(
        default=None,
        foreign_key="requirement.id",
        ondelete="CASCADE",
        primary_key=True,
    )


class ReportAttachment(SQLModel, table=True):
    """Attachments from a notebook that a report explicitly cites."""

    report_id: int | None = Field(
        default=None, foreign_key="report.id", ondelete="CASCADE", primary_key=True
    )
    attachment_id: int | None = Field(
        default=None,
        foreign_key="attachment.id",
        ondelete="CASCADE",
        primary_key=True,
    )


class ReportTag(SQLModel, table=True):
    """Taxonomy: a report is classified with a tag from the controlled vocabulary."""

    report_id: int | None = Field(
        default=None, foreign_key="report.id", ondelete="CASCADE", primary_key=True
    )
    tag_id: int | None = Field(
        default=None, foreign_key="tag.id", ondelete="CASCADE", primary_key=True
    )


# --------------------------------------------------------------------------- #
# Core tables
# --------------------------------------------------------------------------- #
class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    sub: str | None = Field(default=None, index=True, unique=True)
    email: str = Field(index=True, unique=True)
    display_name: str
    role: Role = Field(default=Role.ANALYST)
    preferred_intel_level: IntelLevel | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)


class Notebook(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    topic: str = ""
    owner_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    sources: list["Source"] = Relationship(
        back_populates="notebook", cascade_delete=True
    )
    notes: list["Note"] = Relationship(
        back_populates="notebook", cascade_delete=True
    )
    attachments: list["Attachment"] = Relationship(
        back_populates="notebook", cascade_delete=True
    )
    diamond_models: list["DiamondModel"] = Relationship(
        back_populates="notebook", cascade_delete=True
    )
    reports: list["Report"] = Relationship(
        back_populates="notebook", cascade_delete=True
    )
    requirements: list["Requirement"] = Relationship(
        back_populates="notebooks", link_model=NotebookRequirement
    )


class Source(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    notebook_id: int = Field(
        foreign_key="notebook.id", ondelete="CASCADE", index=True
    )
    title: str
    reference: str = ""  # URL or citation reference
    summary: str = ""
    reliability: SourceReliability | None = Field(default=None)
    credibility: SourceCredibility | None = Field(default=None)
    grading_origin: SourceGradingOrigin = Field(default=SourceGradingOrigin.UNGRADED)
    grading_engine: str = ""
    grading_rationale: str = ""
    grading_error: str = ""
    graded_at: datetime | None = Field(default=None)
    captured_at: datetime = Field(default_factory=utcnow)

    notebook: Notebook = Relationship(back_populates="sources")


class Note(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    notebook_id: int = Field(
        foreign_key="notebook.id", ondelete="CASCADE", index=True
    )
    body_md: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    notebook: Notebook = Relationship(back_populates="notes")


class DiamondModel(SQLModel, table=True):
    """A Diamond Model of Intrusion Analysis assessment held against a notebook.

    Captures the four core features (adversary / capability / infrastructure /
    victim) plus an analytic confidence. Rendered to an SVG diagram and embedded
    inline in a report by writing the ``[[diamond:ID]]`` token in the report
    body — there is no explicit citation link table; the association is the
    token, resolved (notebook-scoped) at render time. See ``services/diamond.py``.
    """

    id: int | None = Field(default=None, primary_key=True)
    notebook_id: int = Field(
        foreign_key="notebook.id", ondelete="CASCADE", index=True
    )
    title: str
    adversary: str = ""
    capability: str = ""
    infrastructure: str = ""
    victim: str = ""
    confidence: DiamondConfidence = Field(default=DiamondConfidence.MODERATE)
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    notebook: Notebook = Relationship(back_populates="diamond_models")


class Attachment(SQLModel, table=True):
    """An uploaded file held against a notebook as reference material.

    Only ``stored_filename`` (a server-generated UUID name) is ever used to build
    a path on disk; ``original_filename`` is metadata for display/download.
    """

    id: int | None = Field(default=None, primary_key=True)
    notebook_id: int = Field(
        foreign_key="notebook.id", ondelete="CASCADE", index=True
    )
    title: str = ""  # optional label; falls back to the filename in the UI
    original_filename: str
    stored_filename: str  # uuid4().hex + ext — the on-disk name
    content_type: str
    file_size: int = 0
    summary: str = ""
    uploaded_at: datetime = Field(default_factory=utcnow)

    notebook: Notebook = Relationship(back_populates="attachments")
    reports: list["Report"] = Relationship(
        back_populates="cited_attachments", link_model=ReportAttachment
    )


class Report(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    notebook_id: int = Field(
        foreign_key="notebook.id", ondelete="CASCADE", index=True
    )
    title: str
    body_md: str = ""
    # ICD 203 structured-judgement scaffolding (optional markdown). Key Judgements
    # lead the product (the BLUF, and the sole content of the brief formats); Key
    # Assumptions and Intelligence Gaps surface the analytic caveats.
    key_judgements: str = ""
    key_assumptions: str = ""
    intelligence_gaps: str = ""
    intel_level: IntelLevel = Field(default=IntelLevel.OPERATIONAL)
    tlp: TLP = Field(default=TLP.AMBER)
    status: ReportStatus = Field(default=ReportStatus.DRAFT)
    author_id: int = Field(foreign_key="user.id")
    reviewer_id: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    published_at: datetime | None = Field(default=None)

    notebook: Notebook = Relationship(back_populates="reports")
    cited_sources: list[Source] = Relationship(link_model=ReportSource)
    cited_attachments: list["Attachment"] = Relationship(
        back_populates="reports", link_model=ReportAttachment
    )
    rendered_products: list["RenderedProduct"] = Relationship(
        back_populates="report", cascade_delete=True
    )
    requirements: list["Requirement"] = Relationship(
        back_populates="reports", link_model=ReportRequirement
    )
    tags: list["Tag"] = Relationship(
        back_populates="reports", link_model=ReportTag
    )
    dissemination_events: list["DisseminationEvent"] = Relationship(
        back_populates="report", cascade_delete=True
    )


class RenderedProduct(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", ondelete="CASCADE", index=True)
    format: ProductFormat
    pdf_path: str
    rendered_at: datetime = Field(default_factory=utcnow)

    report: Report = Relationship(back_populates="rendered_products")


class Requirement(SQLModel, table=True):
    """Stakeholder intelligence requirement (PIR/RFI) feeding analyst tasking."""

    id: int | None = Field(default=None, primary_key=True)
    stakeholder_id: int = Field(foreign_key="user.id", index=True)
    title: str
    description: str = ""
    intel_level: IntelLevel = Field(default=IntelLevel.STRATEGIC)
    priority: Priority = Field(default=Priority.MEDIUM)
    status: RequirementStatus = Field(default=RequirementStatus.OPEN)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    stakeholder: User = Relationship()
    notebooks: list[Notebook] = Relationship(
        back_populates="requirements", link_model=NotebookRequirement
    )
    reports: list[Report] = Relationship(
        back_populates="requirements", link_model=ReportRequirement
    )


_PRIORITY_RANK = {
    Priority.CRITICAL: 3,
    Priority.HIGH: 2,
    Priority.MEDIUM: 1,
    Priority.LOW: 0,
}


def priority_rank(priority: Priority) -> int:
    """Sort key (higher = more urgent) for ordering the tasking board."""
    return _PRIORITY_RANK[Priority(priority)]


class Tag(SQLModel, table=True):
    """A term in the controlled CTI taxonomy. Admin-curated; analysts select
    (never create) tags when classifying a report. Retired tags (``active`` =
    False) stay attached to historical reports but are no longer offered."""

    __table_args__ = (UniqueConstraint("kind", "slug", name="uq_tag_kind_slug"),)

    id: int | None = Field(default=None, primary_key=True)
    kind: TagKind = Field(index=True)
    label: str
    slug: str = Field(index=True)  # normalised label; unique within a kind
    external_id: str = ""  # e.g. MITRE ATT&CK technique id "T1566"
    description: str = ""
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)

    reports: list[Report] = Relationship(
        back_populates="tags", link_model=ReportTag
    )


class DisseminationEvent(SQLModel, table=True):
    """A published report delivered to a stakeholder's feed (Milestone 3)."""

    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", ondelete="CASCADE", index=True)
    stakeholder_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    read_at: datetime | None = Field(default=None)

    report: Report = Relationship(back_populates="dissemination_events")
