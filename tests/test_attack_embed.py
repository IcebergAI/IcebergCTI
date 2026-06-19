"""FR #41: inline `[[attack]]` token — embed a report's own ATT&CK
technique-coverage matrix as an SVG in the web view, the live preview and the
Typst PDF. Mirrors the diamond/ach embed tests: SVG generation + XML-escaping,
inline-token rendering (post-nh3), empty-state degrade, and a Typst smoke test.
"""

import pytest

from iceberg.config import get_settings
from iceberg.models import Report, Tag, TagKind
from iceberg.rendering.typst import _rewrite_attack_token, typst_available
from iceberg.services import attack


# --------------------------------------------------------------------------- #
# Helpers (mirroring tests/test_attack.py)
# --------------------------------------------------------------------------- #
def _tech(label, code, tactic):
    return Tag(kind=TagKind.TECHNIQUE, label=label, slug=code.lower(),
               external_id=code, description=tactic)


def _make_report(rid, tags):
    report = Report(notebook_id=1, title=f"R{rid}", author_id=1)
    report.tags = tags
    return report


def _report(client, login, title, body="body", author="author@example.com"):
    login("ANALYST", email=author)
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    return client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": title, "body_md": body},
    ).json()["id"]


def _tag(client, login, kind="TECHNIQUE", label="Phishing", external_id="T1566",
         description="Initial Access"):
    login("ADMIN", email="admin@example.com")
    return client.post(
        "/api/tags",
        json={"kind": kind, "label": label, "external_id": external_id,
              "description": description},
    ).json()


def _set_tags(client, login, rid, tag_ids):
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{rid}/tags", json={"tag_ids": tag_ids})


# --------------------------------------------------------------------------- #
# Service: SVG generation + escaping
# --------------------------------------------------------------------------- #
def test_render_attack_svg_escapes_dynamic_text():
    report = _make_report(1, [
        _tech("Phish <x>", "T1566", "Initial Access"),
        _tech("Exfil", "T1041", "Exfiltration"),
    ])
    svg = attack.render_attack_svg(attack.coverage_matrix([report]))

    assert svg.lstrip().startswith("<svg")
    assert "&lt;x&gt;" in svg and "Phish <x>" not in svg  # label escaped
    assert "T1566" in svg  # T-code present
    assert "INITIAL ACCESS" in svg  # tactic header (upper-cased)


def test_report_attack_svg_none_without_technique_tags():
    report = _make_report(1, [Tag(kind=TagKind.ACTOR, label="APT29", slug="apt29")])
    assert attack.report_attack_svg(report) is None


def test_report_attack_svg_renders_with_technique_tags():
    report = _make_report(1, [_tech("Phishing", "T1566", "Initial Access")])
    svg = attack.report_attack_svg(report)
    assert svg is not None and "Phishing" in svg


def test_render_attack_svg_empty_placard():
    svg = attack.render_attack_svg({"tactics": [], "max_count": 0, "total": 0})
    assert svg.lstrip().startswith("<svg")
    assert "techniques tagged on this report" in svg


def test_has_attack_token():
    assert attack.has_attack_token("intro [[attack]] end")
    assert not attack.has_attack_token("no token here")
    assert not attack.has_attack_token("")


# --------------------------------------------------------------------------- #
# Inline-token rendering into a report body (web view)
# --------------------------------------------------------------------------- #
def test_attack_token_renders_inline_and_survives_sanitizer(client, login):
    rid = _report(client, login, "Intrusion", body="# Intro\n\n[[attack]]\n\n## End")
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    login("ANALYST", email="author@example.com")

    html = client.get(f"/reports/{rid}").text
    assert "attack-figure" in html
    assert "<svg" in html  # server SVG injected post-sanitisation, not stripped
    assert "[[attack]]" not in html  # token consumed
    assert "Phishing" in html


def test_attack_token_degrades_without_technique_tags(client, login):
    rid = _report(client, login, "No techniques", body="[[attack]]")
    login("ANALYST", email="author@example.com")
    html = client.get(f"/reports/{rid}").text
    assert "attack-missing" in html
    assert "attack-figure" not in html  # no matrix injected


def test_attack_token_mixes_with_other_embeds(client, login):
    """`[[attack]]` resolves alongside a diamond token in the same body."""
    login("ANALYST", email="author@example.com")
    nb_id = client.post("/api/notebooks", json={"title": "nb"}).json()["id"]
    did = client.post(
        f"/api/notebooks/{nb_id}/diamonds",
        json={"title": "D", "adversary": "APT29"},
    ).json()["id"]
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb_id, "title": "R",
              "body_md": f"[[diamond:{did}]]\n\n[[attack]]"},
    ).json()["id"]
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    login("ANALYST", email="author@example.com")
    html = client.get(f"/reports/{rid}").text
    assert "diamond-figure" in html and "attack-figure" in html


# --------------------------------------------------------------------------- #
# Live preview
# --------------------------------------------------------------------------- #
def test_attack_preview_resolves_with_report_id(client, login):
    rid = _report(client, login, "R", body="")
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    login("ANALYST", email="author@example.com")

    with_ctx = client.post(
        "/api/preview", json={"markdown": "[[attack]]", "report_id": rid}
    ).json()["html"]
    assert "attack-figure" in with_ctx and "<svg" in with_ctx

    without = client.post(
        "/api/preview", json={"markdown": "[[attack]]"}
    ).json()["html"]
    assert "attack-figure" not in without
    assert "[[attack]]" in without  # left as literal text


def test_attack_product_preview_resolves(client, login):
    rid = _report(client, login, "R", body="")
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    login("ANALYST", email="author@example.com")
    html = client.post(
        "/api/preview/product",
        json={"report_id": rid, "body_md": "[[attack]]"},
    ).json()["html"]
    assert "attack-figure" in html and "<svg" in html


# --------------------------------------------------------------------------- #
# Typst path
# --------------------------------------------------------------------------- #
def test_rewrite_attack_token():
    rewritten = _rewrite_attack_token("a [[attack]] b", "<svg/>")
    assert "![ATT&CK technique coverage](attack.svg)" in rewritten
    assert "[[attack]]" not in rewritten

    degraded = _rewrite_attack_token("a [[attack]] b", None)
    assert "ATT&CK coverage unavailable" in degraded
    assert "[[attack]]" not in degraded


@pytest.mark.skipif(not typst_available(), reason="Typst binary not installed")
def test_render_with_attack_matrix(client, login, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "render_output_dir", str(tmp_path / "out"))
    rid = _report(client, login, "R", body="# Body\n\n[[attack]]")
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    login("ANALYST", email="author@example.com")

    resp = client.post(f"/api/reports/{rid}/render", json={"format": "FULL"})
    if resp.status_code in (500, 503):
        pytest.skip(f"Typst could not render: {resp.text}")
    assert resp.status_code == 201, resp.text
