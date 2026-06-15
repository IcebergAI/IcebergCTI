# Iceberg

## Overview
Iceberg is a cyber threat intelligence platform for collecting threat intelligence and, authoring and disseminating finished reports and artefacts to stakeholders. It follows the traditional Strategic, Tactical and Operational model for classifying intelligence levels. Iceberg also defines stakeholders and allows aligning intelligence goals with stakeholder requirements e.g. stakeholders are readonly users that can record intelligence goals which can be aggregated for analyst tasking. Stakeholders also define their preferred intelligence level and this drives dissemination. Classification of reports follows the TLP protocol. Reports are authored in markdown and published as finished reports in the portal. Iceberg doesn't deal directly in IOCs (though these may be surfaced in reports)

Collection is notebook-based: analysts open a topic **notebook**, gather **sources**, **notes** and uploaded **attachments** in it, then author one or more **intelligence products** (reports) from that material.

## Architecture
API-first design consumed by a server-rendered portal; all endpoints authenticated using JWT.

A **single FastAPI application** serves both the JSON API under `/api/*` and the server-rendered portal under `/*`. (The earlier "Quart" web framework was dropped in favour of this single-app design.)

Authentication is **OIDC against Microsoft Entra ID** (Authorization Code flow); the IdP's app-role/group claim maps to an Iceberg role. After login Iceberg mints its own short-lived JWT — sent as a Bearer header by API clients, or stored in a signed session cookie by the portal — so the "all endpoints JWT-authenticated" rule holds uniformly. A **dev-login bypass** (`ICEBERG_DEV_AUTH=true`, disabled when `ICEBERG_ENVIRONMENT=prod`) issues a JWT for a chosen role without an IdP, for local development and tests.

The session cookie is `SameSite=Lax` + `HttpOnly` (and `Secure` in prod); as defence-in-depth a **same-origin CSRF middleware** (`auth/csrf.py`) rejects any cookie-authenticated state-changing request whose `Origin`/`Referer` doesn't match the host (Bearer API clients and anonymous requests are exempt — token auth isn't browser-CSRF-prone). Logout is **POST-only** so it can't be triggered cross-site.

Roles: `ADMIN`, `ANALYST`, `REVIEWER`, `STAKEHOLDER` (read-only).

### Technologies
- Python >= 3.14
- FastAPI (API + portal), SQLModel (SQLite), PyTest
- Jinja2 + Tailwind CSS + Alpine JS (portal)
- Typst — report → PDF typesetting (rendering goes directly through the Typst binary; Quarto was considered as a publishing layer but direct Typst was chosen — equivalent output, fewer dependencies)

### Domain model (`src/iceberg/models.py`)
- **User** — identity, role, optional `preferred_intel_level`.
- **Notebook** — topic workspace owned by an analyst; has many sources, notes and reports. Collection material (the notebook, its sources/notes/attachments/diamonds) is **writer-only**: read-only stakeholders never list or open notebooks — they consume only finished products (reports/feed/search). Create/read helpers live in `services/notebooks.py` and are shared by the API and portal.
- **Source** / **Note** — collected material inside a notebook. Sources carry
  Admiralty/NATO-style grading (`reliability` A-F + `credibility` 1-6), with
  provenance (`UNGRADED` / `AUTO` / `MANUAL`), rationale, and non-fatal grading
  warnings. `services/source_grading.py` owns safe public-URL fetching, opt-in LLM
  grading (OpenAI / Anthropic / OpenAI-compatible), local heuristic fallback, and
  manual override/clear.
- **Attachment** — an uploaded reference file held against a notebook. Stored on disk under `ICEBERG_ATTACHMENTS_DIR` with a server-generated UUID name; the DB row keeps metadata + the original filename. Upload/download are **writer-only** (read-only stakeholders have no access); uploads are MIME-whitelisted and size-capped (`ICEBERG_ATTACHMENT_MAX_MB`, default 25). Citable in reports via `ReportAttachment` and listed in the rendered PDF's appendix. See `services/attachments.py`.
- **DiamondModel** — a Diamond Model of Intrusion Analysis assessment held against a notebook (the four core features `adversary`/`capability`/`infrastructure`/`victim` + an analytic `confidence` + notes). Rendered server-side to an **SVG** diagram and **embedded inline in a report** by writing a `[[diamond:ID]]` token in the markdown body — there is **no citation link table**; the token is the association, resolved (notebook-scoped) at render time for the web view, the live preview, and the Typst PDF (Typst renders SVG natively; vertex text is XML-escaped). See `services/diamond.py`.
- **Figure** — an uploaded image (PNG/JPEG/GIF) held against a notebook and **embedded inline in a report** by writing a `[[figure:ID]]` token in the markdown body. Like DiamondModel there is **no citation link table** — the token is the association, resolved (notebook-scoped) at render time. Stored on disk under `ICEBERG_FIGURES_DIR` with a server-generated UUID name (capped by `ICEBERG_FIGURE_MAX_MB`, default 10); the figures **collection** is writer-only. Inlined as a base64 `data:` URI in the web view / live preview (injected after nh3 sanitisation, like diamond SVGs) and copied into the Typst `--root` for the PDF. See `services/figures.py`.
- **Report** (intelligence product) — markdown body, `intel_level` (STRATEGIC/TACTICAL/OPERATIONAL), `tlp`, lifecycle `status`, author/reviewer. Also carries optional **ICD 203 structured-judgement scaffolding** — `key_judgements` (the BLUF), `key_assumptions`, `intelligence_gaps` (all markdown) — rendered as discrete sections in the web view and PDF, plus an optional **`analytic_confidence`** (`AnalyticConfidence` LOW/MODERATE/HIGH; nullable = "not stated") — ICD 203 estimative language: confidence *in the judgements*, stamped as a calm neutral marking on the masthead beside TLP/status (web view `confidence_badge` macro + `product.typ`, all formats). *Likelihood* of an assessed event is expressed in prose via the **probability yardstick** (a controlled lexicon → percentage bands, single-sourced in `help_content.py` as `PROBABILITY_YARDSTICK`, surfaced as a collapsible reference panel in the editor and the `estimative-language` glossary entry); the hedging lint is deferred. The web report view and the editor's live/read-only preview share one assembler (`product_html.render_report_product_html` / `preview_report_product_html`, the latter behind `POST /api/preview/product`) so the finished product (Key Judgements callout + body-with-diagrams-and-figures + Assumptions + Gaps) never drifts between them. Cites a subset of its notebook's sources (`ReportSource`) and attachments (`ReportAttachment`), embeds notebook Diamond Models and figures inline via `[[diamond:ID]]` / `[[figure:ID]]` tokens, and is classified with taxonomy tags (`ReportTag`).
- **RenderedProduct** — an on-demand PDF for a report (FULL / EXEC_BRIEF / ONE_PAGER). **FULL** renders the masthead, Key Judgements, body, then Key Assumptions + Intelligence Gaps, then the sources/attachments appendix; the **EXEC_BRIEF / ONE_PAGER** are *Key-Judgements-only* products (masthead + markings + judgements, body and caveats omitted).
- **Requirement** — stakeholder PIR/RFI with `priority` + `status`, feeding the analyst tasking board. Traced to the reports/notebooks that satisfy/address it via `ReportRequirement` / `NotebookRequirement`.
- **Tag** — a term in the controlled CTI taxonomy (`TagKind`: ACTOR/CAMPAIGN/MALWARE/TECHNIQUE/SECTOR/TOPIC; ATT&CK techniques carry their T-code in `external_id`). **Admin-curated** — analysts only *select* tags when classifying a report, never create them; `active=False` retires a tag (kept on historical reports, no longer offered). The "named-threat" kinds (ACTOR/MALWARE/CAMPAIGN — `tags.ALIASABLE_KINDS`) carry a structured **`aliases`** list (a JSON column on `Tag`) so the APT28 / Fancy Bear / Sofacy naming problem resolves to one entity; aliases drive **alias-aware search** (see *Tagging & search*) and render as "Also known as". The same kinds also carry **structured attribution** (roadmap 2b): `suspected_attribution` (free-text sponsor/country), `motivations` (a JSON list validated against the `Motivation` enum — ESPIONAGE/FINANCIAL/HACKTIVISM/DESTRUCTIVE/INFLUENCE, multi-valued via `tags.normalise_motivations`), and free-text fuzzy `first_seen`/`last_seen`. For these kinds `/tags/{id}` renders a dedicated **entity profile** (`templates/entity_profile.html`) — attribution panel + motivation chips + aliases + an ATT&CK link off `external_id` (G-/S-code) + the reports-tagged list; non-named kinds keep the plain `search.html` drill-down. Attribution is admin-edited in `/admin/tags` (gated to named-threat kinds, like aliases) and seeded from `data/starter_tags.json`. See `services/tags.py`.
- **DisseminationEvent** — a published report delivered to a stakeholder's feed, with read tracking.

### Report lifecycle (`src/iceberg/services/lifecycle.py`)
`DRAFT → IN_REVIEW → APPROVED → PUBLISHED`, with send-back paths for rework. The author submits their own draft; a `REVIEWER`/`ADMIN` approves, sends back, or publishes. Publishing stamps `published_at`. Published reports are immutable.

### Requirements & analyst tasking (`src/iceberg/api/requirements.py`, `services/requirements.py`)
Stakeholders (read-only for intel) **submit requirements**; they see only their own. Analysts/reviewers/admins see the **aggregated tasking board** (`/requirements`), grouped by status and ordered by priority, and drive each requirement's `status` (OPEN/IN_PROGRESS/SATISFIED/CLOSED). Analysts establish **traceability** by ticking the requirements a report satisfies (in the report editor) or a notebook addresses (on the notebook page); the requirement detail page shows the linked reports/notebooks. Role split: only stakeholders/admins create requirements; only analysts/reviewers/admins change status and create links; only the owning stakeholder (or admin) edits/deletes a requirement's fields.

### TLP & intelligence level
TLP is a **display marking + a dissemination-routing input**; it does **not** gate in-portal read access (any authenticated user may browse published reports). `intel_level` is a classification tag used for dissemination matching. Product format (full/brief/one-pager) is chosen on demand and is independent of `intel_level`.

### Source reliability grading (`src/iceberg/services/source_grading.py`)
Notebook sources are graded with the Admiralty/NATO system and shown as compact
chips (e.g. `B2`, `B6`) on notebook rows, report citation pickers, report source
lists, and the FULL PDF appendix. Auto-grading is conservative: Iceberg fetches
only public HTTP(S) URLs, rejects localhost/private/internal targets and unsafe
redirects, caps time/bytes/text, extracts readable HTML/text, and sends only source
metadata + extracted source text to an external provider when configured. If content
cannot be read, heuristics may still grade source reliability from the reference or
domain, but credibility stays `6` ("cannot be judged"). External grading is opt-in
via `ICEBERG_SOURCE_GRADER_PROVIDER` (`heuristic` default, `openai`, `anthropic`,
or `openai_compatible`) plus model/key/base URL config; failures fall back to
`heuristic:v1`. Analysts can manually override, clear, or regrade sources.

Auto-grading that would touch the network (fetching a URL, or calling an LLM
provider) runs **after** the create response as a FastAPI background task —
mirroring the dissemination email pattern — so adding a source never blocks on an
external host. Such a source is created `PENDING` (a transient "Grading…" chip)
and flips to `AUTO` once the task resolves; the portal is server-rendered, so the
chip refreshes on the next page load. Manual grades and the pure-offline heuristic
path (no URL, `heuristic` provider) stay synchronous. SSRF safety on the fetch path:
DNS is resolved once and the connection is pinned to that validated public IP
(hostname preserved for `Host`/TLS SNI), closing the DNS-rebinding window, and the
body is streamed so the byte cap holds even without a `Content-Length`.

### Tagging & search (`src/iceberg/services/tags.py`, `services/search.py`)
A **controlled tag taxonomy** classifies reports by threat actor / campaign / malware / ATT&CK technique / sector / topic. The vocabulary is **admin-curated** (only `ADMIN` creates/edits/retires tags, at `/admin/tags`); analysts *select* from it in the report editor. Tags are classification metadata and are **deliberately editable after a report is published** (CTI re-tags retrospectively) — the tag endpoints guard on `ensure_author` only, not `ensure_editable`. Tags surface as kind-tinted chips in the portal (report view / list / search) and are stamped onto the **rendered PDF** below the masthead (`typst/product.typ` `tag-chip`).

The **starter taxonomy** ships as data (`src/iceberg/data/starter_tags.json`, ~94 entries: CISA sectors, cross-cutting topics, a curated ATT&CK Enterprise technique set, and example threat actors + malware with their ATT&CK G-/S-ids). It is loaded by `load_starter_tags()` and imported idempotently (matched on `(kind, slug)`) by `seed_default_taxonomy()` — run automatically on first boot via `init_db`, and re-runnable as an explicit **import step**: `python -m iceberg.seed` (or the `iceberg-seed` console script), with `--file` (custom taxonomy), `--update` (refresh metadata on existing tags), and `--list` (dry-run summary). CAMPAIGN tags start empty (org-specific events for admins to add).

**Search** (`/search`, `GET /api/search`) is full-text over report title + body + the ICD 203 judgement scaffolding (`key_judgements`/`key_assumptions`/`intelligence_gaps`) via **SQLite FTS5** (bm25-ranked), plus facets (tag, kind, intel_level, tlp, status). Indexed columns mirror `report` column names so FTS5 external-content `rebuild` backfills them. The FTS table + sync triggers are created by an `after_create` event on the `report` table, so they build automatically for both `init_db()` and the in-memory test engine. Search is also **alias-aware** (roadmap 2a): a free-text query is *additionally* resolved against named-threat tag labels + `aliases` (`tags.resolve_alias_report_ids`, a Python scan — the vocabulary is small), and reports tagged with a matching entity are **appended after the bm25 body matches**, so "Fancy Bear" finds APT28-tagged reports even when the body never names the alias. The tag text is deliberately *not* denormalised into the FTS index (that would force re-syncing `report_fts` on every tag edit) — alias resolution stays a query-time tag lookup in `search_reports`, so there is **no FTS DDL change**. **Access control:** stakeholders only ever match *published* reports — `search_reports` reapplies `ensure_visible`'s rule so search can't leak unpublished material.

### Dissemination (`src/iceberg/services/dissemination.py`, `services/email.py`)
On publish, Iceberg matches stakeholders and delivers the report to their **feed**: a stakeholder matches when (a) the report is broadcast-eligible under the TLP ceiling — reports at or below `ICEBERG_DISSEMINATION_MAX_TLP` (default AMBER) are disseminated, while RED / AMBER+STRICT are withheld — **and** (b) the report's `intel_level` equals the stakeholder's `preferred_intel_level` (or the stakeholder has set no preference = all levels). Feed delivery is recorded synchronously as `DisseminationEvent`s; an **email notification** is sent as a FastAPI background task. Email uses a pluggable backend (`ICEBERG_EMAIL_BACKEND`): `console` (default — records to an in-memory outbox + logs, for dev/tests) or `smtp`. Stakeholders set their preference at `/preferences` and read their feed at `/feed`.

### Frontend / design system
The portal is a "light editorial-intel" design — clean, print-like, authoritative. Styling lives in a hand-authored stylesheet `src/iceberg/static/css/iceberg.css` (oklch colour tokens, component classes like `.card` / `.btn` / `.tag` / `.board` / `.md`), served from the existing `/static` mount and linked in `base.html`. Tailwind CDN is still loaded (mapped to the same CSS variables) for utility classes, alongside Alpine.js. Three Google Fonts carry meaning: **Archivo** (UI/headings), **JetBrains Mono** (data/IDs/markings), **Spectral** (finished-product prose — the editor preview and published report body). The iceberg mark is inline SVG in `templates/_glyph.html` (included by `base.html` and `login.html`); active nav is derived from `request.url.path`. This is a skin over the same routes/forms/Alpine bindings — no behavioural coupling.

### Role help & onboarding (`src/iceberg/help_content.py`)
A single **`/help`** portal page (`web/routes.py` `help_view`, no API) orients users. It is **contextual + browsable**: it defaults to the viewer's own role guide but switches to any role via `?role=` (the `_coerce_role` helper falls back to the viewer's role on an unknown value, so `?role=BOGUS` never 500s), and carries a shared **intelligence-concepts glossary** below. All copy is **structured data** in `help_content.py` (frozen `RoleGuide` / `Concept` dataclasses, `ROLE_GUIDES` + `CONCEPTS`, `guide_for`) — one source of truth, rendered through Jinja autoescaping (no markdown/nh3 pass). Each `Concept.slug` is a stable anchor `id` in `help.html`; small `.help-hint` **contextual deep-links** on key screens (dashboard, report editor → `#icd-203`/`#estimative-language`/`#diamond-model`, tasking board / my-requirements → `#requirements`, feed → `#dissemination`, preferences → `#intel-levels`) jump straight to the relevant section. A `Help` link sits in the `base.html` nav for every role.

### Rendering
- Markdown → sanitized HTML (`src/iceberg/rendering/markdown.py`, markdown-it-py + nh3) for the live preview and portal display.
- Report → PDF: **via the Typst binary** (`src/iceberg/rendering/typst.py`). Markdown is rendered inside Typst by the `cmarker` package (fetched from the Typst registry on first use; version pinned in `src/iceberg/typst/product.typ`). If Typst is not installed, render endpoints return 503.
- **Decision:** rendering goes directly through Typst. Quarto (which can drive Typst as its PDF engine) was considered as a publishing layer but not adopted — direct Typst gives equivalent output with fewer moving parts. Revisit only if richer templating is needed.

### Diamond Model diagrams (`src/iceberg/services/diamond.py`)
A Diamond Model is rendered to a **self-contained SVG** by hand-built string templating (no extra deps; same spirit as the inline `_glyph.html` mark). One generator (`render_diamond_svg`) feeds three surfaces from a single source: (a) **web report view / live preview** — the `[[diamond:ID]]` token is swapped for an inline `<figure>` **after** nh3 sanitisation (nh3 would otherwise strip server `<svg>`); the figure is safe because every vertex value is XML-escaped at generation; (b) **Typst PDF** — `render_report` writes each referenced diagram to `diamond-{id}.svg` in the per-render temp `--root` and rewrites the token to a markdown image, so it embeds **inline at the token's position**. `product.typ` overrides cmarker's `image` in its `scope` so the path resolves against the template dir (the temp root), not the cmarker package, and constrains the diagram to 92% column width. Tokens are **notebook-scoped**: a token resolves only to a diamond in the report's own notebook; unknown / cross-notebook ids degrade to an "unavailable" notice. The diagram **labels its two meta-axes** (`↕ SOCIO-POLITICAL` adversary↔victim, `↔ TECHNICAL` capability↔infrastructure — the Diamond Model's defining idea), renders confidence as an **ordinal pip-meter** (not a 4th red/amber/green stamp competing with TLP/status), and exposes accessibility via `<title>`/`<desc>` (so the title lives in escaped text content, never a raw attribute).

### Figure embedding (`src/iceberg/services/figures.py`, `services/product_html.py`)
An analyst uploads an image to a notebook's **Figures** collection (PNG/JPEG/GIF — the browser-`data:`-URI ∩ Typst-`image()` intersection; WebP/SVG are excluded) and embeds it inline by writing a `[[figure:ID]]` token in a report's body — mirroring the Diamond Model token, including notebook-scoping and the "unavailable" degrade for unknown/cross-notebook ids. Two surfaces from one upload: (a) **web view / live preview** — the token is swapped for an inline `<figure>` whose `<img>` carries a base64 `data:` URI of the file's bytes, injected **after** nh3 sanitisation (nh3 would otherwise strip a `data:` URI); the caption/alt is HTML-escaped, so it's safe; (b) **Typst PDF** — `render_report` copies each referenced image into the per-render `--root` as `figure-{id}{ext}` and rewrites the token to a markdown image, reusing `product.typ`'s generic `image` scope override (no template change). The shared web/preview pipeline lives in **`services/product_html.py`** (the assembler moved out of `diamond.py` so one `_to_html` pass substitutes *both* diamond and figure tokens). Figures are a **token-only** embed — there is no `ReportAttachment`-style link table and embedded figures do **not** appear in the PDF appendix. The figures collection is writer-only, but the published report inlines the bytes as a `data:` URI, so report viewers (incl. read-only stakeholders) see embedded figures without hitting the writer-only `/raw` endpoint (which serves the notebook/editor management thumbnails only).

### Database & migrations (`src/iceberg/db.py`, `src/iceberg/migrations/`)
Schema is owned by **Alembic** (SQLModel models are the source autogenerate compares
against). `init_db()` runs `alembic upgrade head` on boot when `ICEBERG_AUTO_MIGRATE=true`
(the default — convenient for dev), then seeds the taxonomy and rebuilds the FTS index (both
idempotent); set the flag false in prod and run migrations in the deploy step. Migrations run
against the app's own engine via a **shared connection** (`db.run_migrations` injects it into
`env.py`) so an in-memory `sqlite://` DB works too. The **FTS5 objects are not in SQLModel
metadata** — the baseline migration owns a frozen copy of the `report_fts` virtual table +
sync triggers (kept identical to `services/search.py`'s runtime DDL), and `env.py` excludes
`report_fts*` from autogenerate so it never tries to drop them. `env.py` uses
`render_as_batch=True` for SQLite ALTER support. Tests keep the fast in-memory `create_all`
path; `tests/test_migrations.py` runs a real `upgrade head` on a temp DB and asserts (via
`alembic check`) that models and migrations don't drift. New change → `alembic revision
--autogenerate -m "..."`; pre-existing DBs built by the old `create_all` → `alembic stamp head`
once.

### Project structure
```
src/iceberg/
  main.py          # app factory: mounts API + portal, session + CSRF mw, auth redirect
  config.py        # pydantic-settings (ICEBERG_ env prefix)
  db.py            # SQLite engine/session, FK pragma, Alembic upgrade on boot
  migrations/      # Alembic env + versioned migrations (baseline owns the FTS DDL)
  models.py        # SQLModel models + enums
  schemas.py       # API request bodies
  seed.py          # CLI: import the tag taxonomy (python -m iceberg.seed)
  help_content.py  # structured /help copy: per-role guides + concepts glossary
  templating.py    # shared Jinja2Templates instance
  auth/            # OIDC (Entra) + dev login, JWT, role dependencies, same-origin CSRF mw
  api/             # JSON routers: notebooks, reports, requirements, feed, account, preview, tags, search
  web/             # portal routes (Jinja2)
  services/        # users, notebooks, lifecycle, citations/rendering (reports), requirements, attachments, figures, diamond, product_html (shared report-HTML assembler), dissemination, email, tags, search
  rendering/       # markdown->HTML, report->PDF
  data/            # starter_tags.json (importable starter taxonomy)
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
Tests use an in-memory SQLite database (overriding the `get_session` dependency) and the dev-login bypass. Coverage includes auth gating, notebook/source/note/report CRUD, source reliability grading (auto/LLM fallback/manual/clear/regrade/fetch safety/UI warnings), citation scoping, the lifecycle state machine (including illegal transitions and published-report immutability), markdown preview sanitization, the full portal authoring flow (exercises the templates), requirement roles/ownership/tasking/traceability, attachment upload/download/delete with MIME + size validation and writer-only access (incl. report citation scoping + publish immutability), dissemination matching (intel level + TLP gate) with feed delivery / email outbox / read tracking / preferences, tag taxonomy curation (admin-only) with retire semantics and post-publish-editable classification, tag aliases (create/update round-trip with case-insensitive dedupe + label-as-alias drop, seed import/refresh), entity attribution profile (2b — `suspected_attribution`/`motivations`/`first_seen`/`last_seen` API round-trip + clear, `normalise_motivations` dedupe/invalid-drop, seed import/refresh, the named-threat entity-profile page vs the plain-kind search drill-down), the starter-taxonomy catalog validity + idempotent import (incl. the `--update`/`--list` CLI paths), FTS search relevance / facets / trigger-driven index sync (incl. the judgement-scaffolding columns) / alias-aware resolution (an alias query returns the canonical entity's reports with no body mention; body matches still rank first) / the stakeholder published-only access filter, Diamond Model CRUD + notebook scoping / SVG generation + XML-escaping / inline-token rendering (post-nh3 injection, unknown + cross-notebook degrade) / live-preview endpoints / writer-only access / the Typst token rewrite, figure upload/delete with image-only MIME + size validation and writer-only access / data-URI inline-token rendering (post-nh3, unknown + cross-notebook degrade, mixed diamond+figure body) / the writer-only raw serve / notebook cascade file cleanup / the Typst figure rewrite + file copy, the ICD 203 judgement scaffolding (Key Judgements / Assumptions / Intelligence Gaps editable + publish-immutable, portal persistence + view rendering, the shared product-HTML assembler behind the live-preview endpoint with fragment sanitisation, `_build_data` carriage, and the brief-format Key-Judgements-only render), the optional `analytic_confidence` marking (API set/round-trip/null-clear + publish-immutability; portal select persistence + `""→None` coercion + masthead chip shown only when set; `_build_data` carriage; probability-yardstick editor panel + `estimative-language` deep-link), the per-role `/help` page (renders for each role leading with the viewer's guide, cross-role `?role=` browsing, the bad-param fallback, glossary anchor coverage, the nav link + contextual deep-links, the anonymous redirect, and the content-module invariants), and a Typst render smoke test (skips when the binary is absent).

## Scope / roadmap
- **Milestone 1 (done)** — the authoring loop end-to-end: notebooks → sources/notes → report authoring with live preview → review/publish → Typst PDFs.
- **Milestone 2 (done)** — stakeholder requirement intake + analyst tasking board + report/notebook↔requirement traceability.
- **Milestone 3 (done)** — dissemination: on publish, match stakeholders by preferred intel level + TLP into a personalized feed, with email notifications (pluggable backend, sent via background task).
- **Milestone 4 (done)** — knowledge layer: an admin-curated tag taxonomy (actor/campaign/malware/ATT&CK technique/sector/topic) classifying reports, plus full-text + faceted search over reports (SQLite FTS5, bm25), access-scoped so stakeholders only match published reports.

- **Analytic models (done)** — Diamond Model of Intrusion Analysis assessments captured per notebook and embedded inline into reports (web + PDF) via `[[diamond:ID]]` tokens. See *Diamond Model diagrams* above.
- **Figures (done)** — images collected per notebook and embedded inline into reports (web + PDF) via `[[figure:ID]]` tokens, mirroring the Diamond Model token. See *Figure embedding* above.

- **Analytic tradecraft — source grading + structured judgements + estimative language (done, ICD 203 §1a/§1b/§1c)** — sources carry Admiralty/NATO reliability/credibility grading with auto suggestions, manual override, and PDF/source-list chips; reports carry optional Key Judgements / Key Assumptions / Intelligence Gaps fields, rendered as discrete sections (Key Judgements as the BLUF) in the web view and PDF (brief PDF formats are Key-Judgements-only); and an optional `analytic_confidence` marking with a probability-yardstick authoring aid (§1b). See the CTI roadmap [`CTI_ROADMAP.md`](CTI_ROADMAP.md).

- **Knowledge graph — tag aliases + attribution profile (done, roadmap 2a + 2b)** — the named-threat tag kinds (ACTOR/MALWARE/CAMPAIGN) carry a structured `aliases` list (search is alias-aware, so APT28 / Fancy Bear / Sofacy resolve to one entity) **plus** structured attribution (`suspected_attribution`, `motivations`, `first_seen`/`last_seen`), and `/tags/{id}` is now a proper **entity profile page** for those kinds. See *Tagging & search* and the **Tag** domain-model bullet above. **Next on that roadmap:** 2c (entity relationships — a STIX-shaped `EntityRelationship` table); or §1c stretch — ACH as a second analytic model.

The original vision (collect → author → disseminate, aligned to stakeholder requirements) is now implemented end-to-end, with tagging + search and analytic models layered on top. Deployment is still dev-oriented; SQLite throughout, with **Alembic migrations** now managing the schema (see *Database & migrations*). Production hardening to consider: a built Tailwind stylesheet (vs CDN), a real SMTP backend + durable job queue for notifications, and verifying the Entra OIDC flow against a live tenant. **Tagging/search fast-follows:** notebook tagging, stakeholder tag *subscriptions* for dissemination (match on shared tags, not just intel_level), tag merge/rename tooling, and a full ATT&CK import. **Diamond Model fast-follows:** the classic meta-features (phase/methodology/direction/result/timestamp), alternative layouts, and per-vertex source linking.

## Maintenance
- Maintain an up to date CLAUDE.md
- Maintain an up to date README.md
