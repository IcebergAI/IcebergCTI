"""DB-backed AI provider configuration (issue #246).

Covers env→DB seeding, the effective-config resolver, provider validation +
base-URL pinning, secret redaction, the retained TLP egress gate on the resolved
path, and admin-only gating of the /admin/ai console.
"""

import pytest
from sqlmodel import Session

from iceberg.config import Settings, get_settings
from iceberg.models import AISettings, Report, TLP, User
from iceberg.services import ai as ai_service
from iceberg.services import ai_settings


# --------------------------------------------------------------------------- #
# Seeding + resolution
# --------------------------------------------------------------------------- #
def test_get_seeds_from_env_on_first_read(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_backend", "claude")
    monkeypatch.setattr(get_settings(), "ai_model", "claude-opus-4-8")
    monkeypatch.setattr(get_settings(), "ai_max_tlp", "GREEN")
    with Session(engine) as session:
        row = ai_settings.get(session)
        assert row.id == 1
        assert row.backend == "claude"
        assert row.model == "claude-opus-4-8"
        assert row.max_tlp == "GREEN"


def test_resolve_overlays_db_row_but_keeps_env_secret(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_api_key", "env-secret")
    with Session(engine) as session:
        ai_settings.update(session, backend="openai", model="gpt-4o-mini")
        resolved = ai_settings.resolve(session)
    assert resolved.ai_backend == "openai"
    assert resolved.ai_model == "gpt-4o-mini"
    # The secret is never on the row; it stays sourced from the environment.
    assert resolved.ai_api_key == "env-secret"


# --------------------------------------------------------------------------- #
# Validation + base-URL pinning
# --------------------------------------------------------------------------- #
def test_validate_requires_model_and_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_api_key", "")
    row = AISettings(backend="openai", model="")
    errors = ai_settings.validate_selection(row)
    assert any("model" in e.lower() for e in errors)
    assert any("ICEBERG_AI_API_KEY" in e for e in errors)


def test_validate_none_is_valid(monkeypatch):
    assert ai_settings.validate_selection(AISettings(backend="none")) == []


def test_validate_ollama_pins_base_url(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_ollama_base_url", "http://ollama.internal/v1")
    bad = AISettings(backend="ollama", model="llama3.1", base_url="http://evil.example/v1")
    assert any("Ollama base URL" in e for e in ai_settings.validate_selection(bad))
    good = AISettings(
        backend="ollama", model="llama3.1", base_url="http://ollama.internal/v1"
    )
    assert ai_settings.validate_selection(good) == []


def test_validate_bedrock_requires_region(monkeypatch):
    row = AISettings(backend="bedrock", model="anthropic.claude-opus-4-8", aws_region="")
    assert any("region" in e.lower() for e in ai_settings.validate_selection(row))


def test_openai_backend_base_url_is_pinned():
    # The DB base_url is ignored for the pinned providers — the target host can't
    # be redirected by a config edit.
    backend = ai_service._BACKENDS["openai"]
    settings = Settings(ai_backend="openai", ai_base_url="http://evil.example", ai_model="m")
    assert backend._resolved_base_url(settings) == ai_service._OPENAI_BASE_URL


# --------------------------------------------------------------------------- #
# TLP egress gate survives on the resolved path
# --------------------------------------------------------------------------- #
def test_tlp_gate_blocks_over_ceiling_report_on_resolved_settings(monkeypatch):
    monkeypatch.setattr(ai_service.httpx, "post", lambda *a, **k: pytest.fail("egressed"))
    resolved = Settings(
        ai_backend="openai-compatible",
        ai_base_url="https://ai.example/v1",
        ai_model="m",
        ai_max_tlp="AMBER",
    )
    report = Report(title="t", body_md="b", tlp=TLP.RED)
    result = ai_service.assist(
        "judgements",
        {"x": 1},
        actor=User(id=1, email="a@x.com", display_name="A"),
        settings=resolved,
        report=report,
    )
    assert result.available is False
    assert "ceiling" in result.message.lower()


# --------------------------------------------------------------------------- #
# Admin console gating + secret redaction
# --------------------------------------------------------------------------- #
def test_admin_ai_page_admin_only(client, login):
    login("ANALYST")
    assert client.get("/admin/ai").status_code == 403
    login("ADMIN")
    assert client.get("/admin/ai").status_code == 200


def test_admin_ai_save_persists_and_never_exposes_key(client, login, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_api_key", "super-secret-value")
    login("ADMIN")
    resp = client.post(
        "/admin/ai",
        data={
            "backend": "openai",
            "model": "gpt-4o-mini",
            "max_tlp": "AMBER",
            "timeout": "20",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    page = client.get("/admin/ai").text
    # The secret value / prefix must never render — only a "configured" status.
    assert "super-secret-value" not in page
    assert "configured" in page
