"""The `claude` (Anthropic first-party) and `bedrock` (Amazon Bedrock) AI-assist
backends (FR #123). The `anthropic` SDK is an optional extra not installed in the
test env, so these tests inject a fake `anthropic` module via ``sys.modules`` and
drive the public ``ai_service.assist`` — the same governance (TLP egress gate,
fail-soft, proxy-aware) the layer already enforces for `openai-compatible`."""

import json
import sys
import types

import pytest
from pydantic import ValidationError

from iceberg.config import Settings
from iceberg.models import ProxyMode, ProxySettings, Report, TLP, User
from iceberg.services import ai as ai_service

ACTOR = User(id=1, email="a@x.com", display_name="A")


# --------------------------------------------------------------------------- #
# Fakes — a stand-in `anthropic` module + a recording httpx.Client
# --------------------------------------------------------------------------- #
def _install_fake_anthropic(monkeypatch, *, payload=None, stop_reason="end_turn", raises=False):
    """Inject a fake `anthropic` module. Returns a state dict capturing the
    constructor kwargs (``ctor``) and messages.create kwargs (``calls``)."""
    state = {
        "ctor": [],
        "calls": [],
        "payload": {"key_judgements": "x"} if payload is None else payload,
        "stop_reason": stop_reason,
        "raises": raises,
    }

    class _Block:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    class _Resp:
        def __init__(self):
            self.content = [_Block(json.dumps(state["payload"]))]
            self.stop_reason = state["stop_reason"]

    class _Messages:
        def create(self, **kwargs):
            state["calls"].append(kwargs)
            if state["raises"]:
                raise RuntimeError("provider boom")
            return _Resp()

    class _Anthropic:
        def __init__(self, **kwargs):
            state["ctor"].append(("Anthropic", kwargs))
            self.messages = _Messages()

    class _Bedrock:
        def __init__(self, **kwargs):
            state["ctor"].append(("Bedrock", kwargs))
            self.messages = _Messages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Anthropic
    mod.AnthropicBedrockMantle = _Bedrock
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return state


def _capture_httpx_client(monkeypatch):
    """Replace ai_service.httpx.Client with a recorder so we can assert the proxy
    kwargs threaded into the SDK's custom http client."""
    captured: dict = {}

    class _C:
        def __init__(self, **kwargs):
            captured.clear()
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ai_service.httpx, "Client", _C)
    return captured


def _claude_settings(**over) -> Settings:
    base = dict(ai_backend="claude", ai_api_key="k")
    base.update(over)
    return Settings(**base)


def _bedrock_settings(**over) -> Settings:
    base = dict(ai_backend="bedrock", ai_aws_region="us-east-1")
    base.update(over)
    return Settings(**base)


def _proxy(**over) -> ProxySettings:
    base = dict(mode=ProxyMode.SYSTEM, proxy_url="", no_proxy="")
    base.update(over)
    return ProxySettings(**base)


def _assist(settings, **over):
    kw = dict(task="judgements", payload={"x": 1}, actor=ACTOR, settings=settings)
    kw.update(over)
    return ai_service.assist(kw.pop("task"), kw.pop("payload"), **kw)


# --------------------------------------------------------------------------- #
# claude — dispatch, parse, model defaulting
# --------------------------------------------------------------------------- #
def test_claude_dispatch_parses_json_and_stamps_provenance(monkeypatch):
    state = _install_fake_anthropic(monkeypatch, payload={"key_judgements": "BLUF"})
    _capture_httpx_client(monkeypatch)

    out = _assist(_claude_settings())

    assert out.available is True
    assert out.suggestion == {"key_judgements": "BLUF"}
    assert out.provenance["backend"] == "claude"
    # Default model, modest cap, and crucially NO temperature/thinking (Opus 4.x 400s).
    create_kwargs = state["calls"][0]
    assert create_kwargs["model"] == "claude-opus-4-8"
    assert create_kwargs["max_tokens"] == ai_service._CLAUDE_MAX_TOKENS
    assert "temperature" not in create_kwargs
    assert "thinking" not in create_kwargs
    # The env-only API key reaches the client constructor.
    assert state["ctor"][0] == ("Anthropic", state["ctor"][0][1])
    assert state["ctor"][0][1]["api_key"] == "k"


def test_claude_honours_explicit_model(monkeypatch):
    state = _install_fake_anthropic(monkeypatch)
    _capture_httpx_client(monkeypatch)

    _assist(_claude_settings(ai_model="claude-sonnet-4-6"))

    assert state["calls"][0]["model"] == "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# bedrock — region, prefixed default model
# --------------------------------------------------------------------------- #
def test_bedrock_dispatch_uses_region_and_prefixed_default_model(monkeypatch):
    state = _install_fake_anthropic(monkeypatch, payload={"adversary": "?"})
    _capture_httpx_client(monkeypatch)

    out = _assist(_bedrock_settings())

    assert out.available is True
    assert out.suggestion == {"adversary": "?"}
    assert out.provenance["backend"] == "bedrock"
    assert state["ctor"][0][0] == "Bedrock"
    assert state["ctor"][0][1]["aws_region"] == "us-east-1"
    assert state["calls"][0]["model"] == "anthropic.claude-opus-4-8"  # provider prefix


# --------------------------------------------------------------------------- #
# Governance — TLP gate, fail-soft, refusal
# --------------------------------------------------------------------------- #
def test_over_ceiling_report_blocks_before_any_egress(monkeypatch):
    state = _install_fake_anthropic(monkeypatch)
    _capture_httpx_client(monkeypatch)

    out = _assist(
        _claude_settings(),  # default ai_max_tlp=AMBER
        report=Report(notebook_id=1, title="secret", tlp=TLP.RED),
    )

    assert out.available is False
    assert "ceiling" in out.message
    assert state["ctor"] == []  # client never constructed
    assert state["calls"] == []  # nothing egressed


@pytest.mark.parametrize("backend", ["claude", "bedrock"])
def test_provider_error_is_fail_soft(monkeypatch, backend):
    _install_fake_anthropic(monkeypatch, raises=True)
    _capture_httpx_client(monkeypatch)
    settings = _claude_settings() if backend == "claude" else _bedrock_settings()

    out = _assist(settings)

    assert out.available is False
    assert out.message == "AI provider failed"


def test_refusal_stop_reason_is_fail_soft(monkeypatch):
    _install_fake_anthropic(monkeypatch, stop_reason="refusal")
    _capture_httpx_client(monkeypatch)

    out = _assist(_claude_settings())

    assert out.available is False
    assert out.message == "AI provider declined the request"


# --------------------------------------------------------------------------- #
# Proxy wiring — the SDK's custom http client carries the resolved proxy kwargs
# --------------------------------------------------------------------------- #
def test_claude_threads_proxy_into_http_client(monkeypatch):
    state = _install_fake_anthropic(monkeypatch)
    captured = _capture_httpx_client(monkeypatch)

    _assist(
        _claude_settings(),
        proxy_settings=_proxy(mode=ProxyMode.EXPLICIT, proxy_url="http://p:3128"),
    )

    assert captured == {"trust_env": False, "proxy": "http://p:3128"}
    # The recorded http client is handed to the SDK constructor.
    assert "http_client" in state["ctor"][0][1]


# --------------------------------------------------------------------------- #
# Lazy import — a deployment that enabled the backend without installing the SDK
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "backend,settings_factory",
    [("claude", _claude_settings), ("bedrock", _bedrock_settings)],
)
def test_missing_sdk_is_fail_soft(monkeypatch, backend, settings_factory):
    # sys.modules["anthropic"] = None makes `import anthropic` raise ImportError.
    monkeypatch.setitem(sys.modules, "anthropic", None)

    out = _assist(settings_factory())

    assert out.available is False
    assert "not installed" in out.message


# --------------------------------------------------------------------------- #
# Config — backend name is validated
# --------------------------------------------------------------------------- #
def test_unknown_backend_rejected_at_config():
    with pytest.raises(ValidationError):
        Settings(ai_backend="bogus")


def test_known_backends_accepted_at_config():
    for backend in ("none", "openai-compatible", "claude", "bedrock"):
        assert Settings(ai_backend=backend).ai_backend == backend
