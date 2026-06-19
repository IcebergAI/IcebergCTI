# Iceberg 🧊

[![CI](https://github.com/TheSlopBucket/iceberg/actions/workflows/ci.yml/badge.svg)](https://github.com/TheSlopBucket/iceberg/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.14-blue.svg)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A cyber threat intelligence platform for **collecting** intelligence, **authoring**
finished intelligence products, and **disseminating** them to stakeholders.

Analysts work in topic **notebooks** — gathering sources, notes and uploaded
**attachments** (reference files), and applying structured analytic techniques (the
**Diamond Model** of Intrusion Analysis and **Analysis of Competing Hypotheses (ACH)**) —
and author **reports** (intelligence products) in markdown. Reports carry
an intelligence level (Strategic / Tactical / Operational) and a TLP marking, cite
sources and attachments, carry Admiralty/NATO-style **source reliability grading**,
**embed Diamond Model diagrams, ACH matrices and figures (images) inline**, are classified with **taxonomy tags** (threat
actor / campaign / malware / ATT&CK technique / sector / topic), move through a review
workflow, and can be rendered to branded PDF products.
Everything is **searchable** — full-text + faceted across the report library — and the
ATT&CK techniques tagged across reports drive a **coverage heatmap** and downloadable
**ATT&CK Navigator layers** (per report and per actor/malware/campaign entity).

> **Status:** Milestones 1–4 are implemented — the full vision plus a knowledge layer:
> the analyst authoring loop, stakeholder requirement intake + tasking board + traceability,
> dissemination (on publish, reports are matched to stakeholders by preferred intel level +
> TLP into a personalized feed, with email notifications), and an admin-curated tag taxonomy
> with full-text + faceted search. See [CLAUDE.md](CLAUDE.md).

## Screenshots

The portal is a server-rendered "command-center" design system (Archivo /
JetBrains Mono / Spectral) — a persistent role-aware left rail + topbar + scrolling
canvas, a ⌘K command palette, and a full-height 3-pane report editor. The views below
use realistic sample data.

### The analyst workspace
![Dashboard](docs/images/dashboard.png)
*Notebooks in collection, reports in flight, and the most recent products — all in one place.*

### The report library
![Report library](docs/images/reports-list.png)
*Every intelligence product with its status, intelligence level, TLP marking and taxonomy chips.*

### Authoring with a live preview
![Report editor](docs/images/report-editor.png)
*The report editor: markdown with a side-by-side live preview, source/attachment citations,
requirement traceability, and taxonomy tagging — all on one screen.*

### The finished intelligence product
![Published report](docs/images/report-view.png)
*A published report — TLP and intelligence-level markings, taxonomy chips, numbered sources,
cited attachments, and on-demand PDF products.*

### Typeset to a branded PDF (Typst)
![Sample PDF product](docs/images/pdf-sample.png)
*The same report rendered to PDF via Typst — classification markings, masthead and the
taxonomy stamp carried through. [Download the full sample »](docs/sample-report-volt-typhoon.pdf)*

### Requirements → analyst tasking
![Tasking board](docs/images/tasking-board.png)
*Stakeholder requirements aggregated into a priority-ordered tasking board, grouped by status.*

### Dissemination to a stakeholder feed
![Intelligence feed](docs/images/feed.png)
*On publish, a report is matched to stakeholders by preferred intel level + TLP and delivered
to their personal feed (with an email notification).*

### Full-text + faceted search
![Search](docs/images/search.png)
*Full-text search over the report library (SQLite FTS5, bm25), narrowed by tag / kind /
intel-level / TLP / status facets — access-scoped so stakeholders only ever match published reports.
**Alias-aware:** a search for "Fancy Bear" surfaces reports tagged **APT28** even when the body never names the alias.*

### Admin-curated tag taxonomy
![Taxonomy](docs/images/taxonomy.png)
*The controlled vocabulary — threat actor / campaign / malware / ATT&CK technique / sector /
topic — that analysts classify reports against. Named-threat entities (actor / malware / campaign)
carry structured **aliases** so APT28 / Fancy Bear / Sofacy resolve to one entity, plus structured
**attribution** (suspected sponsor/country, motivation, first/last seen) — `/tags/{id}` is a proper
**entity profile page** for those kinds. STIX-shaped **entity relationships**
(actor → uses → malware, campaign → attributed-to → actor, actor → targets → sector), curated at
`/admin/relationships`, render on the profile as inbound/outbound chips + an SVG mini-graph.*

## Stack
- **Python ≥ 3.14**, **FastAPI** (single app: JSON API `/api/*` + server-rendered portal `/*`)
- **SQLModel** on **SQLite**
- **Jinja2 + Alpine.js** portal with a "command-center" design system (left rail + ⌘K palette)
  (`static/css/iceberg.css`; Archivo / JetBrains Mono / Spectral; Tailwind CDN for utilities)
- **markdown-it-py + nh3** for the live markdown preview
- **SQLite FTS5** (bm25) for full-text report search
- **Typst** for PDF rendering
- **PyTest** for tests
- Auth: **OIDC (Microsoft Entra ID)** with a dev-login bypass for local use; role-based
  access (notebook collection material is writer-only, stakeholders consume finished
  products) with a same-origin CSRF guard on the cookie-authenticated portal

## Quick start
```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env        # tweak settings if you like
uvicorn iceberg.main:app --reload
```
Open <http://localhost:8000>. With `ICEBERG_DEV_AUTH=true` (the default) you'll see a
**dev login** on the sign-in page — pick a role (e.g. `ANALYST`) and continue. The schema is
created automatically on first boot (`ICEBERG_AUTO_MIGRATE=true` runs migrations for you).

**Get oriented:** open **Help** in the nav (`/help`) for a guide to your role's workflow,
a browsable look at what the other roles do, and a glossary of the intelligence concepts
(TLP, intel levels, source grading, the Diamond Model, ACH, ICD 203 judgements, dissemination).

### Try the authoring loop
1. Create a **notebook** from the dashboard.
2. Add a couple of **sources**, a **note**, and upload an **attachment** (e.g. a PDF).
   Sources are auto-graded when Iceberg can infer enough signal; analysts can override
   or clear the Admiralty/NATO reliability + credibility chip.
3. Create an **intelligence product**, write markdown in the editor and watch the
   **live preview**; tick sources and attachments to cite them.
4. Fill the **analytic scaffolding** (ICD 203) — **Key Judgements** (the BLUF),
   **Key Assumptions** and **Intelligence Gaps**. They render as discrete sections
   on the report page. Optionally set the **analytic confidence** (LOW/MODERATE/HIGH),
   stamped on the masthead; phrase event likelihood in prose using the editor's
   **probability yardstick**.
5. **Submit for review**, then sign in again as a `REVIEWER` to **Approve** and
   **Publish**.
6. **Render** a PDF and download it: **FULL** (judgements + body + caveats +
   appendix) or **EXEC_BRIEF / ONE_PAGER** (Key-Judgements-only briefs).

### Model an intrusion (Diamond Model)
1. In a notebook, open the **Diamond models** section and add one — adversary,
   capability, infrastructure, victim and an analytic confidence. The **Edit** page
   shows a live SVG preview of the diagram as you type.
2. In a report editor, click **Insert** next to the model (or type its
   `[[diamond:ID]]` token) to embed the diagram **inline at that point** in the body.
3. The diagram renders in the live preview, the published report page, and the
   Typst PDF — all from one server-generated SVG.

### Weigh competing hypotheses (ACH)
1. In a notebook, open the **ACH analyses** section and add one. On the **Edit** page,
   pose the key intelligence question, add the competing **hypotheses** (columns) and
   the **evidence** (rows), and rate each cell for consistency. A live SVG preview of
   the matrix — with the **least-inconsistent (most tenable)** hypothesis flagged —
   updates as you go.
2. In a report editor, click **Insert at cursor** next to the analysis (or type its
   `[[ach:ID]]` token) to embed the matrix **inline at that point** in the body.
3. The matrix renders in the live preview, the published report page, and the Typst
   PDF — all from one server-generated SVG.

### Embed an image (figure)
1. In a notebook, open the **Figures** section and upload an image (PNG/JPEG/GIF).
2. In a report editor, click **Insert at cursor** next to the figure (or type its
   `[[figure:ID]]` token) to embed the image **inline at that point** in the body.
3. The image renders in the live preview, the published report page (as an inline
   `data:` URI), and the Typst PDF — all from the one upload.

### Embed the report's ATT&CK coverage matrix
1. Tag the report with **ATT&CK technique** taxonomy terms (the report's coverage is
   derived from its own tags).
2. In a report editor, open **Insert ▾ → ATT&CK coverage matrix** (or type the bare
   `[[attack]]` token) to embed the technique-coverage heatmap **inline at that point**
   in the body. Unlike the diamond/figure/ach tokens it takes no ID.
3. The matrix renders as a server-generated SVG in the live preview, the published
   report page, and the Typst PDF; a report with no technique tags shows an
   "unavailable" notice.

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
   seeded on first run; add or retire entries, or add a **campaign**. For actor / malware /
   campaign terms, list **aliases** (comma-separated) so alternate names resolve to one entity, and
   record **attribution** (suspected sponsor/country, motivation, first/last seen). Then open
   **Entity relationships** (`/admin/relationships`) to link entities with STIX verbs
   (actor → uses → malware, campaign → attributed-to → actor, actor → targets → sector).
2. As an `ANALYST`, open a report editor → **Tags** panel → tick tags to classify the product.
   (Tags stay editable even after the report is published.)
3. Use **Search** (left rail, or ⌘K) — full-text query over title/body, narrowed by tag / kind / intel
   level / TLP / status facets. Search is **alias-aware** — querying an alias (e.g. "Fancy Bear")
   surfaces reports tagged with the canonical entity. Click a named-threat tag chip to open its
   **entity profile** (attribution + aliases + inbound/outbound relationship chips + an SVG
   mini-graph + the reports tagged with it).
   Stakeholders' searches only ever return published reports.

### See ATT&CK coverage & export a Navigator layer
1. Tag reports with **TECHNIQUE** taxonomy terms (they carry MITRE ATT&CK T-codes).
2. Open **Matrix** (left rail, `/matrix`) for a technique-coverage heatmap across all visible
   reports, grouped by ATT&CK tactic and shaded by how many reports exhibit each technique.
   An entity profile shows the same heatmap scoped to that actor/malware/campaign.
3. Download an **ATT&CK Navigator layer** (`.json`) — per report (from the report's *Downloads*)
   or aggregated per entity (from the entity profile) — and open it in
   [ATT&CK Navigator](https://mitre-attack.github.io/attack-navigator/). Stakeholders' coverage
   and exports only ever count published reports.
4. Embed a report's *own* coverage matrix inline with the bare `[[attack]]` token (see
   *Embed the report's ATT&CK coverage matrix* above) so the heatmap appears in the finished
   product itself.

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
| `ICEBERG_FIGURES_DIR` / `ICEBERG_FIGURE_MAX_MB` | Notebook figure (embeddable image) storage dir + size cap (default 10 MB) |
| `ICEBERG_DISSEMINATION_MAX_TLP` | Broadcast ceiling (default `AMBER`; RED/AMBER_STRICT withheld) |
| `ICEBERG_EMAIL_BACKEND` + `ICEBERG_SMTP_*` | `console` (dev) or `smtp`; SMTP server settings |
| `ICEBERG_PORTAL_BASE_URL` | Base URL used in notification email links |
| `ICEBERG_SOURCE_GRADER_PROVIDER` | `heuristic` by default; opt into `openai`, `anthropic`, or `openai_compatible` |
| `ICEBERG_SOURCE_GRADER_MODEL` / `ICEBERG_SOURCE_GRADER_API_KEY` | Model/key for external source grading |
| `ICEBERG_SOURCE_GRADER_BASE_URL` | Optional OpenAI-compatible or alternate provider base URL |
| `ICEBERG_SOURCE_GRADER_FALLBACK` | `heuristic` by default; controls local fallback after fetch/provider failure |

### Source reliability grading
Notebook sources carry Admiralty/NATO-style grades: source reliability (`A-F`) plus
information credibility (`1-6`), displayed as chips such as `B2` or `B6`. Auto-grading
is conservative: Iceberg safely fetches public HTTP(S) source pages, extracts readable
text, and then uses the configured grader. Without provider config, or when fetching/LLM
grading fails, it falls back to a local heuristic. If only the source identity can be
judged, credibility is marked `6` ("cannot be judged"); when URL fetching fails, the
notebook page shows a compact warning after grade/regrade. External LLM grading sends
only source metadata and extracted page text, never notebook notes, report bodies,
attachments, or stakeholder data.

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
Typst is not installed. A rendered example ships at
[docs/sample-report-volt-typhoon.pdf](docs/sample-report-volt-typhoon.pdf).

## Database migrations
Schema is managed by **Alembic** (`src/iceberg/migrations/`); SQLModel models are the source
of truth. By default `init_db()` runs `alembic upgrade head` on boot — set
`ICEBERG_AUTO_MIGRATE=false` in production and migrate explicitly in the deploy step.

```bash
alembic upgrade head                          # apply migrations to ICEBERG_DATABASE_URL
alembic revision --autogenerate -m "add x"    # create a migration after changing a model
alembic downgrade -1                          # roll back one revision
```
The baseline migration also owns the SQLite FTS5 search objects (the `report_fts` virtual
table + sync triggers). A database created by an older `create_all` build has the right tables
but no version row — run **`alembic stamp head`** once to mark it current before upgrading.

## Tests
```bash
pytest                              # run the suite
pytest --cov=iceberg --cov-report=term-missing   # with coverage (CI gates on a floor)
```
Tests run against in-memory SQLite using the dev-login bypass; `tests/test_migrations.py`
additionally applies the real migrations to a temp database and checks the models haven't
drifted from them. The Typst render test skips automatically when the binary isn't present.

## Continuous integration
[CI](.github/workflows/ci.yml) runs on every push to `main` and on pull requests: a **test**
job (`pytest` + coverage, with Typst installed so the PDF-render path is exercised; coverage is
gated by `fail_under` in `pyproject.toml`) and a **static** job — `ruff check` (lint),
`bandit -r src/iceberg` (security), `vulture` (dead code; configured under `[tool.vulture]`
with `vulture_whitelist.py` for framework false positives), and `pip-audit --skip-editable`
(fails on a known CVE in any installed dependency — the version floors in `pyproject.toml`
are not a lockfile). Third-party actions are **pinned
to commit SHAs** (with a tracking version comment), and [Dependabot](.github/dependabot.yml)
keeps the Python dependencies and those action pins current. Reproduce the local gates with
`pip install -e ".[dev]"` then the commands above.

## Project layout
See the structure diagram in [CLAUDE.md](CLAUDE.md).
