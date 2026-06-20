"""CTI program maturity & effectiveness dashboard (backlog H).

Covers the pure-derivation aggregation service (`services/maturity.program_maturity`)
— production / requirement-coverage / dissemination / tradecraft groups and the
indicative CTI-CMM rollup — plus the writer-only `/maturity` route gating and the
template render.
"""

from datetime import timedelta

from sqlmodel import Session

from iceberg.models import (
    AnalyticConfidence,
    DisseminationEvent,
    IntelLevel,
    Notebook,
    Report,
    ReportStatus,
    Requirement,
    RequirementKind,
    RequirementStatus,
    Role,
    Source,
    SourceCredibility,
    SourceGradingOrigin,
    SourceReliability,
    Tag,
    TagKind,
    TLP,
    User,
    utcnow,
)
from iceberg.services import maturity as maturity_service


# --------------------------------------------------------------------------- #
# Service aggregation
# --------------------------------------------------------------------------- #
def test_program_maturity_empty_db(engine):
    """No data → no exceptions, all rates 0, every CTI dimension at CTI0."""
    with Session(engine) as session:
        m = maturity_service.program_maturity(session)

    assert m["production"]["total"] == 0
    assert m["production"]["median_days_to_publish"] is None
    assert m["requirements"]["coverage_rate"] == 0.0
    assert m["dissemination"]["read_rate"] == 0.0
    assert m["tradecraft"]["adoption_share"] == 0.0
    assert m["maturity"]["overall"]["level"] == 0
    assert all(d["level"] == 0 for d in m["maturity"]["dimensions"])
    assert "self-assessment" in m["maturity"]["disclaimer"].lower()


def _seed(session: Session) -> None:
    author = User(email="a@x.io", display_name="Author", role=Role.ANALYST)
    reviewer = User(email="r@x.io", display_name="Reviewer", role=Role.REVIEWER)
    stake = User(email="s@x.io", display_name="Stake", role=Role.STAKEHOLDER)
    session.add_all([author, reviewer, stake])
    session.commit()

    nb = Notebook(title="nb", owner_id=author.id)
    session.add(nb)
    session.commit()

    technique = Tag(kind=TagKind.TECHNIQUE, label="Phishing", slug="t1566",
                    external_id="T1566", description="Initial Access")
    session.add(technique)
    session.commit()

    now = utcnow()

    # A fully-tradecrafted, reviewed, published report (graded source + token + tag).
    graded = Source(notebook_id=nb.id, title="src", reliability=SourceReliability.B,
                    credibility=SourceCredibility.PROBABLY_TRUE,
                    grading_origin=SourceGradingOrigin.MANUAL)
    session.add(graded)
    session.commit()
    rich = Report(
        notebook_id=nb.id, title="Rich", author_id=author.id, reviewer_id=reviewer.id,
        status=ReportStatus.PUBLISHED, intel_level=IntelLevel.STRATEGIC, tlp=TLP.GREEN,
        key_judgements="KJ", key_assumptions="KA", intelligence_gaps="IG",
        analytic_confidence=AnalyticConfidence.HIGH, body_md="See [[ach:1]].",
        created_at=now - timedelta(days=4), published_at=now - timedelta(days=2),
    )
    rich.cited_sources = [graded]
    rich.tags = [technique]
    session.add(rich)

    # A bare published report (no tradecraft, no reviewer, withheld by TLP ceiling).
    bare = Report(
        notebook_id=nb.id, title="Bare", author_id=author.id,
        status=ReportStatus.PUBLISHED, intel_level=IntelLevel.TACTICAL, tlp=TLP.RED,
        created_at=now - timedelta(days=10), published_at=now - timedelta(days=9),
    )
    session.add(bare)

    # In-flight reports (excluded from published/tradecraft denominators).
    session.add(Report(notebook_id=nb.id, title="Draft", author_id=author.id,
                       status=ReportStatus.DRAFT))
    session.add(Report(notebook_id=nb.id, title="Review", author_id=author.id,
                       status=ReportStatus.IN_REVIEW))
    session.commit()

    # Requirements: one linked PIR (covered), one unlinked GIR (gap), one satisfied RFI.
    pir = Requirement(stakeholder_id=stake.id, title="PIR", kind=RequirementKind.PIR,
                      status=RequirementStatus.IN_PROGRESS)
    pir.reports = [rich]
    gir = Requirement(stakeholder_id=stake.id, title="GIR", kind=RequirementKind.GIR,
                      status=RequirementStatus.OPEN)
    rfi = Requirement(stakeholder_id=stake.id, title="RFI", kind=RequirementKind.RFI,
                      status=RequirementStatus.SATISFIED)
    session.add_all([pir, gir, rfi])
    session.commit()

    # Dissemination: rich delivered twice (one read), bare once (unread).
    session.add_all([
        DisseminationEvent(report_id=rich.id, stakeholder_id=stake.id,
                           created_at=now - timedelta(days=2), read_at=now),
        DisseminationEvent(report_id=rich.id, stakeholder_id=reviewer.id,
                           created_at=now - timedelta(days=2)),
        DisseminationEvent(report_id=bare.id, stakeholder_id=stake.id,
                           created_at=now - timedelta(days=9)),
    ])
    session.commit()


def test_program_maturity_aggregation(engine):
    with Session(engine) as session:
        _seed(session)
        m = maturity_service.program_maturity(session)

    prod = m["production"]
    assert prod["total"] == 4
    by_status = {s["status"]: s["count"] for s in prod["by_status"]}
    assert by_status == {"DRAFT": 1, "IN_REVIEW": 1, "APPROVED": 0, "PUBLISHED": 2}
    assert prod["published_total"] == 2
    assert prod["in_flight"] == 2
    # Only the rich report carries a reviewer → 1 of 2 published.
    assert prod["reviewer_engagement"] == 0.5
    assert prod["median_days_to_publish"] is not None

    reqs = m["requirements"]
    assert {k["kind"]: k["count"] for k in reqs["by_kind"]} == {"PIR": 1, "GIR": 1, "RFI": 1}
    # Active = OPEN/IN_PROGRESS (PIR + GIR); only the PIR is linked.
    assert reqs["active_total"] == 2
    assert reqs["linked_active"] == 1
    assert reqs["coverage_rate"] == 0.5
    # Satisfaction over non-CLOSED (3 live: PIR, GIR, RFI; 1 satisfied).
    assert round(reqs["satisfaction_rate"], 3) == round(1 / 3, 3)
    assert reqs["pir_gaps"] == 0  # the only active PIR is linked

    diss = m["dissemination"]
    assert diss["events_total"] == 3
    assert diss["read_count"] == 1
    assert round(diss["read_rate"], 3) == round(1 / 3, 3)
    assert diss["stakeholders_reached"] == 2
    assert diss["withheld_count"] == 1  # the TLP:RED bare report
    assert diss["median_lag_days"] is not None

    trade = m["tradecraft"]
    assert trade["published_total"] == 2
    rates = {mt["label"]: mt["rate"] for mt in trade["metrics"]}
    # The rich report satisfies every practice; the bare one none → 1/2 each.
    assert rates["Key judgements"] == 0.5
    assert rates["Analytic confidence"] == 0.5
    assert rates["Graded sources"] == 0.5
    assert rates["Embedded analytic model"] == 0.5
    assert rates["ATT&CK techniques"] == 0.5
    assert 0.0 < trade["adoption_share"] <= 1.0


def test_maturity_level_thresholds(engine):
    """The CTI-CMM bucket helper maps a 0–1 metric to CTI0..CTI3 by band."""
    lvl = maturity_service._level
    assert lvl(0.0, 0.01, 0.4, 0.75) == 0
    assert lvl(0.2, 0.01, 0.4, 0.75) == 1
    assert lvl(0.4, 0.01, 0.4, 0.75) == 2
    assert lvl(0.9, 0.01, 0.4, 0.75) == 3


# --------------------------------------------------------------------------- #
# Route gating + render
# --------------------------------------------------------------------------- #
def test_maturity_page_renders_for_writers(client, login):
    for role in ("ANALYST", "REVIEWER", "ADMIN"):
        login(role, email=f"{role.lower()}@example.com")
        resp = client.get("/maturity")
        assert resp.status_code == 200, resp.text
        assert "CTI program maturity" in resp.text
        assert "Maturity rollup" in resp.text


def test_maturity_page_forbidden_for_stakeholder(client, login):
    login("STAKEHOLDER")
    resp = client.get("/maturity")
    assert resp.status_code == 403
