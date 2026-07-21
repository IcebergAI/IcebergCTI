"""SQLModel domain models and enums for Iceberg.

Kept in a single module so cross-model relationships resolve without circular
imports. User/Notebook/Source/Note/Report (+ link tables) form the authoring
core; Requirement drives stakeholder intake and the analyst tasking board.
"""

from datetime import date, datetime, timezone
from enum import StrEnum

from sqlalchemy import JSON, Column, Integer, UniqueConstraint
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


class JobKind(StrEnum):
    """Durable external-work categories owned by the lightweight outbox."""

    DISSEMINATION_EMAIL = "DISSEMINATION_EMAIL"
    DISSEMINATION_WEBHOOK = "DISSEMINATION_WEBHOOK"
    RSS_POLL = "RSS_POLL"


class JobStatus(StrEnum):
    """Lifecycle of an :class:`OutboxJob`.

    A ``RUNNING`` job is always paired with an expiry lease, so a worker crash
    cannot strand it permanently.  Retriable failures return to ``PENDING``;
    terminal failures remain inspectable as ``FAILED``.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


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


class IOCType(StrEnum):
    """Type of an indicator of compromise. The **enum value is the MISP attribute
    type string** (so a push maps directly with no lookup table); the display
    label comes from :func:`ioc_type_label`. A deliberately small curated set —
    Iceberg only stages indicators; MISP owns the full taxonomy."""

    IP_SRC = "ip-src"
    IP_DST = "ip-dst"
    DOMAIN = "domain"
    HOSTNAME = "hostname"
    URL = "url"
    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    EMAIL = "email-src"
    FILENAME = "filename"
    CVE = "vulnerability"


_IOC_TYPE_LABELS = {
    IOCType.IP_SRC: "IP address (source)",
    IOCType.IP_DST: "IP address (destination)",
    IOCType.DOMAIN: "Domain",
    IOCType.HOSTNAME: "Hostname",
    IOCType.URL: "URL",
    IOCType.MD5: "MD5 hash",
    IOCType.SHA1: "SHA1 hash",
    IOCType.SHA256: "SHA256 hash",
    IOCType.EMAIL: "Email address",
    IOCType.FILENAME: "Filename",
    IOCType.CVE: "CVE / vulnerability",
}


def ioc_type_label(ioc_type: IOCType) -> str:
    return _IOC_TYPE_LABELS[IOCType(ioc_type)]


class ProxyMode(StrEnum):
    """How outbound HTTP connections are routed (global proxy connectivity).

    SYSTEM honours the environment proxy vars (``HTTP(S)_PROXY``/``NO_PROXY``);
    NONE always connects directly (env ignored); EXPLICIT routes through a
    configured proxy except for hosts in the no-proxy exclusion list."""

    NONE = "NONE"
    SYSTEM = "SYSTEM"
    EXPLICIT = "EXPLICIT"


class AuditOutcome(StrEnum):
    """Whether a security-relevant event succeeded or was denied/failed."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class AuditSeverity(StrEnum):
    """OWASP-style severity for an audit event (ordered low→high)."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


_AUDIT_SEVERITY_RANK = {
    AuditSeverity.INFO: 0,
    AuditSeverity.WARNING: 1,
    AuditSeverity.CRITICAL: 2,
}


def audit_severity_rank(severity: AuditSeverity) -> int:
    """Sort/threshold key (higher = more severe) for the SIEM min-severity gate."""
    return _AUDIT_SEVERITY_RANK[AuditSeverity(severity)]


class AuditCategory(StrEnum):
    """Coarse grouping of audit events (OWASP event taxonomy)."""

    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    LIFECYCLE = "LIFECYCLE"
    ADMIN = "ADMIN"
    DATA_ACCESS = "DATA_ACCESS"
    DISSEMINATION = "DISSEMINATION"
    SYSTEM = "SYSTEM"


class AuditAction(StrEnum):
    """Controlled vocabulary of security-relevant actions. The ``action`` column
    is a plain string so callers may record values outside this enum, but the
    known events live here for one-place discoverability."""

    # Authentication
    AUTH_LOGIN = "AUTH_LOGIN"
    AUTH_LOGOUT = "AUTH_LOGOUT"
    OIDC_SETTINGS_UPDATED = "OIDC_SETTINGS_UPDATED"
    RATE_LIMITED = "RATE_LIMITED"
    NOTEBOOK_DELETED = "NOTEBOOK_DELETED"
    # Authorization (failure outcomes captured centrally)
    AUTHZ_DENIED = "AUTHZ_DENIED"
    CSRF_BLOCKED = "CSRF_BLOCKED"
    # Report lifecycle
    REPORT_SUBMITTED = "REPORT_SUBMITTED"
    REPORT_APPROVED = "REPORT_APPROVED"
    REPORT_SENT_BACK = "REPORT_SENT_BACK"
    REPORT_PUBLISHED = "REPORT_PUBLISHED"
    # Admin taxonomy curation
    TAG_CREATED = "TAG_CREATED"
    TAG_UPDATED = "TAG_UPDATED"
    TAG_DELETED = "TAG_DELETED"
    TAG_MERGED = "TAG_MERGED"
    # Governed analyst-assist calls (prompt/response bodies are never audited).
    AI_ASSIST = "AI_ASSIST"
    # Audit configuration (admin)
    AUDIT_SETTINGS_UPDATED = "AUDIT_SETTINGS_UPDATED"
    AUDIT_TEST = "AUDIT_TEST"
    # Outbound proxy configuration (admin)
    PROXY_SETTINGS_UPDATED = "PROXY_SETTINGS_UPDATED"
    PROXY_TEST = "PROXY_TEST"
    # Inbound collection — RSS feed configuration (admin)
    FEED_CREATED = "FEED_CREATED"
    FEED_UPDATED = "FEED_UPDATED"
    FEED_DELETED = "FEED_DELETED"
    FEED_FETCHED = "FEED_FETCHED"
    # IOC capture (notebook indicators)
    IOC_CREATED = "IOC_CREATED"
    IOC_UPDATED = "IOC_UPDATED"
    IOC_DELETED = "IOC_DELETED"
    # MISP push integration (admin config + report event push)
    MISP_SETTINGS_UPDATED = "MISP_SETTINGS_UPDATED"
    MISP_TEST = "MISP_TEST"
    MISP_PUSHED = "MISP_PUSHED"
    # Publication webhook configuration (admin)
    WEBHOOK_SETTINGS_UPDATED = "WEBHOOK_SETTINGS_UPDATED"
    WEBHOOK_TEST = "WEBHOOK_TEST"
    # Sensitive file access
    ATTACHMENT_UPLOADED = "ATTACHMENT_UPLOADED"
    ATTACHMENT_DOWNLOADED = "ATTACHMENT_DOWNLOADED"
    ATTACHMENT_DELETED = "ATTACHMENT_DELETED"
    FIGURE_UPLOADED = "FIGURE_UPLOADED"
    FIGURE_DELETED = "FIGURE_DELETED"


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


class ReportIOC(SQLModel, table=True):
    """Indicators from a notebook that a report explicitly cites — rendered in the
    report's Indicators appendix and pushed to MISP as the event's attributes."""

    report_id: int | None = Field(
        default=None, foreign_key="report.id", ondelete="CASCADE", primary_key=True
    )
    ioc_id: int | None = Field(
        default=None, foreign_key="ioc.id", ondelete="CASCADE", primary_key=True
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


class UserTagSubscription(SQLModel, table=True):
    """A stakeholder subscribes to a taxonomy tag/entity for dissemination."""

    user_id: int | None = Field(
        default=None, foreign_key="user.id", ondelete="CASCADE", primary_key=True
    )
    tag_id: int | None = Field(
        default=None, foreign_key="tag.id", ondelete="CASCADE", primary_key=True
    )


class UserAudienceGroup(SQLModel, table=True):
    """Membership of a need-to-know audience group."""

    user_id: int | None = Field(
        default=None, foreign_key="user.id", ondelete="CASCADE", primary_key=True
    )
    group_id: int | None = Field(
        default=None, foreign_key="audiencegroup.id", ondelete="CASCADE", primary_key=True
    )


class ReportAudienceGroup(SQLModel, table=True):
    """A published report is limited to the given need-to-know audience group."""

    report_id: int | None = Field(
        default=None, foreign_key="report.id", ondelete="CASCADE", primary_key=True
    )
    group_id: int | None = Field(
        default=None, foreign_key="audiencegroup.id", ondelete="CASCADE", primary_key=True
    )


# --------------------------------------------------------------------------- #
# Core tables
# --------------------------------------------------------------------------- #
class User(SQLModel, table=True):
    # ``sub`` is only unique within an OpenID Provider, and multi-provider support
    # means two IdPs could in theory reuse an (issuer, sub) pair — so identity is
    # keyed on ``(auth_provider, issuer, sub)``. Legacy/dev accounts stay unbound
    # (all three NULL) until an administrator explicitly links them; OIDC
    # provisioning must never use email as an identity key. ``email`` is therefore
    # NOT globally unique — the same person may exist under two providers.
    __table_args__ = (
        UniqueConstraint(
            "auth_provider", "issuer", "sub", name="uq_user_provider_issuer_sub"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    auth_provider: str | None = Field(default=None, index=True)
    issuer: str | None = Field(default=None, index=True)
    sub: str | None = Field(default=None, index=True)
    email: str = Field(index=True)
    display_name: str
    role: Role = Field(default=Role.ANALYST)
    preferred_intel_level: IntelLevel | None = Field(default=None)
    token_version: int = Field(default=0)
    department: str = ""
    job_title: str = ""
    company_name: str = ""
    office_location: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    tag_subscriptions: list["Tag"] = Relationship(
        back_populates="subscribers", link_model=UserTagSubscription
    )
    audience_groups: list["AudienceGroup"] = Relationship(
        back_populates="members", link_model=UserAudienceGroup
    )


class AudienceGroup(SQLModel, table=True):
    """Need-to-know group for published products.

    A report with no audience groups keeps the existing broadly visible
    published-report behavior. A report with groups is visible only to writers or
    members of at least one assigned group.
    """

    __table_args__ = (UniqueConstraint("slug", name="uq_audience_group_slug"),)

    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(index=True)
    description: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    members: list[User] = Relationship(
        back_populates="audience_groups", link_model=UserAudienceGroup
    )
    reports: list["Report"] = Relationship(
        back_populates="audience_groups", link_model=ReportAudienceGroup
    )


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
    iocs: list["IOC"] = Relationship(
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
    # Handling marking for this collected material. Manual sources default to
    # AMBER; RSS-ingested sources are stamped CLEAR. Gates AI egress of the
    # source's content and is inherited by IOCs captured from it.
    tlp: TLP = Field(default=TLP.AMBER)
    summary: str = ""
    # Analyst-provided or ingested source text. The app deliberately does not
    # fetch arbitrary source URLs; AI/source summaries operate only on content
    # already present here.
    content_md: str = ""
    ai_provenance: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
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


class IOC(SQLModel, table=True):
    """An indicator of compromise captured against a notebook.

    Light-touch, *transient* staging only — the authoritative IOC store is
    external (MISP). An analyst records indicators manually (the future LLM/AI
    phase will auto-extract them from sources — see ``services/iocs.py``); a
    report cites a subset (via the :class:`ReportIOC` link table) for its
    Indicators appendix, and a writer can push those cited indicators to MISP as
    one event. ``source_id`` is optional provenance (which source it came from),
    nulled if that source is later deleted.
    """

    id: int | None = Field(default=None, primary_key=True)
    notebook_id: int = Field(
        foreign_key="notebook.id", ondelete="CASCADE", index=True
    )
    ioc_type: IOCType = Field(default=IOCType.DOMAIN)
    value: str
    description: str = ""  # optional analyst context / role of the indicator
    # Handling marking, inherited from the provenance source when one is set
    # (else AMBER). Stamped per-attribute on the MISP push.
    tlp: TLP = Field(default=TLP.AMBER)
    source_id: int | None = Field(
        default=None, foreign_key="source.id", ondelete="SET NULL", index=True
    )
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    notebook: Notebook = Relationship(back_populates="iocs")
    source: Source | None = Relationship()


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


_REPORT_VERSION_COLUMN = Column("version", Integer, nullable=False, default=1)


class Report(SQLModel, table=True):
    # SQLAlchemy emits ``UPDATE ... WHERE version = :loaded_version`` and bumps
    # this value automatically. That protects an edit loaded before publication
    # from silently writing over the immutable finished row.
    __mapper_args__ = {"version_id_col": _REPORT_VERSION_COLUMN}

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
    ai_provenance: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    intel_level: IntelLevel = Field(default=IntelLevel.OPERATIONAL)
    tlp: TLP = Field(default=TLP.AMBER)
    status: ReportStatus = Field(default=ReportStatus.DRAFT)
    author_id: int = Field(foreign_key="user.id")
    reviewer_id: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    published_at: datetime | None = Field(default=None)
    # Incremented by the ORM on every persisted update.  Routes translate a
    # stale update into 409 rather than allowing an edit loaded before publish
    # to overwrite the finished product.
    version: int = Field(default=1, sa_column=_REPORT_VERSION_COLUMN)
    # The immutable finished-product snapshot selected at publication time.
    # Empty for drafts and for legacy rows before their boot-time backfill.
    publication_snapshot_hash: str = Field(default="", index=True)

    notebook: Notebook = Relationship(back_populates="reports")
    cited_sources: list[Source] = Relationship(link_model=ReportSource)
    cited_iocs: list["IOC"] = Relationship(link_model=ReportIOC)
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
    audience_groups: list[AudienceGroup] = Relationship(
        back_populates="reports", link_model=ReportAudienceGroup
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
    # A stakeholder may only obtain a PDF built from the report's active
    # publication snapshot. Writer draft renders deliberately retain an empty
    # snapshot hash.
    snapshot_hash: str = Field(default="", index=True)
    rendered_at: datetime = Field(default_factory=utcnow)

    report: Report = Relationship(back_populates="rendered_products")


class PublicationSnapshot(SQLModel, table=True):
    """Immutable publication-time representation of a finished report.

    The JSON payload carries rendered HTML, Typst input and self-contained
    embedded artefacts.  Keeping it separate from the mutable ``Report`` row
    makes a published product stable even when collection material changes or
    is deleted later.
    """

    __table_args__ = (UniqueConstraint("report_id", name="uq_publication_snapshot_report"),)

    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", ondelete="CASCADE", index=True)
    snapshot_hash: str = Field(index=True)
    payload: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    created_at: datetime = Field(default_factory=utcnow)


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
    # ATT&CK techniques can map to more than one tactic.  This is deliberately
    # distinct from the human-readable description: legacy rows used that field
    # as a one-tactic convention, while this structured list drives Navigator
    # layers and coverage matrices.
    attack_tactics: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    active: bool = Field(default=True)
    # A retired source term remains in the taxonomy after a merge so historic
    # curation decisions retain an auditable, queryable lineage.  The canonical
    # tag owns the report/subscription links; this field only records where the
    # source term was consolidated.
    merged_into_tag_id: int | None = Field(
        default=None, foreign_key="tag.id", ondelete="SET NULL", index=True
    )
    merged_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow, index=True)

    reports: list[Report] = Relationship(
        back_populates="tags", link_model=ReportTag
    )
    subscribers: list[User] = Relationship(
        back_populates="tag_subscriptions", link_model=UserTagSubscription
    )


class ReportEmbedding(SQLModel, table=True):
    """Optional semantic-search vector for a published report."""

    report_id: int | None = Field(
        default=None, foreign_key="report.id", ondelete="CASCADE", primary_key=True
    )
    backend: str = ""
    vector: list[float] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    updated_at: datetime = Field(default_factory=utcnow)


class DisseminationEvent(SQLModel, table=True):
    """A published report delivered to a stakeholder's feed (Milestone 3)."""

    __table_args__ = (
        UniqueConstraint("report_id", "stakeholder_id", name="uq_dissemination_report_stakeholder"),
    )

    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", ondelete="CASCADE", index=True)
    stakeholder_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    read_at: datetime | None = Field(default=None)

    report: Report = Relationship(back_populates="dissemination_events")


class ReportMispEvent(SQLModel, table=True):
    """The MISP event a report's indicators were pushed to (light-touch IOC FR).

    One row per report (unique ``report_id``) recording the external event
    reference (``event_uuid`` / ``event_id``) and the last push outcome, so a
    re-push **updates the same MISP event** (idempotent) and the report view can
    surface success/failure. Kept off the immutable ``Report`` row. Modelled on
    :class:`DisseminationEvent`."""

    __table_args__ = (
        UniqueConstraint("report_id", name="uq_mispevent_report"),
    )

    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="report.id", ondelete="CASCADE", index=True)
    event_uuid: str = ""  # MISP event UUID (the stable cross-instance reference)
    event_id: str = ""  # MISP numeric event id (per-instance)
    external_created: bool = False
    push_token: str = Field(default="", index=True)
    push_started_at: datetime | None = Field(default=None)
    attribute_count: int = 0  # indicators pushed on the last successful push
    last_status: str = ""  # "ok" or a short error summary
    error: str = ""  # last error string (cleared on a successful push)
    pushed_at: datetime | None = Field(default=None)  # last successful push
    updated_at: datetime = Field(default_factory=utcnow)


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


class Feed(SQLModel, table=True):
    """An admin-configured external RSS/Atom feed — the inbound collection
    channel (FR #50). The poller fetches each enabled feed on an interval and
    stores its articles as :class:`FeedItem` rows for analysts to browse and
    "send to notebook". Only an admin ever supplies the ``url`` (analysts never
    do), which is the SSRF-containment boundary. See ``services/feeds.py``.
    """

    id: int | None = Field(default=None, primary_key=True)
    url: str = Field(index=True, unique=True)
    title: str
    description: str = ""
    enabled: bool = Field(default=True)
    last_fetched_at: datetime | None = Field(default=None)
    last_status: str = ""  # e.g. "ok: 12 items" — last successful fetch summary
    fetch_error: str = ""  # last error string (cleared on a successful fetch)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    items: list["FeedItem"] = Relationship(
        back_populates="feed", cascade_delete=True
    )


class FeedItem(SQLModel, table=True):
    """A single article fetched from a :class:`Feed`. Deduped on ``(feed_id,
    guid)`` so re-fetching never duplicates. ``content`` retains the sanitised
    full body as the seam for future IOC extraction / summarisation. An analyst
    captures one into a notebook as a :class:`Source` (stamps ``ingested_at``)."""

    __table_args__ = (
        UniqueConstraint("feed_id", "guid", name="uq_feeditem_feed_guid"),
    )

    id: int | None = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="feed.id", ondelete="CASCADE", index=True)
    guid: str  # entry id or link — the per-feed dedup key
    link: str = ""
    title: str = ""
    summary: str = ""  # nh3-sanitised short description
    content: str = ""  # nh3-sanitised full body (future IOC/summarisation seam)
    author: str = ""
    published_at: datetime | None = Field(default=None)
    fetched_at: datetime = Field(default_factory=utcnow)
    ingested_at: datetime | None = Field(default=None)  # set when sent to a notebook

    feed: Feed = Relationship(back_populates="items")


class OutboxJob(SQLModel, table=True):
    """A durable, lease-based unit of external work.

    Publication feed records are deliberately *not* jobs: they are created
    synchronously in the same transaction as the published report.  This table
    contains only work that leaves the process (email, webhooks and RSS pulls),
    allowing an independently-run worker to retry it after request completion
    or a process restart.
    """

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outboxjob_idempotency_key"),
    )

    id: int | None = Field(default=None, primary_key=True)
    kind: JobKind = Field(index=True)
    status: JobStatus = Field(default=JobStatus.PENDING, index=True)
    # Jobs store only identifiers and non-secret configuration snapshots.  API
    # tokens/passwords remain environment-only and are resolved by the worker.
    payload: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    idempotency_key: str = Field(index=True)
    attempt_count: int = Field(default=0)
    retry_count: int = Field(default=0)
    max_attempts: int = Field(default=5)
    available_at: datetime = Field(default_factory=utcnow, index=True)
    leased_at: datetime | None = Field(default=None)
    lease_expires_at: datetime | None = Field(default=None, index=True)
    lease_token: str = ""
    leased_by: str = ""
    last_error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)


class AuditEvent(SQLModel, table=True):
    """A persisted security-relevant event (the local audit trail).

    Holds the OWASP "when / what / where / who / result" attributes. This is the
    source of truth and survives a SIEM outage; emission to the SIEM is a
    best-effort side effect (see ``services/siem.py``). The ``detail`` JSON is
    deliberately curated by callers — it must **never** carry secrets, tokens,
    passwords, JWTs or file bytes.
    """

    id: int | None = Field(default=None, primary_key=True)
    occurred_at: datetime = Field(default_factory=utcnow, index=True)
    action: str = Field(index=True)  # an AuditAction value (free-form tolerated)
    category: AuditCategory = Field(default=AuditCategory.SYSTEM)
    severity: AuditSeverity = Field(default=AuditSeverity.INFO, index=True)
    outcome: AuditOutcome = Field(default=AuditOutcome.SUCCESS)
    # what — a human-readable summary (OWASP "Description"); auto-derived from the
    # structured fields when a caller doesn't supply one.
    description: str = ""
    # who — actor identity (nullable: anonymous / pre-auth events)
    actor_id: int | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL", index=True
    )
    actor_email: str = ""
    actor_role: str = ""
    source_ip: str = ""
    user_agent: str = ""
    # where
    request_method: str = ""
    request_path: str = ""
    # result / context
    status_code: int | None = Field(default=None)
    resource_type: str = ""
    resource_id: str = ""
    correlation_id: str = Field(default="", index=True)
    detail: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )


class AuditSettings(SQLModel, table=True):
    """Runtime SIEM-emit configuration, admin-editable (single row, id=1).

    Holds only non-secret routing config — the HTTP/HEC token is read from the
    environment (``ICEBERG_AUDIT_HTTP_TOKEN``) and is never persisted here.
    """

    id: int | None = Field(default=None, primary_key=True)
    enabled: bool = Field(default=True)
    # Enabled emit methods: any of "stdout" / "syslog" / "http".
    methods: list[str] = Field(
        default_factory=lambda: ["stdout"],
        sa_column=Column(JSON, nullable=False, server_default='["stdout"]'),
    )
    min_severity: AuditSeverity = Field(default=AuditSeverity.INFO)
    # stdout/file sink
    file_path: str = ""  # empty = stdout logger only
    # syslog sink (RFC 5424)
    syslog_host: str = "localhost"
    syslog_port: int = 514
    syslog_protocol: str = "UDP"  # UDP | TCP
    syslog_facility: int = 13  # "log audit" facility
    # http event-collector / webhook sink (token from env)
    http_endpoint: str = ""
    http_verify_tls: bool = True
    updated_at: datetime = Field(default_factory=utcnow)


class ProxySettings(SQLModel, table=True):
    """Global outbound-proxy configuration, admin-editable (single row, id=1).

    Holds only non-secret routing config — proxy credentials, when needed, are
    read from the environment (``ICEBERG_PROXY_USERNAME``/``ICEBERG_PROXY_PASSWORD``)
    and injected at call time, never persisted here. See ``services/proxy.py``.
    """

    id: int | None = Field(default=None, primary_key=True)
    mode: ProxyMode = Field(default=ProxyMode.SYSTEM)
    # EXPLICIT mode: scheme://host:port (no credentials).
    proxy_url: str = ""
    # EXPLICIT mode: comma-separated domains/suffixes + CIDR ranges to bypass.
    no_proxy: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class MISPSettings(SQLModel, table=True):
    """Outbound MISP connection configuration, admin-editable (single row, id=1).

    The authoritative IOC store is external — this row only configures the push.
    Holds only non-secret config — the API key is read from the environment
    (``ICEBERG_MISP_API_KEY``) and injected at call time, never persisted here
    (same discipline as the SIEM HTTP token / proxy credentials). See
    ``services/misp.py``."""

    id: int | None = Field(default=None, primary_key=True)
    enabled: bool = Field(default=False)
    # Base URL of the MISP instance (e.g. https://misp.example.org); the event
    # endpoint is derived from it. No credentials in the URL.
    url: str = ""
    verify_tls: bool = True
    # MISP event defaults applied on push (numeric ids per the MISP API).
    default_distribution: int = 0  # 0 = your organisation only
    default_threat_level: int = 4  # 4 = undefined
    default_published: bool = False  # leave events unpublished for review by default
    updated_at: datetime = Field(default_factory=utcnow)


class WebhookSettings(SQLModel, table=True):
    """Report-publication webhook config, admin-editable (single row, id=1).

    On publish, Iceberg POSTs report **metadata only** to ``url`` as a background
    task (see ``services/dissemination.send_webhook_notification``). Holds only
    non-secret config — the bearer token is read from the environment
    (``ICEBERG_WEBHOOK_TOKEN``) and injected at call time, never persisted here
    (same discipline as the SIEM HTTP token / MISP API key / proxy credentials).
    Mirrors ``MISPSettings`` / ``ProxySettings``."""

    id: int | None = Field(default=None, primary_key=True)
    enabled: bool = Field(default=False)
    # Endpoint to POST the publication event to (no credentials in the URL).
    url: str = ""
    # Bounds the POST so a stuck endpoint can't hang the background task.
    timeout: float = 5.0
    # ``generic`` is deliberately the default so existing integrations retain
    # their exact JSON envelope. Slack and Teams are metadata-only adapters.
    format: str = Field(default="generic", max_length=16)
    updated_at: datetime = Field(default_factory=utcnow)


class OIDCSettings(SQLModel, table=True):
    """Multi-provider OIDC configuration, admin-editable (single row, id=1).

    Supports Microsoft Entra, Authentik, Auth0 and Okta simultaneously via one
    generic flow. Flattened per-provider fields (``<p>_enabled``, ``<p>_client_id``,
    a provider locator, ``<p>_scopes``, ``<p>_role_claim``, ``<p>_role_map``). The
    per-provider **client secret is env-only** (``ICEBERG_OIDC_<PROVIDER>_CLIENT_SECRET``)
    and never stored here (same discipline as the MISP/webhook/proxy/SIEM secrets).
    Env seeds the row on first read; see ``services/oidc_settings.py`` +
    ``auth/oidc/``. A ``role_map`` is ``"group=ROLE,other=ROLE"``; an unmapped
    group falls back to least-privilege ``STAKEHOLDER``."""

    id: int | None = Field(default=None, primary_key=True)
    # Base URL the IdP redirects back to (per-provider callback path appended).
    redirect_base_url: str = ""

    # Microsoft Entra ID
    entra_enabled: bool = False
    entra_client_id: str = ""
    entra_tenant_id: str = ""
    entra_scopes: str = "openid email profile"
    entra_role_claim: str = "roles"
    entra_role_map: str = ""

    # Authentik (self-hosted): locator is base_url + application slug
    authentik_enabled: bool = False
    authentik_client_id: str = ""
    authentik_base_url: str = ""
    authentik_app_slug: str = ""
    authentik_scopes: str = "openid email profile"
    authentik_role_claim: str = "groups"
    authentik_role_map: str = ""

    # Auth0: locator is the tenant domain
    auth0_enabled: bool = False
    auth0_client_id: str = ""
    auth0_domain: str = ""
    auth0_scopes: str = "openid email profile"
    auth0_role_claim: str = "roles"
    auth0_role_map: str = ""

    # Okta: locator is domain + authorization server id
    okta_enabled: bool = False
    okta_client_id: str = ""
    okta_domain: str = ""
    okta_auth_server: str = "default"
    okta_scopes: str = "openid email profile"
    okta_role_claim: str = "groups"
    okta_role_map: str = ""

    updated_at: datetime = Field(default_factory=utcnow)
