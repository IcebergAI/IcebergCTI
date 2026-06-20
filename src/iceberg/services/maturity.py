"""CTI program maturity & effectiveness dashboard (roadmap backlog H).

A **pure derivation** over existing data — no model, no migration — feeding the
writer-only ``/maturity`` view. Mirrors the aggregation-service convention of
``services/attack.coverage_matrix`` / ``services/requirements.pir_coverage``:
take a session, return plain dicts/lists structured for the template.

It surfaces four program-health groups (production, requirement coverage,
dissemination reach, tradecraft adoption) plus an **indicative** CTI-CMM-style
maturity rollup. CTI-CMM (https://cti-cmm.org/) is normally a *manual*
self-assessment across stakeholder-aligned domains scored CTI0 (Pre-foundational)
→ CTI1 (Foundational) → CTI2 (Advanced) → CTI3 (Leading); here we auto-derive a
handful of capability dimensions from current data via thresholds. This is
evidence to inform a self-assessment, **not a substitute** for one — hence the
``disclaimer`` carried in the payload.
"""

from datetime import date, timedelta
from statistics import median

from sqlmodel import Session, select

from ..config import get_settings
from ..models import (
    DisseminationEvent,
    IntelLevel,
    Report,
    ReportStatus,
    Requirement,
    RequirementKind,
    RequirementStatus,
    SourceGradingOrigin,
    TagKind,
    TLP,
    is_disseminable,
    tlp_label,
)
from . import feedback as feedback_service
from . import requirements as req_service

# CTI-CMM maturity-level names (CTI0..CTI3). Derived, indicative only.
_LEVEL_LABELS = ("Pre-foundational", "Foundational", "Advanced", "Leading")

_DISCLAIMER = (
    "Indicative rollup derived from current Iceberg data — evidence to inform a "
    "CTI-CMM self-assessment, not a substitute for one."
)

# Tokens that mark a report as embedding a structured analytic model/figure.
_ANALYTIC_TOKENS = ("[[ach:", "[[diamond:", "[[figure:", "[[attack]]")


def _pct(part: int, whole: int) -> float:
    """Safe ratio in [0, 1] (0 when the denominator is empty)."""
    return part / whole if whole else 0.0


def _level(value: float, t1: float, t2: float, t3: float) -> int:
    """Bucket a 0–1 metric into a CTI-CMM level 0..3 (highest threshold met).

    ``t1`` is a small positive floor so *any* activity clears CTI0; t2/t3 mark
    the Advanced / Leading bands.
    """
    if value >= t3:
        return 3
    if value >= t2:
        return 2
    if value >= t1:
        return 1
    return 0


def _dimension(name: str, blurb: str, value: float, t2: float, t3: float) -> dict:
    level = _level(value, 0.01, t2, t3)
    return {
        "dimension": name,
        "blurb": blurb,
        "value": value,
        "metric_pct": round(value * 100),
        "level": level,
        "label": _LEVEL_LABELS[level],
    }


def _production(reports: list[Report]) -> dict:
    today = date.today()
    by_status = {s: 0 for s in ReportStatus}
    for r in reports:
        by_status[ReportStatus(r.status)] += 1

    published = [r for r in reports if r.status == ReportStatus.PUBLISHED]
    pub_total = len(published)

    def _published_since(days: int) -> int:
        cutoff = today - timedelta(days=days)
        return sum(
            1 for r in published if r.published_at and r.published_at.date() >= cutoff
        )

    ttp_days = [
        (r.published_at - r.created_at).days
        for r in published
        if r.published_at and r.created_at
    ]
    by_level = {lv: 0 for lv in IntelLevel}
    for r in published:
        by_level[IntelLevel(r.intel_level)] += 1
    reviewed = sum(1 for r in published if r.reviewer_id is not None)

    in_flight = (
        by_status[ReportStatus.DRAFT]
        + by_status[ReportStatus.IN_REVIEW]
        + by_status[ReportStatus.APPROVED]
    )
    return {
        "total": len(reports),
        "by_status": [
            {"status": s.value, "count": by_status[s]} for s in ReportStatus
        ],
        "in_flight": in_flight,
        "published_total": pub_total,
        "published_30d": _published_since(30),
        "published_90d": _published_since(90),
        "median_days_to_publish": (round(median(ttp_days), 1) if ttp_days else None),
        "by_intel_level": [
            {"level": lv.value, "count": by_level[lv]} for lv in IntelLevel
        ],
        "reviewer_engagement": _pct(reviewed, pub_total),
    }


def _requirements(session: Session) -> dict:
    reqs = list(session.exec(select(Requirement)).all())
    active = {RequirementStatus.OPEN, RequirementStatus.IN_PROGRESS}

    by_kind = {k: 0 for k in RequirementKind}
    by_status = {s: 0 for s in RequirementStatus}
    for r in reqs:
        by_kind[RequirementKind(r.kind)] += 1
        by_status[RequirementStatus(r.status)] += 1

    # Satisfaction over the requirements that are still "live" (exclude CLOSED —
    # an administratively closed item is neither a win nor an open obligation).
    non_closed = [r for r in reqs if r.status != RequirementStatus.CLOSED]
    satisfied = sum(1 for r in non_closed if r.status == RequirementStatus.SATISFIED)

    # Collection coverage: an active requirement of *any* kind is "covered" once
    # a report or notebook is traced to it (generalises pir_coverage's gap test).
    active_reqs = [r for r in reqs if r.status in active]
    linked = [r for r in active_reqs if r.reports or r.notebooks]

    pir = req_service.pir_coverage(session)
    return {
        "total": len(reqs),
        "by_kind": [{"kind": k.value, "count": by_kind[k]} for k in RequirementKind],
        "by_status": [
            {"status": s.value, "count": by_status[s]} for s in RequirementStatus
        ],
        "satisfaction_rate": _pct(satisfied, len(non_closed)),
        "active_total": len(active_reqs),
        "linked_active": len(linked),
        "coverage_rate": _pct(len(linked), len(active_reqs)),
        "pir_gaps": len(pir["gaps"]),
        "pir_overdue": len(pir["overdue"]),
    }


def _dissemination(session: Session, reports: list[Report]) -> dict:
    events = list(session.exec(select(DisseminationEvent)).all())
    total = len(events)
    read = sum(1 for e in events if e.read_at is not None)
    reached = {e.stakeholder_id for e in events}

    published = [r for r in reports if r.status == ReportStatus.PUBLISHED]
    published_at = {r.id: r.published_at for r in published}
    lags = [
        (e.created_at - published_at[e.report_id]).days
        for e in events
        if published_at.get(e.report_id) and e.created_at
    ]

    # Reports finished but held back from broadcast by the TLP ceiling.
    try:
        max_tlp = TLP(get_settings().dissemination_max_tlp)
    except ValueError:
        max_tlp = TLP.AMBER
    withheld = sum(
        1 for r in published if not is_disseminable(TLP(r.tlp), max_tlp)
    )
    return {
        "events_total": total,
        "read_count": read,
        "read_rate": _pct(read, total),
        "stakeholders_reached": len(reached),
        "median_lag_days": (round(median(lags), 1) if lags else None),
        "withheld_count": withheld,
        "withheld_rate": _pct(withheld, len(published)),
        "max_tlp_label": tlp_label(max_tlp),
        # Intelligence-cycle feedback loop (backlog D): the return signal.
        "feedback": feedback_service.feedback_effectiveness(session),
    }


def _tradecraft(reports: list[Report]) -> dict:
    published = [r for r in reports if r.status == ReportStatus.PUBLISHED]
    n = len(published)

    def _share(pred) -> int:
        return sum(1 for r in published if pred(r))

    def _has_graded_source(r: Report) -> bool:
        return any(
            SourceGradingOrigin(s.grading_origin) != SourceGradingOrigin.UNGRADED
            for s in r.cited_sources
        )

    def _has_token(r: Report) -> bool:
        return any(tok in (r.body_md or "") for tok in _ANALYTIC_TOKENS)

    def _has_technique(r: Report) -> bool:
        return any(t.kind == TagKind.TECHNIQUE for t in r.tags)

    checks = [
        ("Graded sources", _has_graded_source),
        ("Key judgements", lambda r: bool(r.key_judgements.strip())),
        ("Key assumptions", lambda r: bool(r.key_assumptions.strip())),
        ("Intelligence gaps", lambda r: bool(r.intelligence_gaps.strip())),
        ("Analytic confidence", lambda r: r.analytic_confidence is not None),
        ("Embedded analytic model", _has_token),
        ("ATT&CK techniques", _has_technique),
    ]
    metrics = [
        {"label": label, "count": (c := _share(pred)), "rate": _pct(c, n)}
        for label, pred in checks
    ]
    # Overall adoption share = mean of the per-practice rates.
    adoption = sum(m["rate"] for m in metrics) / len(metrics) if metrics else 0.0
    return {
        "published_total": n,
        "metrics": metrics,
        "adoption_share": adoption,
    }


def _alignment_value(requirements: dict, feedback: dict) -> float:
    """Stakeholder alignment = collection coverage, blended with satisfaction once
    feedback verdicts exist (so the loop's return signal lifts/lowers the score,
    but absence of feedback never penalises a covered program)."""
    coverage = requirements["coverage_rate"]
    if feedback["verdicts"]:
        return (coverage + feedback["satisfaction_rate"]) / 2
    return coverage


def _maturity(production: dict, requirements: dict, dissemination: dict,
              tradecraft: dict) -> dict:
    dims = [
        _dimension(
            "Analytic tradecraft",
            "Share of published products applying source grading, structured "
            "judgements, confidence and analytic models.",
            tradecraft["adoption_share"], 0.40, 0.75,
        ),
        _dimension(
            "Stakeholder alignment",
            "Active requirements traced to a report or notebook, blended with "
            "stakeholder satisfaction on delivered products.",
            _alignment_value(requirements, dissemination["feedback"]), 0.50, 0.80,
        ),
        _dimension(
            "Production discipline",
            "Published products carrying a reviewer (peer-reviewed tradecraft).",
            production["reviewer_engagement"]
            if production["published_total"] else 0.0,
            0.50, 0.85,
        ),
        _dimension(
            "Dissemination",
            "Delivered products opened by their stakeholder audience.",
            dissemination["read_rate"], 0.40, 0.70,
        ),
    ]
    avg = round(sum(d["level"] for d in dims) / len(dims)) if dims else 0
    return {
        "dimensions": dims,
        "overall": {"level": avg, "label": _LEVEL_LABELS[avg]},
        "disclaimer": _DISCLAIMER,
    }


def program_maturity(session: Session) -> dict:
    """Aggregate the CTI program maturity & effectiveness payload (backlog H).

    Writer-only by route gating; queries are unscoped because writers see all
    reports. Returns a dict of ``production`` / ``requirements`` /
    ``dissemination`` / ``tradecraft`` groups plus an indicative ``maturity``
    rollup.
    """
    reports = list(session.exec(select(Report)).all())

    production = _production(reports)
    requirements = _requirements(session)
    dissemination = _dissemination(session, reports)
    tradecraft = _tradecraft(reports)
    maturity = _maturity(production, requirements, dissemination, tradecraft)
    return {
        "production": production,
        "requirements": requirements,
        "dissemination": dissemination,
        "tradecraft": tradecraft,
        "maturity": maturity,
    }
