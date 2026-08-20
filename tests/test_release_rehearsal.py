"""The beta release's rehearsal and verification tooling (#314).

The rehearsal exists to catch an unshippable release *before* a tag is pushed, so
the tooling itself is covered here rather than trusted to work on the day.
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from iceberg import db
from iceberg.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "scripts" / "rehearsal_seed.py"
SCRIPTS = (ROOT / "scripts" / "release_rehearsal.sh", ROOT / "scripts" / "verify_release.sh")


def _run(*args, env=None, cwd=ROOT):
    return subprocess.run(
        [sys.executable, str(SEED), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


# --------------------------------------------------------------------------- #
# The migration history the rehearsal walks
# --------------------------------------------------------------------------- #
def test_the_migration_history_is_a_single_ordered_chain():
    """A second head would break both the rehearsal and a real upgrade."""

    sys.path.insert(0, str(ROOT / "scripts"))
    import rehearsal_seed

    chain = rehearsal_seed._revision_chain()
    versions = list((ROOT / "src" / "iceberg" / "migrations" / "versions").glob("*.py"))

    assert len(chain) == len(versions), "every migration must be reachable from the root"
    assert len(set(chain)) == len(chain), "revision ids must be unique"


def test_the_rollback_report_classifies_every_migration():
    result = _run("--stage", "rollback-report")

    assert result.returncode == 0, result.stderr
    body = result.stdout
    # The data-only backfill restores nothing; a table-creating migration is lossy.
    assert re.search(r"b8c9d0e1f2a3\s+no-op", body)
    assert re.search(r"e1f2a3b4c5d6\s+lossy", body)
    assert "Roll back by restoring the pre-upgrade backup" in body
    assert "missing" not in body, "every migration should define a downgrade()"


def test_the_rollback_report_needs_no_database(monkeypatch):
    """It is a static reading of the migration files, so it always works."""

    env = {"PATH": "/usr/bin:/bin"}
    result = _run("--stage", "rollback-report", env=env)

    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# The upgrade the rehearsal performs
# --------------------------------------------------------------------------- #
def test_seeded_data_survives_the_upgrade_to_head(tmp_path, monkeypatch):
    """Stage at the previous revision, seed, migrate forward, and read it back.

    This is the rehearsal's second stage, run here against SQLite so it is part
    of the ordinary suite; CI runs the same stages against PostgreSQL.
    """

    url = f"sqlite:///{tmp_path / 'rehearsal.db'}"
    env = {
        "PATH": "/usr/bin:/bin",
        "ICEBERG_DATABASE_URL": url,
        "ICEBERG_SECRET_KEY": "rehearsal-secret-0123456789abcdef0123456789",
        "ICEBERG_ENVIRONMENT": "dev",
    }

    staged = _run("--stage", "previous", env=env)
    assert staged.returncode == 0, staged.stderr
    assert "staged at previous revision" in staged.stdout

    monkeypatch.setattr(get_settings(), "database_url", url)
    command.upgrade(db.alembic_config(), "head")

    verified = _run("--stage", "verify", env=env)
    assert verified.returncode == 0, verified.stderr
    assert "readable at head" in verified.stdout

    engine = sa.create_engine(url)
    with engine.connect() as conn:
        revision = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    engine.dispose()
    sys.path.insert(0, str(ROOT / "scripts"))
    import rehearsal_seed

    assert revision == rehearsal_seed._revision_chain()[-1]


def test_verify_fails_loudly_when_the_data_is_not_there(tmp_path):
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    env = {
        "PATH": "/usr/bin:/bin",
        "ICEBERG_DATABASE_URL": url,
        "ICEBERG_SECRET_KEY": "rehearsal-secret-0123456789abcdef0123456789",
        "ICEBERG_ENVIRONMENT": "dev",
    }
    command.upgrade(db.alembic_config(), "head")  # schema only, no seed

    result = _run("--stage", "verify", env=env)

    assert result.returncode != 0


def test_a_stage_needing_a_database_says_so(tmp_path):
    result = _run("--stage", "verify", env={"PATH": "/usr/bin:/bin"})

    assert result.returncode == 2
    assert "ICEBERG_DATABASE_URL is required" in result.stderr


# --------------------------------------------------------------------------- #
# The scripts themselves
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_the_release_scripts_are_executable_and_valid_shell(script):
    assert script.exists(), script
    assert script.stat().st_mode & 0o111, f"{script.name} must be executable"
    checked = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    body = script.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    # Fail fast: a rehearsal that continues past a failed step proves nothing.
    assert "set -euo pipefail" in body


# --------------------------------------------------------------------------- #
# The version the release will carry
# --------------------------------------------------------------------------- #
_PRERELEASE = {"a": "alpha", "b": "beta", "rc": "rc"}


def _pep440_to_semver(version: str) -> str:
    """The same normalisation `release.yml` applies before comparing the tag."""

    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    assert match, f"{version} is not a release-workflow-acceptable version"
    base, kind, number = match.groups()
    if not kind:
        return f"v{base}"
    return f"v{base}-{_PRERELEASE[kind]}.{number}"


def test_the_project_version_and_changelog_agree():
    """`release.yml` refuses a tag that disagrees with pyproject; catch it here."""

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    semver = _pep440_to_semver(version)
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert f"## [{semver.lstrip('v')}]" in changelog, (
        f"CHANGELOG has no heading for {semver}"
    )
    assert f"[{semver.lstrip('v')}]: https://github.com/IcebergAI/IcebergCTI" in changelog


def test_the_lockfile_records_the_project_version():
    """`uv sync --locked` fails in CI when the bump skips `uv lock`."""

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    lock = (ROOT / "uv.lock").read_text()

    assert re.search(rf'name = "iceberg"\nversion = "{re.escape(version)}"', lock)
