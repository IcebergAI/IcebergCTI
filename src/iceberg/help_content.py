"""In-app help & onboarding content.

The role guides and the shared intelligence-concepts glossary are held here as
structured data (one module is the single source of truth, so wording edits live
in one place) and rendered by ``templates/help.html`` via the ``/help`` portal
route. Content is plain text + lists, rendered through Jinja autoescaping — no
markdown/sanitisation pass is needed.

The ``Concept.slug`` values are a stable contract: ``help.html`` uses them as
anchor ``id``s, and the contextual "?" deep-links scattered across the portal
(e.g. ``/help#dissemination``) point at them. Renaming a slug means updating
those links too.
"""

from dataclasses import dataclass, field

from .models import Role


@dataclass(frozen=True)
class HelpLink:
    """A pointer from a role guide to a screen the role uses."""

    label: str
    href: str


@dataclass(frozen=True)
class RoleGuide:
    """The "who you are / what you do" guide for a single role."""

    role: Role
    tagline: str
    workflow: list[str]
    can: list[str]
    cannot: list[str] = field(default_factory=list)
    key_screens: list[HelpLink] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)  # related Concept slugs


@dataclass(frozen=True)
class Concept:
    """A glossary entry. ``slug`` is the anchor id used for deep-links."""

    slug: str
    term: str
    category: str
    body: str
    points: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProbabilityBand:
    """A rung on the ICD 203 probability yardstick: an estimative term mapped to a
    percentage band. Analysts phrase the *likelihood* of an assessed event in
    these standardised terms — kept distinct from analytic confidence (the
    ``analytic_confidence`` marking)."""

    term: str
    low: int
    high: int

    @property
    def range_label(self) -> str:
        return f"{self.low:02d}–{self.high}%"


# The single source of truth for the editor's probability-yardstick reference
# panel and the estimative-language glossary entry below.
PROBABILITY_YARDSTICK: list[ProbabilityBand] = [
    ProbabilityBand("Almost no chance", 1, 5),
    ProbabilityBand("Very unlikely", 5, 20),
    ProbabilityBand("Unlikely", 20, 45),
    ProbabilityBand("Roughly even chance", 45, 55),
    ProbabilityBand("Likely", 55, 80),
    ProbabilityBand("Very likely", 80, 95),
    ProbabilityBand("Almost certain", 95, 99),
]


# --------------------------------------------------------------------------- #
# Intelligence-concepts glossary (shared across every role)
# --------------------------------------------------------------------------- #
CONCEPTS: list[Concept] = [
    Concept(
        slug="intel-levels",
        term="Intelligence levels",
        category="Foundations",
        body=(
            "Every report and requirement is classified at one of three levels, "
            "following the traditional intelligence model. The level describes the "
            "audience and time-horizon of the product, and it drives dissemination: "
            "a stakeholder receives a published report when its level matches their "
            "preferred level (or they have set no preference)."
        ),
        points=[
            "STRATEGIC — big-picture, decision-maker intelligence over a long horizon.",
            "TACTICAL — threat-focused analysis for defenders and team leads.",
            "OPERATIONAL — immediate, action-oriented intelligence for a live situation.",
        ],
    ),
    Concept(
        slug="tlp",
        term="Traffic Light Protocol (TLP 2.0)",
        category="Foundations",
        body=(
            "TLP is the handling marking on a report. In Iceberg it is a display "
            "marking and a dissemination-routing input — it does NOT gate in-portal "
            "read access (any authenticated user may browse published reports). On "
            "publish, only reports at or below the configured broadcast ceiling "
            "(default TLP:AMBER) are pushed to stakeholder feeds; TLP:RED and "
            "TLP:AMBER+STRICT are withheld from automatic dissemination."
        ),
        points=[
            "TLP:RED — named recipients only; never disseminated automatically.",
            "TLP:AMBER+STRICT — limited to the recipient's organisation.",
            "TLP:AMBER — limited sharing on a need-to-know basis.",
            "TLP:GREEN — shareable within the community.",
            "TLP:CLEAR — no restriction on sharing.",
        ],
    ),
    Concept(
        slug="notebooks",
        term="Collection notebooks",
        category="Collection",
        body=(
            "Collection in Iceberg is notebook-based. A notebook is a per-topic "
            "workspace that holds everything gathered on that topic — sources, notes, "
            "attachments, figures and Diamond Model assessments — and is where one or "
            "more reports are authored from that material. Notebooks and their contents "
            "are writer-only: read-only stakeholders never see them and consume only the "
            "finished reports."
        ),
        points=[
            "Sources — references you cite, each carrying a reliability grade.",
            "Notes — free-form working analysis.",
            "Attachments — uploaded reference files, listed in a report's PDF appendix.",
            "Figures & Diamond Models — embedded inline in reports via tokens.",
        ],
    ),
    Concept(
        slug="source-grading",
        term="Source reliability grading (Admiralty / NATO)",
        category="Collection",
        body=(
            "Sources collected in a notebook carry a two-part Admiralty grade shown "
            "as a compact chip (e.g. B2). Grading can be suggested automatically — "
            "Iceberg safely fetches public URLs and optionally calls a configured LLM, "
            "falling back to a local heuristic — or set manually by an analyst. A "
            "source you just added may briefly show a 'Grading…' chip while the "
            "background grade resolves."
        ),
        points=[
            "Reliability A–F — A is completely reliable, F means it cannot be judged.",
            "Credibility 1–6 — 1 is confirmed, 6 means it cannot be judged.",
            "Provenance — UNGRADED, AUTO (suggested), or MANUAL (analyst override).",
        ],
    ),
    Concept(
        slug="diamond-model",
        term="Diamond Model of Intrusion Analysis",
        category="Analytic models",
        body=(
            "A Diamond Model assessment captures an intrusion's four core features and "
            "an analytic confidence, held against a notebook. Embed it inline in a "
            "report by writing a [[diamond:ID]] token in the body — it renders as a "
            "diagram in the web view, the live preview, and the PDF. Tokens are "
            "notebook-scoped; an unknown or cross-notebook id degrades to a notice."
        ),
        points=[
            "Adversary ↔ Victim — the socio-political axis.",
            "Capability ↔ Infrastructure — the technical axis.",
            "Confidence — an ordinal Low/Moderate/High assessment.",
        ],
    ),
    Concept(
        slug="ach-model",
        term="Analysis of Competing Hypotheses (ACH)",
        category="Analytic models",
        body=(
            "ACH (Heuer) adjudicates a key intelligence question by scoring a matrix of "
            "competing hypotheses against the available evidence. You rate how consistent "
            "each piece of evidence is with each hypothesis; the diagnostic signal is "
            "*inconsistency* — evidence never confirms a hypothesis, it only weakens the "
            "ones it contradicts — so the hypothesis carrying the fewest weighted "
            "inconsistencies is the most tenable. Held against a notebook and embedded "
            "inline in a report with an [[ach:ID]] token (notebook-scoped, like the "
            "Diamond Model), rendered as a matrix in the web view, live preview, and PDF."
        ),
        points=[
            "List the competing hypotheses across the top, the evidence down the side.",
            "Rate each cell: consistent (+/++), neutral, inconsistent (−/−−), or N/A.",
            "Least inconsistent = most tenable — the leading column is flagged.",
            "Disconfirm, don't confirm: focus on the evidence that rules hypotheses out.",
        ],
    ),
    Concept(
        slug="figures",
        term="Figures (embedded images)",
        category="Collection",
        body=(
            "Upload an image (PNG/JPEG/GIF) to a notebook's Figures collection, then "
            "embed it inline in a report with a [[figure:ID]] token — mirroring the "
            "Diamond Model token, including notebook-scoping. Unlike a plain attachment "
            "(a reference file listed in the PDF appendix), a figure is rendered inline "
            "in the report body; its bytes are baked into the published report, so "
            "readers see it without access to the notebook's collection material."
        ),
    ),
    Concept(
        slug="icd-203",
        term="Structured judgements (ICD 203)",
        category="Tradecraft",
        body=(
            "Reports carry optional structured-judgement scaffolding rendered as "
            "discrete sections in the web view and PDF. Lead with the Key Judgements: "
            "they are the bottom line up front (BLUF), and they are the only content of "
            "the brief PDF formats."
        ),
        points=[
            "Key Judgements — the BLUF; your core analytic conclusions.",
            "Key Assumptions — the assumptions the judgements rest on.",
            "Intelligence Gaps — what you do not know and could not collect.",
        ],
    ),
    Concept(
        slug="estimative-language",
        term="Estimative language (ICD 203)",
        category="Tradecraft",
        body=(
            "ICD 203 keeps two expressions deliberately separate. Analytic "
            "confidence is how much faith you place in a judgement, given the "
            "sourcing and reasoning behind it — set it as the report's optional "
            "LOW / MODERATE / HIGH confidence marking, shown beside the TLP and "
            "status. Likelihood is the probability of the assessed event itself; "
            "express it in the report's prose using the standardised probability "
            "yardstick so 'likely' always means the same thing."
        ),
        points=[
            f"{b.term} — {b.range_label}." for b in PROBABILITY_YARDSTICK
        ],
    ),
    Concept(
        slug="lifecycle",
        term="Report lifecycle",
        category="Workflow",
        body=(
            "A report moves through a review workflow. The author submits their own "
            "draft; a reviewer (or admin) approves it, sends it back for rework, or "
            "publishes it. Publishing stamps the publication time, triggers "
            "dissemination, and makes the report immutable — tags can still be edited "
            "afterwards, since classification is revised retrospectively."
        ),
        points=[
            "DRAFT — the author's working copy.",
            "IN_REVIEW — submitted, awaiting a reviewer.",
            "APPROVED — cleared for publication.",
            "PUBLISHED — final, immutable, and disseminated.",
        ],
    ),
    Concept(
        slug="requirements",
        term="Requirements (PIR / GIR / RFI) & the tasking board",
        category="Workflow",
        body=(
            "Stakeholders submit intelligence requirements with a kind and a "
            "priority. The kind records what is being asked: a PIR (Priority "
            "Intelligence Requirement) is leadership-designated, tied to a decision "
            "and time-bound; a GIR (General Intelligence Requirement) is standing "
            "baseline coverage; an RFI (Request for Information) is an ad-hoc, "
            "one-off question. Analysts, reviewers and admins see the aggregated "
            "tasking board — grouped by status — and drive each requirement's "
            "status. Traceability is established by linking the reports that satisfy "
            "a requirement and the notebooks that address it."
        ),
        points=[
            "Kinds — PIR (decision-tied, time-bound), GIR (standing), RFI (ad-hoc). "
            "A PIR adds a decision-context note and a review-by date.",
            "Board ordering blends urgency and kind: a PIR is treated as at least "
            "High priority so it leads standing/ad-hoc work, but a genuine Critical "
            "item of any kind still tops its column — urgent work is never buried.",
            "The PIR coverage panel flags PIRs with no linked report or notebook "
            "(collection gaps) and PIRs past their review-by date (overdue).",
            "Statuses — OPEN, IN_PROGRESS, SATISFIED, CLOSED.",
            "Only stakeholders/admins create requirements; only analysts/reviewers/"
            "admins change status and create links.",
        ],
    ),
    Concept(
        slug="dissemination",
        term="Dissemination & the feed",
        category="Workflow",
        body=(
            "On publish, Iceberg matches stakeholders and delivers the report to their "
            "personal feed. A stakeholder matches when the report is within the TLP "
            "broadcast ceiling AND its intelligence level equals the stakeholder's "
            "preferred level (or they have set no preference). Feed delivery is "
            "recorded immediately; an email notification follows in the background."
        ),
    ),
    Concept(
        slug="tags",
        term="Tag taxonomy & search",
        category="Knowledge layer",
        body=(
            "Reports are classified with a controlled tag taxonomy. The vocabulary is "
            "admin-curated — analysts select from it, they do not create terms. Tags "
            "power faceted full-text search across reports; stakeholders only ever "
            "match published reports."
        ),
        points=[
            "Kinds — Actor, Campaign, Malware, Technique (ATT&CK), Sector, Topic.",
            "Retired tags stay on historical reports but are no longer offered.",
        ],
    ),
    Concept(
        slug="products",
        term="Report products (PDF formats)",
        category="Knowledge layer",
        body=(
            "A published report can be rendered on demand to a PDF in one of three "
            "formats via Typst. The brief formats are Key-Judgements-only summaries."
        ),
        points=[
            "FULL — masthead, Key Judgements, body, caveats, and source appendix.",
            "EXEC_BRIEF / ONE_PAGER — Key-Judgements-only (body and caveats omitted).",
        ],
    ),
    Concept(
        slug="program-maturity",
        term="Program maturity & effectiveness (CTI-CMM)",
        category="Knowledge layer",
        body=(
            "The Maturity dashboard (writers only) measures the health of the "
            "intelligence programme itself, not individual reports — production, "
            "requirement coverage, dissemination reach, and tradecraft adoption, all "
            "derived from existing data. On top sits an indicative CTI-CMM-style "
            "rollup: a few capability dimensions scored CTI0 (Pre-foundational) to "
            "CTI3 (Leading). It is evidence to inform a formal CTI-CMM "
            "self-assessment, not a substitute for one."
        ),
        points=[
            "CTI0 Pre-foundational · CTI1 Foundational · CTI2 Advanced · CTI3 Leading.",
            "Pure aggregation over reports, requirements, dissemination and sources.",
            "Tradecraft adoption = share of published reports applying the analytic "
            "standards (source grading, judgements, confidence, analytic models, ATT&CK).",
        ],
    ),
]

# --------------------------------------------------------------------------- #
# Per-role guides
# --------------------------------------------------------------------------- #
ROLE_GUIDES: list[RoleGuide] = [
    RoleGuide(
        role=Role.ANALYST,
        tagline=(
            "You collect raw material and turn it into finished intelligence products."
        ),
        workflow=[
            "Open or create a topic notebook to hold your collection.",
            "Gather sources (graded for reliability), notes, attachments, figures and "
            "Diamond Model assessments inside it.",
            "Author a report from that material: write the body, lead with your Key "
            "Judgements, cite sources, and embed diagrams, figures and ACH matrices with "
            "[[diamond:ID]], [[figure:ID]] and [[ach:ID]] tokens.",
            "Classify the report with tags and tick the requirements it satisfies.",
            "Submit your draft for review.",
        ],
        can=[
            "Create and edit notebooks and all their collection material.",
            "Author reports and use the live preview.",
            "Drive requirement status and link reports/notebooks to requirements.",
            "Re-tag reports even after they are published.",
        ],
        cannot=[
            "Approve or publish a report — that is a reviewer's call.",
            "Edit a report once it has been published (tags aside).",
            "Create or retire taxonomy tags.",
        ],
        key_screens=[
            HelpLink("Dashboard", "/"),
            HelpLink("Reports", "/reports"),
            HelpLink("Tasking board", "/requirements"),
        ],
        concepts=[
            "notebooks",
            "source-grading",
            "diamond-model",
            "figures",
            "icd-203",
            "estimative-language",
            "tags",
            "lifecycle",
        ],
    ),
    RoleGuide(
        role=Role.REVIEWER,
        tagline=(
            "You are the quality gate — you review analysts' drafts and decide what "
            "gets published."
        ),
        workflow=[
            "Pick up a report that has been submitted for review.",
            "Read it against the source grades, Key Assumptions and Intelligence Gaps.",
            "Approve it, or send it back to the author for rework with your reasons.",
            "Publish an approved report — this disseminates it and locks it.",
        ],
        can=[
            "Do everything an analyst does (author and collect).",
            "Approve, send back, and publish reports.",
            "Drive requirement status and traceability.",
        ],
        cannot=[
            "Edit a report after publishing it (it becomes immutable).",
            "Create or retire taxonomy tags.",
        ],
        key_screens=[
            HelpLink("Reports", "/reports"),
            HelpLink("Tasking board", "/requirements"),
        ],
        concepts=[
            "lifecycle",
            "icd-203",
            "estimative-language",
            "source-grading",
            "tlp",
            "dissemination",
        ],
    ),
    RoleGuide(
        role=Role.STAKEHOLDER,
        tagline=(
            "You are a consumer of finished intelligence — you state what you need and "
            "receive products tailored to it."
        ),
        workflow=[
            "Set your preferred intelligence level so the right products reach you.",
            "Submit requirements (PIRs / RFIs) describing what you need to know.",
            "Read the reports delivered to your feed as they are published.",
            "Browse and search all published reports at any time.",
        ],
        can=[
            "Submit, edit and delete your own requirements.",
            "Set a preferred intelligence level.",
            "Read your feed and all published reports.",
        ],
        cannot=[
            "See notebooks or any collection material (sources, notes, figures).",
            "See unpublished reports.",
            "Change a requirement's status — analysts do that.",
        ],
        key_screens=[
            HelpLink("Feed", "/feed"),
            HelpLink("My requirements", "/requirements"),
            HelpLink("Preferences", "/preferences"),
        ],
        concepts=["requirements", "dissemination", "intel-levels", "tlp"],
    ),
    RoleGuide(
        role=Role.ADMIN,
        tagline="You administer the platform and can perform every role's actions.",
        workflow=[
            "Curate the controlled tag taxonomy that analysts classify reports with.",
            "Step into any analyst, reviewer or stakeholder action as needed.",
            "Oversee the tasking board and manage any requirement.",
        ],
        can=[
            "Everything analysts, reviewers and stakeholders can do.",
            "Create, edit and retire taxonomy tags.",
            "Edit or delete any requirement.",
        ],
        key_screens=[
            HelpLink("Taxonomy", "/admin/tags"),
            HelpLink("Reports", "/reports"),
            HelpLink("Tasking board", "/requirements"),
        ],
        concepts=["tags", "lifecycle", "requirements", "tlp", "dissemination"],
    ),
]

_GUIDES_BY_ROLE = {g.role: g for g in ROLE_GUIDES}


def guide_for(role: Role) -> RoleGuide:
    """Return the guide for ``role``, defaulting to the analyst guide."""
    return _GUIDES_BY_ROLE.get(role, _GUIDES_BY_ROLE[Role.ANALYST])
