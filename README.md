# Iceberg 🧊

A cyber threat intelligence platform for **collecting** intelligence, **authoring**
finished intelligence products, and **disseminating** them to stakeholders.

Analysts work in topic **notebooks** — gathering sources, notes and uploaded
**attachments** (reference files) — and author **reports** (intelligence products)
in markdown. Reports carry an intelligence level (Strategic / Tactical /
Operational) and a TLP marking, cite sources and attachments, are classified with
**taxonomy tags** (threat actor / campaign / malware / ATT&CK technique / sector /
topic), move through a review workflow, and can be rendered to branded PDF products.
Everything is **searchable** — full-text + faceted across the report library.

> **Status:** Milestones 1–4 are implemented — the full vision plus a knowledge layer:
> the analyst authoring loop, stakeholder requirement intake + tasking board + traceability,
> dissemination (on publish, reports are matched to stakeholders by preferred intel level +
> TLP into a personalized feed, with email notifications), and an admin-curated tag taxonomy
> with full-text + faceted search. See [CLAUDE.md](CLAUDE.md).

## Stack
- **Python ≥ 3.14**, **FastAPI** (single app: JSON API `/api/*` + server-rendered portal `/*`)
- **SQLModel** on **SQLite**
- **Jinja2 + Alpine.js** portal with a "light editorial-intel" design system
  (`static/css/iceberg.css`; Archivo / JetBrains Mono / Spectral; Tailwind CDN for utilities)
- **markdown-it-py + nh3** for the live markdown preview
- **SQLite FTS5** (bm25) for full-text report search
- **Typst** for PDF rendering
- **PyTest** for tests
- Auth: **OIDC (Microsoft Entra ID)** with a dev-login bypass for local use

## Quick start
```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env        # tweak settings if you like
uvicorn iceberg.main:app --reload
```
Open <http://localhost:8000>. With `ICEBERG_DEV_AUTH=true` (the default) you'll see a
**dev login** on the sign-in page — pick a role (e.g. `ANALYST`) and continue.

### Try the authoring loop
1. Create a **notebook** from the dashboard.
2. Add a couple of **sources**, a **note**, and upload an **attachment** (e.g. a PDF).
3. Create an **intelligence product**, write markdown in the editor and watch the
   **live preview**; tick sources and attachments to cite them.
4. **Submit for review**, then sign in again as a `REVIEWER` to **Approve** and
   **Publish**.
5. **Render** a PDF (FULL / EXEC_BRIEF / ONE_PAGER) and download it.

### Try requirements & tasking
1. Sign in as a `STAKEHOLDER` → **My Requirements** → submit an intelligence requirement
   (title, priority, intel level).
2. Sign in as an `ANALYST` → **Tasking** to see the aggregated board; open a requirement
   and move its status (OPEN → IN_PROGRESS → SATISFIED).
3. In a report editor, tick the **Requirements satisfied**; the link shows up on the
   requirement's detail page (traceability). Notebooks can be linked the same way.

### Try dissemination
1. As a `STAKEHOLDER`, set your **Preferences** (preferred intel level, or "All levels").
2. As an `ANALYST`/`REVIEWER`, author and **publish** a report at that level (TLP AMBER or below).
3. Back as the stakeholder, your **Feed** shows the new report (with an unread badge on the
   dashboard); a notification email is recorded by the `console` backend (in-memory outbox).
   Reports marked TLP:RED or AMBER+STRICT are withheld from broadcast.

### Try tagging & search
1. Sign in as an `ADMIN` → **Taxonomy** (`/admin/tags`). A starter taxonomy (~94 tags: CISA
   sectors, intel topics, MITRE ATT&CK techniques, and example threat actors + malware) is
   seeded on first run; add or retire entries, or add a **campaign**.
2. As an `ANALYST`, open a report editor → **Tags** panel → tick tags to classify the product.
   (Tags stay editable even after the report is published.)
3. Use **Search** (top nav) — full-text query over title/body, narrowed by tag / kind / intel
   level / TLP / status facets. Click any tag chip to see everything classified with it.
   Stakeholders' searches only ever return published reports.

The starter taxonomy is bundled as data (`src/iceberg/data/starter_tags.json`) and imported
automatically on first boot. To (re-)import explicitly — e.g. after enriching the catalog or
to load your own vocabulary — run the idempotent import step:
```bash
python -m iceberg.seed            # or: iceberg-seed
python -m iceberg.seed --list     # preview the catalog without writing
python -m iceberg.seed --file my_tags.json --update
```

## Configuration
All settings use the `ICEBERG_` env prefix and can live in `.env` (see
[.env.example](.env.example)). Highlights:

| Variable | Purpose |
| --- | --- |
| `ICEBERG_SECRET_KEY` | JWT + session signing key (use a random 32+ byte value in prod) |
| `ICEBERG_DATABASE_URL` | SQLite URL, e.g. `sqlite:///./iceberg.db` |
| `ICEBERG_DEV_AUTH` | Enable the dev-login bypass (auto-off when `ICEBERG_ENVIRONMENT=prod`) |
| `ICEBERG_OIDC_ENABLED` + `ICEBERG_OIDC_*` | Microsoft Entra ID OIDC settings |
| `ICEBERG_TYPST_BIN` / `ICEBERG_RENDER_OUTPUT_DIR` | Typst binary + PDF output dir |
| `ICEBERG_ATTACHMENTS_DIR` / `ICEBERG_ATTACHMENT_MAX_MB` | Notebook attachment storage dir + size cap (default 25 MB) |
| `ICEBERG_ATTACHMENT_ALLOWED_TYPES` | Comma-separated MIME whitelist for uploads (override the default set) |
| `ICEBERG_DISSEMINATION_MAX_TLP` | Broadcast ceiling (default `AMBER`; RED/AMBER_STRICT withheld) |
| `ICEBERG_EMAIL_BACKEND` + `ICEBERG_SMTP_*` | `console` (dev) or `smtp`; SMTP server settings |
| `ICEBERG_PORTAL_BASE_URL` | Base URL used in notification email links |

### Entra ID (OIDC)
Set `ICEBERG_OIDC_ENABLED=true` and fill in `ICEBERG_OIDC_TENANT_ID`,
`ICEBERG_OIDC_CLIENT_ID`, `ICEBERG_OIDC_CLIENT_SECRET` and
`ICEBERG_OIDC_REDIRECT_URI`. Iceberg maps the app-role/group claim named by
`ICEBERG_OIDC_ROLE_CLAIM` to a role (`ADMIN`/`ANALYST`/`REVIEWER`/`STAKEHOLDER`),
defaulting unknown users to read-only `STAKEHOLDER`.

## PDF rendering (Typst)
Install the [`typst`](https://github.com/typst/typst) binary and ensure it's on
`PATH` (or set `ICEBERG_TYPST_BIN`). The template `src/iceberg/typst/product.typ`
uses the `cmarker` package, fetched from the Typst registry on first render
(needs network once). If the pinned version is unavailable for your Typst
install, change it at the top of that file. Render endpoints return **503** when
Typst is not installed.

## Tests
```bash
pytest
```
Tests run against in-memory SQLite using the dev-login bypass. The Typst render
test skips automatically when the binary isn't present.

## Project layout
See the structure diagram in [CLAUDE.md](CLAUDE.md).
