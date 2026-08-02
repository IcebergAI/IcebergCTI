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


def test_validate_openai_compatible_pins_base_url(monkeypatch):
    """The generic escape hatch used to accept ANY non-empty URL, which handed a
    DB writer the operator's API key plus TLP-gated content (#270). It is now
    pinned to an operator env value exactly like ollama."""
    monkeypatch.setattr(get_settings(), "ai_api_key", "env-key")
    monkeypatch.setattr(
        get_settings(), "ai_openai_compatible_base_url", "https://gateway.internal/v1"
    )
    bad = AISettings(
        backend="openai-compatible", model="m", base_url="https://attacker.example/v1"
    )
    assert any("must match" in e for e in ai_settings.validate_selection(bad))
    good = AISettings(
        backend="openai-compatible", model="m", base_url="https://gateway.internal/v1"
    )
    assert ai_settings.validate_selection(good) == []


def test_validate_openai_compatible_refused_without_the_env_pin(monkeypatch):
    """With no operator-approved URL there is no trust anchor, so the backend is
    refused outright rather than trusting whatever the row says."""
    monkeypatch.setattr(get_settings(), "ai_api_key", "env-key")
    monkeypatch.setattr(get_settings(), "ai_openai_compatible_base_url", "")
    row = AISettings(
        backend="openai-compatible", model="m", base_url="https://anything.example/v1"
    )
    errors = ai_settings.validate_selection(row)
    assert any("ICEBERG_AI_OPENAI_COMPATIBLE_BASE_URL" in e for e in errors)


def test_env_pinned_backends_match_the_runtime_backend_fields():
    """``ai_settings`` and ``services/ai.py`` each name the approving env field;
    a drift between them would silently disable one half of the pin."""
    for backend, (_, field_name, _env) in ai_settings._ENV_PINNED_BACKENDS.items():
        assert ai_service._BACKENDS[backend].approved_base_url_field == field_name
        assert ai_service._BACKENDS[backend].pinned_base_url is None
        assert hasattr(Settings(), field_name)


@pytest.mark.parametrize(
    "base_url", ["http://169.254.169.254/latest/meta-data", "https://attacker.example/v1"]
)
def test_openai_compatible_refuses_an_unapproved_host_at_runtime(base_url, monkeypatch):
    """Runtime enforcement, not just admin-form validation: nothing may leave the
    process for a host the operator never approved — including a link-local
    address that would turn every assist into an authenticated SSRF (#270)."""
    monkeypatch.setattr(
        ai_service.httpx, "post", lambda *a, **k: pytest.fail("AI content egressed")
    )
    backend = ai_service._BACKENDS["openai-compatible"]
    settings = Settings(
        ai_backend="openai-compatible",
        ai_base_url=base_url,
        ai_openai_compatible_base_url="https://gateway.internal/v1",
        ai_model="m",
        ai_api_key="env-key",
    )
    with pytest.raises(ai_service.BackendUnavailable):
        backend._complete({"task": "t"}, settings=settings, proxy_settings=None)


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


def test_invalid_ollama_base_url_cannot_egress_at_runtime(engine, monkeypatch):
    """A saved Ollama base URL that doesn't match the operator-approved value
    must fail closed at resolution — no writer AI call can reach it (review of
    #246: base-URL pinning has to hold at runtime, not just on the admin page)."""
    monkeypatch.setattr(get_settings(), "ai_ollama_base_url", "http://approved.internal/v1")
    monkeypatch.setattr(get_settings(), "ai_api_key", "env-key")
    monkeypatch.setattr(
        ai_service.httpx, "post", lambda *a, **k: pytest.fail("AI content egressed")
    )
    with Session(engine) as session:
        ai_settings.update(
            session,
            backend="ollama",
            model="llama3.1",
            base_url="http://attacker.example/v1",  # not the approved URL
        )
        resolved = ai_settings.resolve(session)
        # Resolution neutralises the invalid selection.
        assert resolved.ai_backend == "none"
        result = ai_service.assist(
            "judgements",
            {"x": 1},
            actor=User(id=1, email="a@x.com", display_name="A"),
            settings=resolved,
        )
    assert result.available is False
