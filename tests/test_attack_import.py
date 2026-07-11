"""ATT&CK structured-tactic and explicit bundle-import regressions (#176)."""

import hashlib
import json

from sqlmodel import Session, select

from iceberg import attack_import
from iceberg.models import Tag, TagKind
from iceberg.services import attack as attack_service
from iceberg.services.attack_import import import_enterprise_bundle
from iceberg.services.tags import seed_default_taxonomy


def _bundle(*objects: dict) -> dict:
    return {"type": "bundle", "objects": list(objects)}


def _technique(
    code: str,
    name: str,
    *phases: str,
    revoked: bool = False,
) -> dict:
    return {
        "type": "attack-pattern",
        "name": name,
        "description": f"{name} upstream description.",
        "x_mitre_domains": ["enterprise-attack"],
        "kill_chain_phases": [
            {"kill_chain_name": "mitre-attack", "phase_name": phase}
            for phase in phases
        ],
        "external_references": [
            {"source_name": "mitre-attack", "external_id": code}
        ],
        "revoked": revoked,
    }


def test_structured_tactics_support_multiple_columns_and_legacy_fallback():
    structured = Tag(
        kind=TagKind.TECHNIQUE,
        label="Shared technique",
        slug="shared-technique",
        external_id="T1000",
        description="Not a tactic",
        attack_tactics=["Initial Access", "Execution"],
    )
    legacy = Tag(
        kind=TagKind.TECHNIQUE,
        label="Legacy technique",
        slug="legacy-technique",
        external_id="T1001",
        description="Discovery",
    )
    from iceberg.models import Report

    report = Report(notebook_id=1, author_id=1, title="Coverage")
    report.tags = [structured, legacy]
    matrix = attack_service.coverage_matrix([report])

    assert [column["tactic"] for column in matrix["tactics"]] == [
        "Initial Access",
        "Execution",
        "Discovery",
    ]
    assert matrix["total"] == 2


def test_import_creates_updates_and_retires_techniques(engine):
    initial = _bundle(
        _technique("T1566", "Phishing", "initial-access", "execution"),
        _technique("T9999", "Retired technique", "impact", revoked=True),
    )
    with Session(engine) as session:
        first = import_enterprise_bundle(session, initial, update=True)
        assert (first.created, first.updated, first.retired) == (2, 0, 1)
        phish = session.exec(
            select(Tag).where(Tag.external_id == "T1566")
        ).one()
        retired = session.exec(
            select(Tag).where(Tag.external_id == "T9999")
        ).one()
        assert phish.attack_tactics == ["Initial Access", "Execution"]
        assert phish.active is True
        assert retired.active is False

        updated = _bundle(_technique("T1566", "Spearphishing", "credential-access"))
        result = import_enterprise_bundle(session, updated, update=True)
        assert (result.created, result.updated, result.retired) == (0, 1, 0)
        session.refresh(phish)
        assert phish.label == "Spearphishing"
        assert "Phishing" in phish.aliases
        assert phish.attack_tactics == ["Credential Access"]


def test_starter_seed_promotes_legacy_tactic_descriptions(engine):
    with Session(engine) as session:
        seed_default_taxonomy(
            session,
            [
                {
                    "kind": "TECHNIQUE",
                    "label": "Phishing",
                    "external_id": "T1566",
                    "description": "Initial Access",
                }
            ],
        )
        tag = session.exec(select(Tag).where(Tag.external_id == "T1566")).one()
        assert tag.attack_tactics == ["Initial Access"]


def test_attack_import_cli_checks_a_pinned_local_bundle(tmp_path, capsys):
    bundle = _bundle(_technique("T1566", "Phishing", "initial-access"))
    source = tmp_path / "enterprise-attack.json"
    raw = json.dumps(bundle).encode()
    source.write_bytes(raw)

    assert attack_import.main(
        [
            "--file",
            str(source),
            "--check",
            "--sha256",
            hashlib.sha256(raw).hexdigest(),
        ]
    ) == 0
    assert "Validated 1 Enterprise ATT&CK technique" in capsys.readouterr().out
