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
**ATT&CK Navigator layers** (per report and per actor/malware/campaign entity). A writer-only
**program maturity dashboard** rolls the same data up into CTI-CMM-style program-health
indicators. Security-relevant events are captured to a **structured-JSON audit log** (OWASP
application-logging shape) and can be **forwarded to a SIEM** (stdout/file, syslog, or HTTP
event collector) — configurable in the admin console.

> **Status:** Milestones 1–4 are implemented — the full vision plus a knowledge layer:
> the analyst authoring loop, stakeholder requirement intake + tasking board + traceability,
> dissemination (on publish, reports are matched to stakeholders by preferred intel level +
> TLP into a personalized feed, with email notifications) **closed by a stakeholder feedback /
> RFI-satisfaction loop**, and an admin-curated tag taxonomy with full-text + faceted search.
> See [CLAUDE.md](CLAUDE.md).

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
*Stakeholder requirements — typed as **PIR** (priority, decision-tied, time-bound), **GIR** (standing)
or **RFI** (ad-hoc) — aggregated into a tasking board grouped by status. Ordering blends urgency and
kind: a PIR is floored to at least High priority so it leads standing/ad-hoc work, but a genuine
Critical item still tops its column. A PIR coverage panel flags PIRs with no linked report/notebook
(collection gaps) or past their review-by date (overdue).*

### Dissemination to a stakeholder feed
![Intelligence feed](docs/images/feed.png)
*On publish, a report is matched to stakeholders by preferred intel level + TLP and delivered
to their personal feed (with an email notification).*

### Intelligence-cycle feedback loop
*On a product delivered to them, a stakeholder leaves **feedback** — a usefulness rating, an optional
**RFI-satisfaction** verdict against one of their own requirements the report addressed, and a comment.
A **Met** verdict from the owning stakeholder auto-advances that requirement to **Satisfied**, closing
the cycle. Feedback surfaces on the report (for authors) and the requirement detail (for analysts), and
its response / satisfaction / useful rates roll up into the maturity dashboard.*

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
**entity profile page** for those kinds (attribution + aliases + ATT&CK coverage + the reports
tagged with it).*

## Stack
- **Python ≥ 3.14**, **FastAPI** (single app: JSON API `/api/*` + server-rendered portal `/*`)
- **SQLModel** on **SQLite**
- **Jinja2 + Alpine.js** portal with a "command-center" design system (left rail + ⌘K palette)
  (`static/css/iceberg.css`; Archivo / JetBrains Mono / Spectral; a compiled Tailwind utility build)
  — Tailwind, Alpine and the fonts are **self-hosted, version-pinned and SRI-protected** (no CDN);
  regenerate with `python scripts/vendor_assets.py`
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
   (title, **kind** — PIR / GIR / RFI, priority, intel level). Choosing **PIR** reveals a
   decision-context note and a review-by date.
2. Sign in as an `ANALYST` → **Tasking** to see the aggregated board; PIRs lead standing/ad-hoc
   work (but a Critical item of any kind still tops its column), and the **PIR coverage panel**
   flags uncovered or overdue PIRs. Open a requirement and move its status
   (OPEN → IN_PROGRESS → SATISFIED).
3. In a report editor, tick the **Requirements satisfied**; the link shows up on the
   requirement's detail page (traceability) and clears the PIR's collection gap. Notebooks can be
   linked the same way.

### Try dissemination
1. As a `STAKEHOLDER`, set your **Preferences** (preferred intel level, or "All levels").
2. As an `ANALYST`/`REVIEWER`, author and **publish** a report at that level (TLP AMBER or below).
3. Back as the stakeholder, your **Feed** shows the new report (with an unread badge on the
   dashboard); a notification email is recorded by the `console` backend (in-memory outbox).
   Reports marked TLP:RED or AMBER+STRICT are withheld from broadcast.

### Try the feedback loop
1. Before publishing, have the `ANALYST` tick the **Requirements satisfied** so the report addresses
   one of the stakeholder's requirements; then publish so it disseminates to that stakeholder.
2. As the `STAKEHOLDER`, open the report from your **Feed** → the **Your feedback** card: rate its
   usefulness, pick the requirement it satisfied, mark it **Met**, and send. The requirement jumps
   straight to **Satisfied** — the cycle is closed.
3. As the `ANALYST`/`REVIEWER`, the report view shows a **Product feedback** panel and the
   requirement detail shows the verdict; **Maturity** picks up the new response / satisfaction rates.

### Try tagging & search
1. Sign in as an `ADMIN` → **Taxonomy** (`/admin/tags`). A starter taxonomy (~94 tags: CISA
   sectors, intel topics, MITRE ATT&CK techniques, and example threat actors + malware) is
   seeded on first run; add or retire entries, or add a **campaign**. For actor / malware /
   campaign terms, list **aliases** (comma-separated) so alternate names resolve to one entity, and
   record **attribution** (suspected sponsor/country, motivation, first/last seen).
2. As an `ANALYST`, open a report editor → **Tags** panel → tick tags to classify the product.
   (Tags stay editable even after the report is published.)
3. Use **Search** (left rail, or ⌘K) — full-text query over title/body, narrowed by tag / kind / intel
   level / TLP / status facets. Search is **alias-aware** — querying an alias (e.g. "Fancy Bear")
   surfaces reports tagged with the canonical entity. Click a named-threat tag chip to open its
   **entity profile** (attribution + aliases + ATT&CK coverage + the reports tagged with it).
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

### Gauge program maturity & effectiveness
1. Open **Maturity** (left rail, `/maturity`) — a writer-only, leadership-facing dashboard that
   derives program-health indicators purely from existing data: production (publish velocity,
   time-to-publish, reviewer engagement), requirement coverage across all kinds (PIR/GIR/RFI),
   dissemination reach (stakeholders reached, feed read-rate, TLP-withheld, plus feedback-loop
   response / satisfaction / useful rates), and tradecraft
   adoption (share of published reports using source grading, structured judgements, analytic
   confidence, embedded analytic models and ATT&CK tags).
2. The page tops it with an **indicative [CTI-CMM](https://cti-cmm.org/) maturity rollup** —
   four capability dimensions scored CTI0 (Pre-foundational) → CTI3 (Leading) by thresholds.
   It is evidence to inform a self-assessment, **not a substitute** for a formal one.

### Forward security events to your SIEM
1. Sign in as **ADMIN** and open **Audit log** (left rail, `/admin/audit`). Security-relevant
   events — logins/logouts, authorization denials and CSRF blocks, report lifecycle transitions,
   admin taxonomy edits, and sensitive-file access — are recorded to a local trail **and** emitted
   as structured JSON (OWASP application-logging shape).
2. Choose one or more **emit methods** — `stdout`/file (for a sidecar shipper), **syslog** (RFC 5424
   over UDP/TCP), or an **HTTP event collector** (Splunk HEC / Elastic / webhook) — set the endpoints
   and a minimum severity, and **Save**. The HTTP/HEC token is read from `ICEBERG_AUDIT_HTTP_TOKEN`
   and is never stored in the database.
3. Click **Send test event** to verify connectivity end-to-end, then watch the filterable event
   trail on the same page. A failing/unreachable SIEM never blocks a request — events still persist
   locally and forward off the response path.

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
| `ICEBERG_AUDIT_ENABLED` + `ICEBERG_AUDIT_METHODS` | Master switch + default SIEM emit methods (`stdout`/`syslog`/`http`); editable live at `/admin/audit` |
| `ICEBERG_AUDIT_SYSLOG_*` / `ICEBERG_AUDIT_HTTP_ENDPOINT` | syslog (RFC 5424) host/port/protocol + HTTP event-collector endpoint defaults |
| `ICEBERG_AUDIT_HTTP_TOKEN` | **Secret** HEC/bearer token for the HTTP SIEM method (env-only — never stored in the DB) |

### Source reliability grading
Notebook sources carry Admiralty/NATO-style grades: source reliability (`A-F`) plus
information credibility (`1-6`), displayed as chips such as `B2` or `B6`. Grading is a
**fully offline local heuristic** applied inline when a source is added: reliability is
inferred from the source identity (recognised publisher domain or named authority) and
credibility from the analyst's summary. If only the source identity can be judged,
credibility is marked `6` ("cannot be judged"). There is no outbound network fetch and no
external LLM provider — analysts can always manually override, clear, or regrade a source.

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
with `vulture_whitelist.py` for framework false positives), `pip-audit --skip-editable`
(fails on a known CVE in any installed dependency — the version floors in `pyproject.toml`
are not a lockfile), plus **frontend lint**: `djlint src/iceberg/templates --lint` (Jinja/HTML
structure, configured under `[tool.djlint]`) and `biome lint src/iceberg/static` (the
hand-authored CSS + Alpine component JS, vendored assets excluded; configured in `biome.jsonc`).
A third **assets** job re-runs `scripts/vendor_assets.py` and fails on any drift, so the
self-hosted, SRI-protected Tailwind/Alpine/font assets always match their pinned versions.
Third-party actions are **pinned to commit SHAs** (with a tracking version comment), and
[Dependabot](.github/dependabot.yml) keeps the Python dependencies and those action pins current.
Reproduce the local gates with `pip install -e ".[dev]"` then the commands above.

The static gates also run automatically on every commit via
[pre-commit](.pre-commit-config.yaml) — `repo: local` hooks that invoke the same pinned dev
tools, so local and CI results match. Activate them once per clone:
```bash
pip install -e ".[dev]"
pre-commit install                  # wire the git pre-commit hook
pre-commit run --all-files          # optional: run them on demand
```
[Biome](https://biomejs.dev) is the one gate not in the pip dev extra — it ships as a standalone
binary (no Node toolchain). CI installs it via `biomejs/setup-biome`; for the local hook, drop the
binary on your `PATH` (the pre-commit hook no-ops if it's absent):
```bash
curl -fsSL -o ~/.local/bin/biome \
  https://github.com/biomejs/biome/releases/download/@biomejs/biome@2.5.0/biome-linux-x64
chmod +x ~/.local/bin/biome
```

## Project layout
See the structure diagram in [CLAUDE.md](CLAUDE.md).
