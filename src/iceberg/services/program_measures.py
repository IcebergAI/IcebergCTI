"""Program measures: requirement coverage, collection gaps, product usefulness.

A **pure derivation** over existing records — no model, no migration — feeding
the writer-only ``/measures`` view (#310). It answers programme questions, never
questions about people: there is no per-analyst output here, and nothing that
could be read as a leaderboard.

Four rules shape every number:

* **Each measure is defined.** :data:`MEASURES` carries the definition and the
  source records for every value the page shows, and the page renders them, so a
  reader never has to guess what a percentage counts.
* **Authorization comes first.** Everything is computed from the products the
  *actor* may read (``reports.ensure_visible``'s rule, applied in SQL), so a
  restricted product cannot show up in a total, a trend, or a drill-down.
* **Small groups are suppressed.** Any breakdown resting on fewer than
  ``ICEBERG_MEASURES_MIN_GROUP`` distinct responding stakeholders is reported as
  suppressed rather than shown, so an aggregate can't identify one person's
  answer.
* **Silence is not a bad review.** Usefulness and satisfaction rates are always
  taken over *responses*, never over deliveries; the share of deliveries that
  drew any response is reported separately as its own measure.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import (
    DisseminationEvent,
    ProductFeedback,
    ProductUsefulness,
    Report,
    ReportRequirement,
    ReportStatus,
    Requirement,
    RequirementKind,
    RequirementStatus,
    RfiSatisfaction,
    Role,
    User,
    utcnow,
)
from . import reports as reports_service

WINDOWS = (30, 90, 365, 0)  # 0 = all time
# Requirement age buckets, in days.
_AGE_BUCKETS = ((7, "0–7 days"), (30, "8–30 days"), (90, "31–90 days"), (0, "over 90 days"))


@dataclass(frozen=True)
class MeasureDefinition:
    """What one published number means, and which records it is taken from."""

    key: str
    label: str
    definition: str
    source: str


MEASURES: tuple[MeasureDefinition, ...] = (
    MeasureDefinition(
        "requirement_age",
        "Requirement age",
        "Days between a requirement being raised and now, for requirements not "
        "yet SATISFIED or CLOSED. Bucketed; the median is over the same set.",
        "Requirement.created_at, Requirement.status",
    ),
    MeasureDefinition(
        "requirement_status",
        "Requirement status mix",
        "Count of requirements in each status, split by kind (PIR/GIR/RFI).",
        "Requirement.status, Requirement.kind",
    ),
    MeasureDefinition(
        "task_coverage",
        "Tasked coverage",
        "Share of open requirements with at least one linked notebook — the "
        "requirement has been taken up as collection work.",
        "NotebookRequirement",
    ),
    MeasureDefinition(
        "source_coverage",
        "Source coverage",
        "Share of open requirements whose linked notebooks hold at least one "
        "source — collection has actually produced material.",
        "NotebookRequirement, Source",
    ),
    MeasureDefinition(
        "product_linkage",
        "Product linkage",
        "Share of published products in the window linked to at least one "
        "requirement.",
        "ReportRequirement, Report.status, Report.published_at",
    ),
    MeasureDefinition(
        "neglected_requirements",
        "Neglected requirements",
        "Open requirements with no linked notebook and no linked product, "
        "oldest first. These are the collection needs nothing is answering.",
        "Requirement, NotebookRequirement, ReportRequirement",
    ),
    MeasureDefinition(
        "declared_gaps",
        "Declared collection gaps",
        "Published products in the window that record an Intelligence Gaps "
        "section, and the gaps they declare.",
        "Report.intelligence_gaps",
    ),
    MeasureDefinition(
        "audience_reach",
        "Audience reach",
        "Deliveries, distinct stakeholders reached, and the share of "
        "deliveries opened, for products in the window.",
        "DisseminationEvent.created_at, DisseminationEvent.read_at",
    ),
    MeasureDefinition(
        "response_rate",
        "Feedback response rate",
        "Share of deliveries in the window that drew a feedback response. "
        "Reported separately so that silence is never counted as a verdict.",
        "ProductFeedback, DisseminationEvent",
    ),
    MeasureDefinition(
        "usefulness",
        "Product usefulness",
        "Distribution of usefulness ratings, and the useful-or-better share, "
        "over responses received — never over deliveries. Both are suppressed "
        "below the configured minimum group size; a rate over a small group "
        "would state an individual's rating just as the distribution would.",
        "ProductFeedback.usefulness",
    ),
    MeasureDefinition(
        "satisfaction",
        "Requirement satisfaction",
        "Share of RFI-satisfaction verdicts recorded as MET, over responses "
        "that carried a verdict. Suppressed below the configured minimum group "
        "size, counted over the stakeholders who gave a verdict: over a small "
        "group the rate would state an individual's verdict.",
        "ProductFeedback.satisfaction",
    ),
    MeasureDefinition(
        "usefulness_trend",
        "Usefulness trend",
        "Useful-or-better share per calendar month over the window, computed "
        "over that month's responses. A month below the minimum group size is "
        "suppressed rather than plotted.",
        "ProductFeedback.created_at, ProductFeedback.usefulness",
    ),
    MeasureDefinition(
        "feedback_themes",
        "Feedback themes",
        "Counts of recurring terms in feedback comments, over the window. "
        "Suppressed below the minimum group size; comments are never shown "
        "attributed.",
        "ProductFeedback.comment",
    ),
)

def definitions() -> list[dict]:
    return [asdict(measure) for measure in MEASURES]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pct(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _min_group() -> int:
    return max(0, get_settings().measures_min_group)


def _suppressed(contributors: int) -> bool:
    """True when a breakdown rests on too few responding stakeholders to show."""

    minimum = _min_group()
    return bool(minimum) and contributors < minimum


def _group(values: dict, contributors: int) -> dict:
    """Wrap a breakdown with its suppression state, so a template never has to
    decide whether it is safe to render."""

    if _suppressed(contributors):
        return {"suppressed": True, "contributors": contributors, "values": {}}
    return {"suppressed": False, "contributors": contributors, "values": values}


def _rate(value: float, contributors: int) -> dict:
    """A single rate carries the same suppression as a breakdown would.

    A rate is a summary of the same responses the distribution summarises, so
    hiding one and publishing the other hides nothing: over a single respondent
    a "satisfaction rate" of 1.0 *is* that stakeholder's verdict, and a
    "useful rate" of 0.0 is their rating. Suppression therefore follows the
    number of people the value rests on, not the shape it is reported in.
    """

    if _suppressed(contributors):
        return {"suppressed": True, "contributors": contributors, "value": None}
    return {"suppressed": False, "contributors": contributors, "value": value}


def visible_report_ids(session: Session, user: User) -> set[int]:
    """Ids of the products this actor may read — ``ensure_visible`` in SQL.

    Everything on the page is filtered through this set, so a measure can never
    total, trend or link to a product the reader has no access to.
    """

    statement = select(col(Report.id))
    if user.role == Role.STAKEHOLDER:
        if user.id is None:
            return set()
        statement = statement.where(
            Report.status == ReportStatus.PUBLISHED,
            reports_service.stakeholder_visibility_clause(user.id),
        )
    return {report_id for report_id in session.exec(statement).all() if report_id is not None}


def _window_start(window_days: int) -> datetime | None:
    return utcnow() - timedelta(days=window_days) if window_days else None


def _in_window(value: datetime | None, start: datetime | None) -> bool:
    if start is None:
        return True
    moment = _aware(value)
    return moment is not None and moment >= start


# --------------------------------------------------------------------------- #
# Measure groups
# --------------------------------------------------------------------------- #
def _requirement_measures(
    session: Session, *, kind: RequirementKind | None, visible: set[int]
) -> dict:
    requirements = list(session.exec(select(Requirement)).all())
    if kind is not None:
        requirements = [r for r in requirements if RequirementKind(r.kind) == kind]
    open_states = {RequirementStatus.OPEN, RequirementStatus.IN_PROGRESS}
    outstanding = [r for r in requirements if RequirementStatus(r.status) in open_states]

    now = utcnow()
    ages = [max(0, (now - (_aware(r.created_at) or now)).days) for r in outstanding]
    buckets = {label: 0 for _, label in _AGE_BUCKETS}
    for age in ages:
        for limit, label in _AGE_BUCKETS:
            if not limit or age <= limit:
                buckets[label] += 1
                break

    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for requirement in requirements:
        by_status[str(requirement.status)] = by_status.get(str(requirement.status), 0) + 1
        by_kind[str(requirement.kind)] = by_kind.get(str(requirement.kind), 0) + 1

    tasked = sum(1 for r in outstanding if r.notebooks)
    sourced = sum(
        1 for r in outstanding if any(notebook.sources for notebook in r.notebooks)
    )
    # A requirement nothing is answering: no collection work, and no product
    # this reader can see that claims to address it.
    neglected = [
        r
        for r in outstanding
        if not r.notebooks
        and not any(report.id in visible for report in r.reports)
    ]
    neglected.sort(key=lambda r: _aware(r.created_at) or now)
    return {
        "total": len(requirements),
        "outstanding": len(outstanding),
        "median_age_days": round(median(ages), 1) if ages else 0.0,
        "age_buckets": buckets,
        "by_status": by_status,
        "by_kind": by_kind,
        "tasked_rate": _pct(tasked, len(outstanding)),
        "sourced_rate": _pct(sourced, len(outstanding)),
        "neglected": [
            {
                "id": r.id,
                "title": r.title,
                "kind": str(r.kind),
                "priority": str(r.priority),
                "age_days": max(0, (now - (_aware(r.created_at) or now)).days),
            }
            for r in neglected[:20]
        ],
        "neglected_total": len(neglected),
    }


def _product_measures(
    session: Session, *, start: datetime | None, visible: set[int]
) -> dict:
    published = [
        report
        for report in session.exec(
            select(Report).where(Report.status == ReportStatus.PUBLISHED)
        ).all()
        if report.id in visible and _in_window(report.published_at, start)
    ]
    linked_ids = set(session.exec(select(ReportRequirement.report_id)).all())
    linked = sum(1 for report in published if report.id in linked_ids)
    gaps = [report for report in published if report.intelligence_gaps.strip()]
    return {
        "published": len(published),
        "linked": linked,
        "linkage_rate": _pct(linked, len(published)),
        "declared_gap_rate": _pct(len(gaps), len(published)),
        "declared_gaps": [
            {"id": report.id, "title": report.title, "gaps": report.intelligence_gaps.strip()[:400]}
            for report in sorted(
                gaps, key=lambda r: _aware(r.published_at) or utcnow(), reverse=True
            )[:10]
        ],
    }


def _reach_measures(
    session: Session, *, start: datetime | None, visible: set[int]
) -> dict:
    events = [
        event
        for event in session.exec(select(DisseminationEvent)).all()
        if event.report_id in visible and _in_window(event.created_at, start)
    ]
    read = sum(1 for event in events if event.read_at is not None)
    stakeholders = {event.stakeholder_id for event in events}
    return {
        "deliveries": len(events),
        "stakeholders_reached": len(stakeholders),
        "read_rate": _pct(read, len(events)),
    }


_THEME_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "was", "were", "are", "not",
        "but", "its", "their", "they", "which", "very", "would", "could", "have",
        "has", "had", "you", "your", "our", "from", "about", "more", "than",
        "report", "product",
    }
)


def _feedback_measures(
    session: Session, *, start: datetime | None, visible: set[int]
) -> dict:
    responses = [
        row
        for row in session.exec(select(ProductFeedback)).all()
        if row.report_id in visible and _in_window(row.created_at, start)
    ]
    deliveries = [
        event
        for event in session.exec(select(DisseminationEvent)).all()
        if event.report_id in visible and _in_window(event.created_at, start)
    ]
    respondents = {row.stakeholder_id for row in responses}

    distribution: dict[str, int] = {value.value: 0 for value in ProductUsefulness}
    for row in responses:
        distribution[ProductUsefulness(row.usefulness).value] += 1
    useful = sum(
        1
        for row in responses
        if row.usefulness in (ProductUsefulness.USEFUL, ProductUsefulness.HIGHLY_USEFUL)
    )
    verdicts = [row for row in responses if row.satisfaction is not None]
    met = sum(1 for row in verdicts if row.satisfaction == RfiSatisfaction.MET)

    themes: dict[str, int] = {}
    for row in responses:
        for word in {
            token.strip(".,;:!?()'\"").lower()
            for token in row.comment.split()
            if len(token) > 3
        }:
            if word and word not in _THEME_STOPWORDS and word.isalpha():
                themes[word] = themes.get(word, 0) + 1
    top_themes = dict(sorted(themes.items(), key=lambda item: (-item[1], item[0]))[:12])

    trend: dict[str, dict] = {}
    by_month: dict[str, list[ProductFeedback]] = {}
    for row in responses:
        moment = _aware(row.created_at)
        if moment is None:
            continue
        by_month.setdefault(moment.strftime("%Y-%m"), []).append(row)
    for month, rows in sorted(by_month.items()):
        contributors = len({row.stakeholder_id for row in rows})
        month_useful = sum(
            1
            for row in rows
            if row.usefulness
            in (ProductUsefulness.USEFUL, ProductUsefulness.HIGHLY_USEFUL)
        )
        trend[month] = _group(
            {"responses": len(rows), "useful_rate": _pct(month_useful, len(rows))},
            contributors,
        )

    return {
        # Reported on its own: a delivery that drew no response is not a verdict.
        "responses": len(responses),
        "deliveries": len(deliveries),
        "response_rate": _pct(len(responses), len(deliveries)),
        "respondents": len(respondents),
        # Every rate below is over responses, never over deliveries.
        "usefulness": _group(distribution, len(respondents)),
        "useful_rate": _rate(_pct(useful, len(responses)), len(respondents)),
        "verdicts": len(verdicts),
        # Satisfaction rests on the people who gave a verdict, which can be a
        # smaller group than everyone who responded.
        "satisfaction_rate": _rate(
            _pct(met, len(verdicts)), len({row.stakeholder_id for row in verdicts})
        ),
        "themes": _group(top_themes, len(respondents)),
        "trend": trend,
    }


def program_measures(
    session: Session,
    *,
    user: User,
    window_days: int = 90,
    kind: RequirementKind | None = None,
) -> dict:
    """The whole programme picture for one actor, one window and one kind filter."""

    window_days = window_days if window_days in WINDOWS else 90
    start = _window_start(window_days)
    visible = visible_report_ids(session, user)
    return {
        "window_days": window_days,
        "kind": str(kind) if kind else "",
        "min_group": _min_group(),
        "requirements": _requirement_measures(session, kind=kind, visible=visible),
        "products": _product_measures(session, start=start, visible=visible),
        "reach": _reach_measures(session, start=start, visible=visible),
        "feedback": _feedback_measures(session, start=start, visible=visible),
        "definitions": definitions(),
    }


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def export_rows(measures: dict) -> list[tuple[str, str, str]]:
    """Flatten the measures into ``(measure, item, value)`` rows for CSV.

    A suppressed breakdown exports the word ``suppressed`` rather than its
    values — the export must not become the way around the privacy control.
    """

    rows: list[tuple[str, str, str]] = [
        ("window_days", "", str(measures["window_days"])),
        ("requirement_kind", "", measures["kind"] or "all"),
        ("minimum_group_size", "", str(measures["min_group"])),
    ]
    requirements = measures["requirements"]
    rows += [
        ("requirements_total", "", str(requirements["total"])),
        ("requirements_outstanding", "", str(requirements["outstanding"])),
        ("requirement_median_age_days", "", str(requirements["median_age_days"])),
        ("task_coverage", "", str(requirements["tasked_rate"])),
        ("source_coverage", "", str(requirements["sourced_rate"])),
        ("neglected_requirements", "", str(requirements["neglected_total"])),
    ]
    rows += [("requirement_age", label, str(count)) for label, count in requirements["age_buckets"].items()]
    rows += [("requirement_status", name, str(count)) for name, count in sorted(requirements["by_status"].items())]

    products = measures["products"]
    rows += [
        ("published_products", "", str(products["published"])),
        ("product_linkage", "", str(products["linkage_rate"])),
        ("declared_gap_rate", "", str(products["declared_gap_rate"])),
    ]
    reach = measures["reach"]
    rows += [
        ("deliveries", "", str(reach["deliveries"])),
        ("stakeholders_reached", "", str(reach["stakeholders_reached"])),
        ("read_rate", "", str(reach["read_rate"])),
    ]
    feedback = measures["feedback"]
    rows += [
        ("responses", "", str(feedback["responses"])),
        ("response_rate", "", str(feedback["response_rate"])),
    ]
    for key in ("useful_rate", "satisfaction_rate"):
        rate = feedback[key]
        rows.append(
            (key, "", "suppressed" if rate["suppressed"] else str(rate["value"]))
        )
    for key in ("usefulness", "themes"):
        group = feedback[key]
        if group["suppressed"]:
            rows.append((key, "", "suppressed"))
            continue
        rows += [(key, name, str(count)) for name, count in group["values"].items()]
    for month, group in feedback["trend"].items():
        if group["suppressed"]:
            rows.append(("usefulness_trend", month, "suppressed"))
            continue
        rows.append(("usefulness_trend", month, str(group["values"]["useful_rate"])))
    return rows


def export_csv(measures: dict) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(("measure", "item", "value"))
    writer.writerows(export_rows(measures))
    return buffer.getvalue()
