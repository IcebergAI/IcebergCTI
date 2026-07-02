# Contributing to Iceberg

Thanks for your interest in improving Iceberg. This guide covers local setup, the
quality gates your change must pass, and what we look for in a pull request.

By contributing, you agree that your contributions are licensed under the
project's [Apache License 2.0](LICENSE) (inbound = outbound). You confirm you have
the right to submit the work under that license.

## Development setup

Iceberg uses [`uv`](https://docs.astral.sh/uv/) for dependency management. Python
**3.14+** is required.

```bash
uv sync --extra dev              # install runtime + dev dependencies
cp .env.example .env             # adjust as needed
uv run uvicorn iceberg.main:app --reload
# open http://localhost:8000 and use the dev login
```

The default datastore is **SQLite** (zero-dependency, dev/test only). Production
runs on **PostgreSQL** — see the README and `CLAUDE.md` for the full picture of
the architecture, domain model, and design decisions. `CLAUDE.md` is the
authoritative deep-dive; skim the section relevant to your change first.

Optional: install the [`typst`](https://github.com/typst/typst) binary on your
`PATH` to exercise the real PDF rendering path (tests skip it otherwise).

## Tests

```bash
uv run pytest                    # parallel by default (-n auto); pass -n0 to debug
```

All crucial functionality is covered by tests, and **bug fixes must come with a
regression test**. Coverage is enforced (`fail_under` in `pyproject.toml`).

## Quality gates

CI and the pre-commit hooks run the same static gates — please run them locally
before pushing:

```bash
uv run pre-commit install        # once, to enable the hooks
uv run pre-commit run --all-files
```

The gates are:

| Gate | Tool | Scope |
| --- | --- | --- |
| Lint | `ruff` | Python |
| Security | `bandit` | Python |
| Dead code | `vulture` | Python |
| Dependency CVEs | `pip-audit` | dependencies |
| Templates | `djlint --lint` | Jinja/HTML |
| CSS + Alpine JS | `biome lint` | `src/iceberg/static` |

`biome` is a standalone binary (no Node); the pre-commit hook no-ops if it is
absent, but CI enforces it.

### Frontend assets

Tailwind, Alpine and the fonts are **self-hosted** and version-pinned in
`scripts/vendor_assets.py` with Subresource Integrity. Do not hand-edit the files
under `static/*/vendor/`. To change a version, edit the pins and run
`python scripts/vendor_assets.py`, then commit the regenerated assets +
`static/assets.lock.json`. The `assets` CI job fails on any drift.

### Database migrations

Schema is owned by Alembic (SQLModel models are the source of truth). If you change
a model, generate a migration:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

`tests/test_migrations.py` asserts models and migrations don't drift.

## Pull requests

- **Branch** off `main`; keep PRs focused on one logical change.
- **Write a clear description**: what changed and why. Link the issue it closes
  (`Closes #123`).
- **Green CI**: tests, coverage, and all static gates must pass.
- **Update docs**: keep `README.md` and `CLAUDE.md` current when behaviour,
  configuration, or architecture changes — this is a project convention, not an
  afterthought.
- **Security-sensitive changes** (auth, egress, sanitisation, audit) get extra
  scrutiny — call out the security reasoning in the PR description.

## Reporting bugs and requesting features

Use the issue templates. For **security vulnerabilities**, do **not** open a public
issue — follow [`SECURITY.md`](SECURITY.md) instead.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it.
