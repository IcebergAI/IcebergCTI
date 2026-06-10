# Iceberg

## Overview
Iceberg is a cyber threat intelligence platform for collecting threat intelligence and, authoring and disseminating finished reports and artefacts to stakeholders. It follows the traditional Strategic, Tactical and Operational model for classifying intelligence levels. Iceberg also defines stakeholders and allows aligning intelligence goals with stakeholder requirements e.g. stakeholders are readonly users that can record intelligence goals which can be aggregated for analyst tasking. Stakeholders also define their preferred intelligence level and this drives dissemination. Classification of reports follows the TLP protocol. Reports are authored in markdown and published as finished reports in the portal. Iceberg doesn't deal directly in IOCs (though these may be surfaced in reports)

Collection is notebook-based: analysts open a topic **notebook**, gather **sources**, **notes** and uploaded **attachments** in it, then author one or more **intelligence products** (reports) from that material.

## Architecture
API-first design consumed by a server-rendered portal; all endpoints authenticated using JWT.

A **single FastAPI application** serves both the JSON API under `/api/*` and the server-rendered portal under `/*`. (The earlier "Quart" web framework was dropped in favour of this single-app design.)

Authentication is **OIDC against Microsoft Entra ID** (Authorization Code flow); the IdP's app-role/group claim maps to an Iceberg role. After login Iceberg mints its own short-lived JWT — sent as a Bearer header by API clients, or stored in a signed session cookie by the portal — so the "all endpoints JWT-authenticated" rule holds uniformly. A **dev-login bypass** (`ICEBERG_DEV_AUTH=true`, disabled when `ICEBERG_ENVIRONMENT=prod`) issues a JWT for a chosen role without an IdP, for local development and tests.

Roles: `ADMIN`, `ANALYST`, `REVIEWER`, `STAKEHOLDER` (read-only).

### Technologies
- Python >= 3.14
- FastAPI (API + portal), SQLModel (SQLite), PyTest
- Jinja2 + Tailwind CSS + Alpine JS (portal)
- Typst — report → PDF typesetting (rendering goes directly through the Typst binary; Quarto was considered as a publishing layer but direct Typst was chosen — equivalent output, fewer dependencies)

### Domain model (`src/iceberg/models.py`)
- **User** — identity, role, optional `preferred_intel_level`.
- **Notebook** — topic workspace owned by an analyst; has many sources, notes and reports.
- **Source** / **Note** — collected material inside a notebook.
- **Attachment** — an uploaded reference file held against a notebook. Stored on disk under `ICEBERG_ATTACHMENTS_DIR` with a server-generated UUID name; the DB row keeps metadata + the original filename. Upload/download are **writer-only** (read-only stakeholders have no access); uploads are MIME-whitelisted and size-capped (`ICEBERG_ATTACHMENT_MAX_MB`, default 25). Citable in reports via `ReportAttachment` and listed in the rendered PDF's appendix. See `services/attachments.py`.
- **Report** (intelligence product) — markdown body, `intel_level` (STRATEGIC/TACTICAL/OPERATIONAL), `tlp`, lifecycle `status`, author/reviewer. Cites a subset of its notebook's sources (`ReportSource`) and attachments (`ReportAttachment`).
- **RenderedProduct** — an on-demand PDF for a report (FULL / EXEC_BRIEF / ONE_PAGER).
- **Requirement** — stakeholder PIR/RFI with `priority` + `status`, feeding the analyst tasking board. Traced to the reports/notebooks that satisfy/address it via `ReportRequirement` / `NotebookRequirement`.
- **DisseminationEvent** — a published report delivered to a stakeholder's feed, with read tracking.

### Report lifecycle (`src/iceberg/services/lifecycle.py`)
`DRAFT → IN_REVIEW → APPROVED → PUBLISHED`, with send-back paths for rework. The author submits their own draft; a `REVIEWER`/`ADMIN` approves, sends back, or publishes. Publishing stamps `published_at`. Published reports are immutable.

### Requirements & analyst tasking (`src/iceberg/api/requirements.py`, `services/requirements.py`)
Stakeholders (read-only for intel) **submit requirements**; they see only their own. Analysts/reviewers/admins see the **aggregated tasking board** (`/requirements`), grouped by status and ordered by priority, and drive each requirement's `status` (OPEN/IN_PROGRESS/SATISFIED/CLOSED). Analysts establish **traceability** by ticking the requirements a report satisfies (in the report editor) or a notebook addresses (on the notebook page); the requirement detail page shows the linked reports/notebooks. Role split: only stakeholders/admins create requirements; only analysts/reviewers/admins change status and create links; only the owning stakeholder (or admin) edits/deletes a requirement's fields.

### TLP & intelligence level
TLP is a **display marking + a dissemination-routing input**; it does **not** gate in-portal read access (any authenticated user may browse published reports). `intel_level` is a classification tag used for dissemination matching. Product format (full/brief/one-pager) is chosen on demand and is independent of `intel_level`.

### Dissemination (`src/iceberg/services/dissemination.py`, `services/email.py`)
On publish, Iceberg matches stakeholders and delivers the report to their **feed**: a stakeholder matches when (a) the report is broadcast-eligible under the TLP ceiling — reports at or below `ICEBERG_DISSEMINATION_MAX_TLP` (default AMBER) are disseminated, while RED / AMBER+STRICT are withheld — **and** (b) the report's `intel_level` equals the stakeholder's `preferred_intel_level` (or the stakeholder has set no preference = all levels). Feed delivery is recorded synchronously as `DisseminationEvent`s; an **email notification** is sent as a FastAPI background task. Email uses a pluggable backend (`ICEBERG_EMAIL_BACKEND`): `console` (default — records to an in-memory outbox + logs, for dev/tests) or `smtp`. Stakeholders set their preference at `/preferences` and read their feed at `/feed`.

### Frontend / design system
The portal is a "light editorial-intel" design — clean, print-like, authoritative. Styling lives in a hand-authored stylesheet `src/iceberg/static/css/iceberg.css` (oklch colour tokens, component classes like `.card` / `.btn` / `.tag` / `.board` / `.md`), served from the existing `/static` mount and linked in `base.html`. Tailwind CDN is still loaded (mapped to the same CSS variables) for utility classes, alongside Alpine.js. Three Google Fonts carry meaning: **Archivo** (UI/headings), **JetBrains Mono** (data/IDs/markings), **Spectral** (finished-product prose — the editor preview and published report body). The iceberg mark is inline SVG in `templates/_glyph.html` (included by `base.html` and `login.html`); active nav is derived from `request.url.path`. This is a skin over the same routes/forms/Alpine bindings — no behavioural coupling.

### Rendering
- Markdown → sanitized HTML (`src/iceberg/rendering/markdown.py`, markdown-it-py + nh3) for the live preview and portal display.
- Report → PDF: **via the Typst binary** (`src/iceberg/rendering/typst.py`). Markdown is rendered inside Typst by the `cmarker` package (fetched from the Typst registry on first use; version pinned in `src/iceberg/typst/product.typ`). If Typst is not installed, render endpoints return 503.
- **Decision:** rendering goes directly through Typst. Quarto (which can drive Typst as its PDF engine) was considered as a publishing layer but not adopted — direct Typst gives equivalent output with fewer moving parts. Revisit only if richer templating is needed.

### Project structure
```
src/iceberg/
  main.py          # app factory: mounts API + portal, session mw, auth redirect
  config.py        # pydantic-settings (ICEBERG_ env prefix)
  db.py            # SQLite engine/session, FK pragma, create_all
  models.py        # SQLModel models + enums
  schemas.py       # API request bodies
  templating.py    # shared Jinja2Templates instance
  auth/            # OIDC (Entra) + dev login, JWT, role dependencies
  api/             # JSON routers: notebooks, reports, requirements, feed, account, preview
  web/             # portal routes (Jinja2)
  services/        # users, lifecycle, citations/rendering, requirements, attachments, dissemination, email
  rendering/       # markdown->HTML, report->PDF
  templates/       # Jinja2 + Alpine (base, _glyph, _macros, one per screen)
  static/css/      # iceberg.css design system (served at /static/css/iceberg.css)
  typst/           # product.typ template
tests/             # pytest
```

## Running
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # adjust as needed
uvicorn iceberg.main:app --reload
# open http://localhost:8000 — use the dev login (or configure Entra)
```
Optional for PDF rendering: install the [`typst`](https://github.com/typst/typst) binary on PATH.

The portal's own styling ships as `static/css/iceberg.css`; Tailwind, Alpine and the Google Fonts are still loaded from CDNs (dev convenience). For production, self-host the fonts and replace the Tailwind CDN with a built stylesheet (the design system in `iceberg.css` already does most of the work).

## Testing
Test all crucial functionality with Pytest, create regression tests for identified bugs.
```bash
pytest
```
Tests use an in-memory SQLite database (overriding the `get_session` dependency) and the dev-login bypass. Coverage includes auth gating, notebook/source/note/report CRUD, citation scoping, the lifecycle state machine (including illegal transitions and published-report immutability), markdown preview sanitization, the full portal authoring flow (exercises the templates), requirement roles/ownership/tasking/traceability, attachment upload/download/delete with MIME + size validation and writer-only access (incl. report citation scoping + publish immutability), dissemination matching (intel level + TLP gate) with feed delivery / email outbox / read tracking / preferences, and a Typst render smoke test (skips when the binary is absent).

## Scope / roadmap
- **Milestone 1 (done)** — the authoring loop end-to-end: notebooks → sources/notes → report authoring with live preview → review/publish → Typst PDFs.
- **Milestone 2 (done)** — stakeholder requirement intake + analyst tasking board + report/notebook↔requirement traceability.
- **Milestone 3 (done)** — dissemination: on publish, match stakeholders by preferred intel level + TLP into a personalized feed, with email notifications (pluggable backend, sent via background task).

The original vision (collect → author → disseminate, aligned to stakeholder requirements) is now implemented end-to-end. Deployment is still dev-oriented; SQLite throughout (migrations via `create_all`, Alembic to follow once the schema stabilises). Production hardening to consider: a built Tailwind stylesheet (vs CDN), Alembic migrations, a real SMTP backend + durable job queue for notifications, and verifying the Entra OIDC flow against a live tenant.

## Maintenance
- Maintain an up to date CLAUDE.md
- Maintain an up to date README.md
