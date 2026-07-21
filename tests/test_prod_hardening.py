"""Production hardening for API docs and signing-key separation."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from iceberg import main as main_module
from iceberg.auth.signing import jwt_signing_key, session_signing_key
from iceberg.config import Settings

_SECRET = "x" * 40
_PG_URL = "postgresql+psycopg://iceberg:iceberg@postgres:5432/iceberg"
_REPO_ROOT = Path(__file__).resolve().parents[1]


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


def test_compose_prod_overrides_and_loopback_port(tmp_path):
    """Render Compose with an isolated env file so local .env cannot mask #163."""
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")

    env_file = tmp_path / "compose.env"
    env_file.write_text(
        "\n".join(
            [
                "ICEBERG_ENVIRONMENT=prod",
                "ICEBERG_AUTO_MIGRATE=false",
                "ICEBERG_DATABASE_URL=",
                "POSTGRES_USER=iceberg_app",
                "POSTGRES_PASSWORD=testpass",
                "POSTGRES_DB=iceberg_prod",
                "",
            ]
        )
    )
    env = os.environ.copy()
    env.update(
        {
            "ICEBERG_ENVIRONMENT": "prod",
            "ICEBERG_AUTO_MIGRATE": "false",
            "ICEBERG_DATABASE_URL": "",
            "POSTGRES_USER": "iceberg_app",
            "POSTGRES_PASSWORD": "testpass",
            "POSTGRES_DB": "iceberg_prod",
        }
    )

    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(tmp_path),
            "--env-file",
            str(env_file),
            "-f",
            str(_REPO_ROOT / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    config = json.loads(result.stdout)

    iceberg = config["services"]["iceberg"]
    postgres = config["services"]["postgres"]
    redis = config["services"]["redis"]

    assert iceberg["environment"]["ICEBERG_ENVIRONMENT"] == "prod"
    assert iceberg["environment"]["ICEBERG_AUTO_MIGRATE"] == "false"
    assert (
        iceberg["environment"]["ICEBERG_DATABASE_URL"]
        == "postgresql+psycopg://iceberg_app:testpass@postgres:5432/iceberg_prod"
    )
    assert iceberg["environment"]["ICEBERG_RATE_LIMIT_REDIS_URL"] == "redis://redis:6379/0"
    assert "redis" in iceberg["depends_on"]
    assert postgres["environment"]["POSTGRES_PASSWORD"] == "testpass"
    assert postgres["environment"]["POSTGRES_USER"] == "iceberg_app"
    assert postgres["environment"]["POSTGRES_DB"] == "iceberg_prod"
    assert redis["image"] == "redis:8-alpine"
    assert iceberg["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8000,
            "published": "8000",
            "protocol": "tcp",
        }
    ]

    # Caddy is the default front end (no profile flag needed): sole public
    # ports, non-root, minimal capabilities, gated on a healthy app.
    caddy = config["services"]["caddy"]
    assert {(p["target"], p["protocol"]) for p in caddy["ports"]} == {
        (80, "tcp"),
        (443, "tcp"),
        (443, "udp"),
    }
    assert all("host_ip" not in p for p in caddy["ports"])  # public, not loopback
    assert caddy["user"] == "1000:1000"
    assert caddy["cap_drop"] == ["ALL"]
    assert caddy["cap_add"] == ["NET_BIND_SERVICE"]
    assert caddy["read_only"] is True
    assert caddy["depends_on"]["iceberg"]["condition"] == "service_healthy"
    assert (
        caddy["depends_on"]["caddy-init"]["condition"] == "service_completed_successfully"
    )
