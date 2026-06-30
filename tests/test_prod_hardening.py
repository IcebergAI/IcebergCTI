"""Production hardening for API docs and signing-key separation."""

from iceberg import main as main_module
from iceberg.auth.signing import jwt_signing_key, session_signing_key
from iceberg.config import Settings

_SECRET = "x" * 40
_PG_URL = "postgresql+psycopg://iceberg:iceberg@postgres:5432/iceberg"


def _route_paths(app) -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


def test_api_docs_enabled_in_dev(monkeypatch):
    settings = Settings(environment="dev", secret_key=_SECRET)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    app = main_module.create_app()

    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"
    assert {"/docs", "/redoc", "/openapi.json"} <= _route_paths(app)


def test_api_docs_disabled_in_prod(monkeypatch):
    settings = Settings(
        environment="prod",
        secret_key=_SECRET,
        database_url=_PG_URL,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    app = main_module.create_app()

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    assert not ({"/docs", "/redoc", "/openapi.json"} & _route_paths(app))


def test_signing_keys_are_purpose_separated_and_deterministic():
    settings = Settings(environment="dev", secret_key=_SECRET)

    assert jwt_signing_key(settings) == jwt_signing_key(settings)
    assert session_signing_key(settings) == session_signing_key(settings)
    assert jwt_signing_key(settings) != session_signing_key(settings)
    assert jwt_signing_key(settings) != settings.secret_key
    assert session_signing_key(settings) != settings.secret_key
