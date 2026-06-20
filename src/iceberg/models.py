"""SQLModel domain models and enums for Iceberg.

Kept in a single module so cross-model relationships resolve without circular
imports. User/Notebook/Source/Note/Report (+ link tables) form the authoring
core; Requirement drives stakeholder intake and the analyst tasking board.
"""

from datetime import date, datetime, timezone
from enum import StrEnum

from sqlalchemy import JSON, Column, UniqueConstraint
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


class RequirementKind(StrEnum):
    """CTI requirement type — drives collection differently per kind."""

    PIR = "PIR"  # Priority Intelligence Requirement: leadership-designated, time-bound
    GIR = "GIR"  # General Intelligence Requirement: standing baseline coverage
    RFI = "RFI"  # Request For Information: ad-hoc, one-off question


class ProductUsefulness(StrEnum):
    """How useful a stakeholder found a disseminated product (feedback loop)."""

    NOT_USEFUL = "NOT_USEFUL"
    USEFUL = "USEFUL"
    HIGHLY_USEFUL = "HIGHLY_USEFUL"


class RfiSatisfaction(StrEnum):
    """Whether a delivered product satisfied the requirement that prompted it."""

    MET = "MET"
    PARTIALLY_MET = "PARTIALLY_MET"
    NOT_MET = "NOT_MET"


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


class Motivation(StrEnum):
    """Why a threat entity operates (attribution profile, roadmap 2b). Multi-valued
    on a Tag — actors commonly have mixed motives (e.g. DPRK = espionage +
    financial). Only meaningful for the named-threat kinds."""

    ESPIONAGE = "ESPIONAGE"
    FINANCIAL = "FINANCIAL"
    HACKTIVISM = "HACKTIVISM"
    DESTRUCTIVE = "DESTRUCTIVE"
    INFLUENCE = "INFLUENCE"


class DiamondConfidence(StrEnum):
    """Analytic confidence in a Diamond Model assessment."""

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class ACHCellRating(StrEnum):
    """Consistency of one evidence item with one hypothesis (Heuer ACH).

    The diagnostic signal is *inconsistency*: a hypothesis is weakened — never
    confirmed — by evidence it cannot explain, so the inconsistency weights
    (``INCONSISTENT`` 1, ``STRONGLY_INCONSISTENT`` 2; all others 0) drive the
    per-hypothesis score. ``NEUTRAL`` is also the default for an unrated cell.
    """

    STRONGLY_CONSISTENT = "STRONGLY_CONSISTENT"  # ++
    CONSISTENT = "CONSISTENT"  # +
    NEUTRAL = "NEUTRAL"  # N
    INCONSISTENT = "INCONSISTENT"  # −
    STRONGLY_INCONSISTENT = "STRONGLY_INCONSISTENT"  # −−
    NOT_APPLICABLE = "NOT_APPLICABLE"  # N/A


class AnalyticConfidence(StrEnum):
    """ICD 203 analytic confidence in a report's judgements (distinct from the
    *likelihood* of the assessed event — see the probability yardstick). Scoped
    to the whole product, unlike the per-assessment ``DiamondConfidence``."""

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
    ach_models: list["ACHModel"] = Relationship(
        back_populates="notebook", cascade_delete=True
    )
    figures: list["Figure"] = Relationship(
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


class ACHModel(SQLModel, table=True):
    """An Analysis of Competing Hypotheses (Heuer) matrix held against a notebook.

    Adjudicates a key intelligence ``question`` by scoring a matrix of
    ``hypotheses`` × ``evidence``: each cell of ``ratings`` records how
    consistent one evidence item is with one hypothesis. The hypotheses /
    evidence carry **stable string ids** (``h1`` / ``e1`` …) — not positional
    indices — and ``ratings`` is keyed ``"{hid}:{eid}"`` so removing a row never
    silently re-keys the matrix. The analytic payload is the per-hypothesis
    *inconsistency* score (the least-inconsistent hypothesis is the most
    tenable). Rendered to an SVG matrix and embedded inline in a report via the
    ``[[ach:ID]]`` token — like the Diamond Model there is no citation link
    table, the token is the association, resolved (notebook-scoped) at render
    time. See ``services/ach.py``.
    """

    id: int | None = Field(default=None, primary_key=True)
    notebook_id: int = Field(
        foreign_key="notebook.id", ondelete="CASCADE", index=True
    )
    title: str
    question: str = ""  # the key intelligence question being adjudicated
    hypotheses: list[dict] = Field(  # [{"id": "h1", "text": "..."}]
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    evidence: list[dict] = Field(  # [{"id": "e1", "text": "..."}]
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    ratings: dict[str, str] = Field(  # {"h1:e1": "INCONSISTENT", ...}
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    notebook: Notebook = Relationship(back_populates="ach_models")


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


class Figure(SQLModel, table=True):
    """An uploaded image held against a notebook and embedded inline into a
    report by writing the ``[[figure:ID]]`` token in the report body.

    Like ``DiamondModel`` (and unlike ``Attachment``), there is no citation link
    table — the token *is* the association, resolved (notebook-scoped) at render
    time. Restricted to PNG/JPEG/GIF (the browser-data-URI ∩ Typst-image()
    intersection). Only ``stored_filename`` (a server-generated UUID name) builds
    a path on disk; ``original_filename`` is display/download metadata. See
    ``services/figures.py``.
    """

    id: int | None = Field(default=None, primary_key=True)
    notebook_id: int = Field(
        foreign_key="notebook.id", ondelete="CASCADE", index=True
    )
    title: str = ""  # caption / alt text; falls back to the filename in the UI
    original_filename: str
    stored_filename: str  # uuid4().hex + ext — the on-disk name
    content_type: str  # image/png | image/jpeg | image/gif
    file_size: int = 0
    created_at: datetime = Field(default_factory=utcnow)

    notebook: Notebook = Relationship(back_populates="figures")


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
    # ICD 203 estimative language: analytic confidence in the judgements (LOW/
    # MODERATE/HIGH). Optional — None means "not stated"; no marking is shown.
    # Kept distinct from the *likelihood* of the assessed event, which analysts
    # express in prose via the probability yardstick.
    analytic_confidence: AnalyticConfidence | None = Field(default=None)
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
    feedback: list["ProductFeedback"] = Relationship(
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
    kind: RequirementKind = Field(default=RequirementKind.RFI, index=True)
    # PIR-only time-bound scaffolding (blank/ignored for GIR/RFI).
    decision_context: str = ""
    review_by: date | None = Field(default=None)
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


_KIND_RANK = {
    RequirementKind.PIR: 2,
    RequirementKind.GIR: 1,
    RequirementKind.RFI: 0,
}


def kind_rank(kind: RequirementKind) -> int:
    """Tiebreak rank (higher = more strategically prioritised)."""
    return _KIND_RANK[RequirementKind(kind)]


# A PIR is treated as at least HIGH priority on the board, so it always leads
# standing/ad-hoc work — but a true CRITICAL item of any kind still tops the
# column (urgency is never buried). See FR #42 "urgency vs kind".
_PIR_PRIORITY_FLOOR = _PRIORITY_RANK[Priority.HIGH]


def board_rank(req: "Requirement") -> int:
    """Effective board priority: a PIR is floored at HIGH; others rank as-is."""
    base = priority_rank(req.priority)
    if RequirementKind(req.kind) is RequirementKind.PIR:
        return max(base, _PIR_PRIORITY_FLOOR)
    return base


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
    # Alternate names for named-threat entities (ACTOR/MALWARE/CAMPAIGN) so the
    # APT28 / Fancy Bear / Sofacy naming problem resolves to one tag; makes search
    # alias-aware (see services/search.py). Empty for other kinds.
    aliases: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    # Structured attribution for named-threat entities (roadmap 2b): suspected
    # sponsor/country (free text, e.g. "Russia (GRU Unit 26165)"), motivation(s),
    # and fuzzy first/last-seen markers (free text, e.g. "2004" … "present").
    # Empty for non-named kinds; surfaced on the /tags/{id} entity profile.
    suspected_attribution: str = ""
    # Stored as plain strings (a JSON list, like ``aliases``); values are validated
    # against ``Motivation`` on write (services/tags.normalise_motivations).
    motivations: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    first_seen: str = ""
    last_seen: str = ""
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


class ProductFeedback(SQLModel, table=True):
    """A stakeholder's feedback on a disseminated report — the intelligence-cycle
    feedback loop (backlog D). One row per (report, stakeholder); the submit path
    upserts. Optionally tied to one of the stakeholder's own requirements that the
    product satisfied — a ``MET`` verdict from the owner auto-closes it."""

    __table_args__ = (
        UniqueConstraint("report_id", "stakeholder_id", name="uq_feedback_report_stakeholder"),
    )

    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", ondelete="CASCADE", index=True)
    stakeholder_id: int = Field(foreign_key="user.id", index=True)
    # Optional: which of the stakeholder's own requirements this product satisfied.
    requirement_id: int | None = Field(
        default=None, foreign_key="requirement.id", ondelete="SET NULL", index=True
    )
    usefulness: ProductUsefulness
    satisfaction: RfiSatisfaction | None = Field(default=None)
    comment: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    report: Report = Relationship(back_populates="feedback")
