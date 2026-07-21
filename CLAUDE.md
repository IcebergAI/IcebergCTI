# Iceberg

> Detailed domain model, subsystem designs, and the test-coverage map live in [ARCHITECTURE.md](ARCHITECTURE.md).

## Overview
Iceberg is a cyber threat intelligence platform for collecting threat intelligence and, authoring and disseminating finished reports and artefacts to stakeholders. It follows the traditional Strategic, Tactical and Operational model for classifying intelligence levels. Iceberg also defines stakeholders and allows aligning intelligence goals with stakeholder requirements e.g. stakeholders are readonly users that can record intelligence goals which can be aggregated for analyst tasking. Stakeholders also define their preferred intelligence level and this drives dissemination. Classification of reports follows the TLP protocol. Reports are authored in markdown and published as finished reports in the portal. Iceberg is **not an IOC store** — the authoritative indicator store stays external (MISP in the first instance). It does carry **light-touch IOCs** as a staging layer: analysts capture indicators as notebook entities, cite a subset in a report (rendered as an Indicators appendix), and push them to MISP as one event (see *Light-touch IOCs (MISP push)*).

Collection is notebook-based: analysts open a topic **notebook**, gather **sources**, **notes** and uploaded **attachments** in it, then author one or more **intelligence products** (reports) from that material.

## Architecture
API-first design consumed by a server-rendered portal; all endpoints authenticated using JWT.

A **single FastAPI application** serves both the JSON API under `/api/*` and the server-rendered portal under `/*`. (The earlier "Quart" web framework was dropped in favour of this single-app design.) The interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled in prod.

Authentication is **OIDC against Microsoft Entra ID**; after login Iceberg mints its own short-lived **JWT** (per-user `token_version` for logout/revocation), sent as a Bearer header by API clients or stored in a signed session cookie by the portal. The JWT and the session cookie are signed with **purpose-separated keys** derived from `ICEBERG_SECRET_KEY` (`auth/signing.py`, `jwt` vs `session` contexts). A **dev-login bypass** (`ICEBERG_DEV_AUTH=true`, disabled in prod) issues a role JWT without an IdP for local dev/tests. Missing/unrecognised role claims default to read-only `STAKEHOLDER`; a missing email claim is rejected. A same-origin **CSRF** middleware (`auth/csrf.py`) rejects cookie-authenticated state-changing requests whose `Origin`/`Referer` mismatch the host (Bearer/anonymous exempt); logout is POST-only. Full detail — auth, CSRF, rate limiting, security response headers, health probes — in ARCHITECTURE.md.

Roles: `ADMIN`, `ANALYST`, `REVIEWER`, `STAKEHOLDER` (read-only).

### Key invariants

- **Secrets are env-only** — no secret DB columns; single-row settings (`Audit`/`Proxy`/`MISP`/`Webhook`Settings) seeded via `services/singleton.get_or_create` (atomic `INSERT … ON CONFLICT DO NOTHING`).
- **Published reports are immutable** — stakeholder/PDF/MISP output comes from the frozen `PublicationSnapshot`, not live notebook material; an ORM optimistic-lock `version` column maps a stale write to **409**.
- **Notebook access is role-wide** for any writer (ANALYST/REVIEWER/ADMIN), NOT owner-scoped (#65); `owner_id` is provenance only — the one owner/admin gate is deleting the whole notebook.
- **Inline-embed tokens** `[[diamond|figure|ach:ID]]` + bare `[[attack]]` (single source `embeds.py`); SVG / base64 `data:` URIs are injected **after** nh3 sanitisation; tokens are notebook-scoped, unknown/cross-notebook ids degrade to an "unavailable" notice.
- **TLP** is a display + dissemination-routing marking (never an in-portal read gate) that gates **AI egress** (`ICEBERG_AI_MAX_TLP`), **dissemination** (`ICEBERG_DISSEMINATION_MAX_TLP`), and the **MISP push** (`ICEBERG_MISP_MAX_TLP`).
- **Every outbound HTTP call honours the global proxy** (`proxy.resolve` — RSS/SIEM/MISP/AI/webhook); **external work rides the durable `OutboxJob` outbox**, enqueued in the same transaction as its cause and drained by `iceberg-worker` (lease/retry/backoff).
- **CSP-safe Alpine** — strict `script-src 'self'` (no `unsafe-inline`/`unsafe-eval`): the vendored CSP build, every component registered in `static/js/tags.js`, no inline JS / `on*=` handlers / `x-html`. **Middleware order** (outer→inner): `SecurityHeaders → Audit → RateLimit → Session → CSRF`.
- **Hardening** — the one server-side fetcher (RSS + writer TAXII/MISP pull) rides a full **SSRF guard** (http(s)-only, private/loopback rejected, byte/timeout-bounded, redirect hops re-validated); uploads are MIME-whitelisted + size-capped + **magic-byte validated** (`services/upload_validation.py`); the audit `detail` JSON never carries secrets/PII; prod guards (`config._guard_production`) refuse a SQLite URL / weak `ICEBERG_SECRET_KEY` / wildcard `FORWARDED_ALLOW_IPS`.

### Technologies
- Python >= 3.14
- FastAPI (API + portal), SQLModel — **SQLite** (zero-dependency **local dev/test default only**) or **PostgreSQL** (the **required datastore for every container/production deployment**, via the `psycopg` v3 driver and a `postgresql+psycopg://` `ICEBERG_DATABASE_URL`; `pip install` the `postgres` extra). The app **refuses to boot on a SQLite URL when `ICEBERG_ENVIRONMENT=prod`** (`config._guard_production`, alongside the secret-key guard and a prod-only rejection of a wildcard `FORWARDED_ALLOW_IPS` — client IPs key rate limits and audit data, so wildcard proxy trust is unsafe), and the Docker image ships **no SQLite fallback** (no `ICEBERG_DATABASE_URL` default) so a container can't silently run SQLite. PyTest
- Jinja2 + Tailwind CSS + Alpine JS (portal)
- Typst — report → PDF typesetting (rendering goes directly through the Typst binary; Quarto was considered as a publishing layer but direct Typst was chosen — equivalent output, fewer dependencies)

### Domain model (`src/iceberg/models.py`)
_Compact index — full per-model bullets in ARCHITECTURE.md._

- **User** — identity, role, intel-level preference, `token_version`, tag subs, audience-group memberships.
- **Notebook** — writer-only topic workspace; role-wide access (#65), owner/admin-only whole-notebook delete.
- **Source / Note** — collected notebook material; sources carry a TLP marking + Admiralty/NATO grading.
- **Attachment** — uploaded file on disk; writer-only; MIME + magic-byte validated; citable in reports.
- **DiamondModel** — Diamond Model assessment; embedded inline via `[[diamond:ID]]` (no link table).
- **Figure** — uploaded image (PNG/JPEG/GIF); embedded inline via `[[figure:ID]]` as a `data:` URI.
- **IOC** — light-touch indicator staging entity; MISP-typed; TLP inherited from provenance source.
- **ReportMispEvent** — records a report's idempotent MISP push (event uuid + last outcome).
- **MISPSettings** — single-row outbound MISP config (`/admin/misp`); API key env-only.
- **WebhookSettings** — single-row publication-webhook config (`/admin/webhook`); bearer token env-only.
- **ACHModel** — Analysis of Competing Hypotheses matrix; embedded inline via `[[ach:ID]]`.
- **Report** — the intelligence product — markdown body, `intel_level`, `tlp`, lifecycle, ICD 203 scaffolding, tags, optimistic-lock `version`, snapshot hash.
- **RenderedProduct** — on-demand PDF (FULL / EXEC_BRIEF / ONE_PAGER); retention-pruned, snapshot-bound.
- **PublicationSnapshot** — immutable publish-time frozen representation (HTML / Typst / MISP inputs).
- **Requirement** — stakeholder requirement (PIR/GIR/RFI) feeding the analyst tasking board.
- **Tag** — controlled CTI taxonomy term; admin-curated; named-threat kinds carry aliases + attribution.
- **DisseminationEvent** — a published report delivered to a stakeholder's feed, with read tracking.
- **OutboxJob** — durable lease-based unit of external work (dissemination email / webhook / RSS poll).
- **AudienceGroup** — admin-managed need-to-know group scoping stakeholders + reports.
- **ReportEmbedding** — rebuildable local vector row for related-products (no content egress).
- **ProductFeedback** — stakeholder feedback on a delivered product (usefulness + RFI-satisfaction verdict).
- **Feed / FeedItem** — admin-configured RSS/Atom source + fetched articles (inbound collection).
- **AuditEvent** — persisted security-relevant event (OWASP attributes); local forensic trail.
- **AuditSettings** — single-row SIEM-emit config (`/admin/audit`); HTTP/HEC token env-only.
- **ProxySettings** — single-row global outbound-proxy config (`/admin/proxy`); credentials env-only.

### TLP & intelligence level
TLP is a **display marking + a dissemination-routing input**; it does **not** gate in-portal read access (any authenticated user may browse published reports). `intel_level` is a classification tag used for dissemination matching. Product format (full/brief/one-pager) is chosen on demand and is independent of `intel_level`.

### Project structure
```
src/iceberg/
  main.py          # app factory: mounts API + portal, middleware stack (CSRF/session/rate-limit/audit/headers), auth redirect, 409 stale-write handler, RSS poll loop
  health.py        # unauthenticated /healthz (liveness) + /readyz (readiness) probes
  config.py        # pydantic-settings (ICEBERG_ env prefix) + prod guards (secret key, SQLite, wildcard proxy trust)
  db.py            # SQLite engine/session, FK pragma, Alembic upgrade on boot
  migrations/      # Alembic env + versioned migrations (baseline owns the FTS DDL)
  models.py        # SQLModel models + enums
  schemas.py       # API request bodies
  seed.py          # CLI: import the tag taxonomy (python -m iceberg.seed)
  attack_import.py # CLI: file-only MITRE Enterprise ATT&CK bundle import (iceberg-import-attack)
  help_content.py  # structured /help copy: per-role guides + concepts glossary
  maintenance.py   # console scripts: migrate (deploy entrypoint) + render retention prune + related-index rebuild + jobs worker (iceberg-worker) + restore file check (iceberg-verify-files)
  embeds.py        # single source of the inline-embed token grammar ([[diamond|figure|ach:ID]] / [[attack]])
  logging_config.py # app-log setup (level/format env, correlation-id contextvar)
  templating.py    # shared Jinja2Templates instance
  auth/            # OIDC (Entra) + dev login, JWT mint/decode (tokens), purpose-separated signing keys (signing), role dependencies, same-origin CSRF mw, rate-limit mw, audit capture mw, best-effort request-actor lookup
  api/             # JSON routers: notebooks (incl. imports/taxii + imports/misp), reports, requirements, feed, feedback, account, preview, tags, search, attack, audience, ai, taxii
                   #   (/api/preview/* are an authoring aid and are WRITER-ONLY: they resolve notebook-scoped
                   #    [[diamond]]/[[figure]]/[[ach]] tokens, so a read-only stakeholder must not reach them)
  web/             # portal routes (Jinja2): common (shared router + guards/helpers) + domain modules
                   #   (notebooks, analytics [diamond/ACH], reports, requirements, feed [stakeholder dissemination],
                   #    feeds [analyst RSS reader], discovery [search/taxonomy/audience], admin_audit, admin_feeds [RSS config],
                   #    admin_proxy [outbound proxy config], admin_misp [MISP push config], admin_webhook [publication webhook config])
  services/        # users, notebooks, lifecycle, citations/rendering (reports), publication (immutable publish snapshots + atomic publish), requirements, attachments, figures, upload_validation (magic-byte content sniffing for uploads), source_grading (offline Admiralty heuristic), diamond, ach, iocs (light-touch IOC staging CRUD + AI-candidate normalisation: refang + IOCType constraint), product_html (shared report-HTML assembler + inline-embed registry), dissemination (feed delivery + email/webhook job enqueue), jobs (durable OutboxJob outbox: enqueue/lease/retry + worker pass), singleton (conflict-safe settings-row seeding), webhook_settings (publication-webhook config row, env-only token), email, feed (stakeholder feed helpers), feedback (intel-cycle feedback loop / RFI-satisfaction), feeds (inbound RSS ingestion: admin CRUD + bounded SSRF-guarded fetch/parse/sanitise + send-to-notebook), inbound (writer-triggered TAXII/MISP pull → notebook sources/IOCs), misp + misp_settings (outbound MISP push: report indicators → one MISP event, env-only API key), proxy + proxy_settings (global outbound-proxy resolution for httpx), tags, search (FTS dispatch + paginated search_page), attack (ATT&CK Navigator export + coverage matrix), attack_import (Enterprise ATT&CK bundle → TECHNIQUE tags), stix, taxii (read-only TAXII 2.1-shaped serving), related, audience (fail-closed audience-group mutations), ai, tradecraft (advisory estimative-language lint), maturity (CTI program maturity & effectiveness dashboard), audit + audit_settings + siem (security audit logging → SIEM)
  rendering/       # markdown->HTML, report->PDF (typst), svg (shared diagram helpers: escape/fonts/wrap/placard)
  data/            # starter_tags.json (importable starter taxonomy)
  templates/       # Jinja2 + Alpine: base (command-center shell: rail+topbar+canvas+⌘K palette), _glyph, _macros,
                   #   one per screen (+ notebooks_list / entities_list index pages backing the rail)
  static/css/      # iceberg.css design system + vendor/{tailwind,fonts}.css (built, SRI)
  static/js/       # tags.js (Alpine factories) + vendor/alpine.min.js (pinned, SRI)
  static/fonts/    # self-hosted Archivo/JetBrains Mono/Spectral woff2
  static/assets.lock.json  # vendored-asset manifest (versions + SRI), read by templating.py
  typst/           # product.typ template
frontend/          # Tailwind v4 build entrypoint (input.css: @import/@source/@theme) for scripts/vendor_assets.py
scripts/           # vendor_assets.py — regenerate the self-hosted frontend assets
tests/             # pytest
```

## Running
```bash
uv sync --extra dev
cp .env.example .env            # adjust as needed
uv run uvicorn iceberg.main:app --reload
# open http://localhost:8000 — use the dev login (or configure Entra)
```
Optional for PDF rendering: install the [`typst`](https://github.com/typst/typst) binary on PATH.

The portal's frontend assets are **self-hosted** — `static/css/iceberg.css` (the hand-authored design system) plus the vendored, version-pinned, SRI-protected Tailwind build, Alpine and fonts under `static/{css,js,fonts}/vendor` (see *Self-hosted assets + SRI*). No CDN is contacted at runtime. To **regenerate or bump** them, edit the pins at the top of `scripts/vendor_assets.py` (and the font spec) and run `python scripts/vendor_assets.py`, then commit the regenerated `static/` assets + `assets.lock.json`. The `assets` CI job re-runs the script and fails on any drift; a `tests/test_frontend_assets.py` guard re-checks the SRI hashes and that no template references a CDN origin. (A standalone Tailwind binary is downloaded to `frontend/.bin/`, gitignored.)

## Testing
Test all crucial functionality with Pytest, create regression tests for identified bugs.
```bash
uv run pytest        # parallel by default (-n auto via pytest-xdist; pass -n0 to debug)
```
The suite is per-test setup-bound (each test rebuilds the app: migrations + taxonomy seed + FTS
rebuild), so it's run **in parallel** via `pytest-xdist` (`addopts = "-n auto"`) — ~107s → ~40s
on 8 cores. Tests are process-isolated and parallel-safe (in-memory DB per test, `tmp_path` for
files, ASGI transport so no port binding; the `siem`/`email` `OUTBOX` globals are cleared by
autouse fixtures per test). Use `pytest -n0` when you need `pdb` or live `-s` output.

**Static gates** (CI + pre-commit, mirrored): `ruff` (lint), `bandit` (security), `vulture`
(dead code), `mypy` (types), `pip-audit` (dependency CVEs), and **frontend lint** — `djlint
src/iceberg/templates --lint` (Jinja/HTML structure; lint-only, config + ignore rationale in
`[tool.djlint]`) and `biome lint src/iceberg/static` (hand-authored CSS + Alpine component JS; config
in `biome.jsonc`). Biome is the one tool not in the pip dev extra — it's a standalone binary (no
Node); CI installs it via `biomejs/setup-biome`, the pre-commit hook no-ops if it's absent.
(curlylint was evaluated and rejected: its parser can't handle Alpine's `@click`/`:class` attribute
syntax.) **mypy** type-checks the pure-logic / interface modules; ORM-query-heavy modules (SQLModel
column expressions defeat mypy) are staged out via an `ignore_errors` override list in `[tool.mypy]`
that is burned down over time — config in `pyproject.toml`, `migrations/` excluded like the other
gates. Two **separate CI-only workflows** run outside the bundled `test` job: **CodeQL** SAST
(`.github/workflows/codeql.yml`, Python + JavaScript/TypeScript, weekly cron, SARIF → code scanning,
vendored Alpine excluded) and a **`lint-workflows`** job (`zizmor` + `actionlint`) that audits the
workflow files themselves so the SHA-pinning / least-privilege posture can't silently regress. The
push-only `docker` job also runs a **Trivy** image vulnerability scan (fixable HIGH/CRITICAL,
`ignore-unfixed`) so base-layer CVEs beyond `pip-audit`'s reach surface.

**Releases (`.github/workflows/release.yml`, [`docs/RELEASING.md`](docs/RELEASING.md)).** A release
is a git tag: pushing a `v*` tag to `main` fires the workflow, which verifies the tag matches the
`pyproject.toml` version (PEP 440 → SemVer normalised) and that the commit is on `main`, then builds
and pushes `ghcr.io/icebergai/icebergcti` (SemVer tags; `:latest`/`major.minor` only for a stable
tag) **with an SBOM + SLSA `mode=max` provenance**, attests provenance to the registry,
**cosign-signs** the image (keyless/OIDC), and creates the GitHub Release. `workflow_dispatch` is a
build-only dry run. Version bumps are documented in `docs/RELEASING.md`; notable changes in
`CHANGELOG.md`. The `deploy/k8s/` manifests reference the published image (pin an immutable digest
in prod via `deploy/k8s/release.sh`).

## Maintenance
- Maintain an up to date CLAUDE.md
- Maintain an up to date README.md
- Maintain an up-to-date ARCHITECTURE.md alongside CLAUDE.md/README.md.
- **Frontend asset versions** — Tailwind/Alpine/fonts are pinned in `scripts/vendor_assets.py`. Periodically (e.g. quarterly) check upstream for newer releases, bump the pins, run `python scripts/vendor_assets.py`, and commit the regenerated `static/` assets + `assets.lock.json`. Dependabot can't track these CDN-script versions, so the bump is deliberate; the `assets` CI job enforces that the committed files match the pins.
