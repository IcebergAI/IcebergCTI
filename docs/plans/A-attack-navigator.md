# Backlog A — ATT&CK Navigator layer export + matrix view

> Roadmap ref: [CTI_ROADMAP.md](../../CTI_ROADMAP.md) Secondary backlog A. GitHub FR #28.
> **Impact / Effort:** High / Low — a quick win that rides entirely on data we already store.

## Goal
Turn the ATT&CK T-codes already carried on the controlled taxonomy
(`Tag.external_id` on `TECHNIQUE` tags) into two consumer-facing surfaces:

1. **Navigator layer export** (`.json`) — for a single **report** and for a
   named-threat **entity** (actor/malware/campaign), techniques scored by
   occurrence. Schema-conformant ATT&CK Navigator layer files.
2. **In-portal matrix / heatmap** — a global `/matrix` page aggregating technique
   coverage across all *visible* reports (grouped into ATT&CK tactic columns),
   plus a mini-heatmap embedded on each entity profile.

## Key design decision — no new model, no migration
A Navigator layer and the heatmap are **pure derivations** from existing rows:
- `TECHNIQUE` tags carry the T-code in `Tag.external_id` (e.g. `T1566`).
- A report's techniques = its `TECHNIQUE`-kind tags (via `ReportTag`).
- An entity's techniques = union over reports tagged with that entity.
- **Tactic** (for matrix columns) is already stored in `Tag.description` for
  technique tags (e.g. "Initial Access", "Execution") — see `data/starter_tags.json`.

So this is additive read-only code only. No `models.py` change, no Alembic
revision. (Caveat: `description`-as-tactic is a soft convention; we normalise it
against the known enterprise tactic list and bucket anything else under
"Uncategorised". A dedicated `tactic` column is a possible future hardening, noted
as a limitation — out of scope for the quick win.)

## Access control
Everything is scoped exactly like search so nothing leaks:
- Report layer export → `reports.ensure_visible` (stakeholder requesting an
  unpublished report's layer gets **404**, matching report view).
- Entity layer + matrix aggregation → built over `search.search_reports(session,
  user=user, ...)`, which already reapplies the published-only rule for
  stakeholders. So a stakeholder's heatmap/entity layer counts published reports
  only; analysts/reviewers/admins see everything.

All endpoints stay JWT-authenticated (no anonymous access), consistent with the
platform rule. These are read-only GETs — no CSRF surface.

## Work items

### 1. `services/attack.py` (new — the engine)
Constants + pure functions, no DB writes:
- `TACTIC_ORDER` — the 14 enterprise ATT&CK tactics in kill-chain order (column
  ordering for the matrix); `normalise_tactic(desc)` maps a tag description to one
  of them or `"Uncategorised"`.
- `LAYER_VERSIONS` / `ATTACK_DOMAIN` — pinned `{"attack": ..., "navigator": ...,
  "layer": "4.5"}`, `"enterprise-attack"` (single-sourced constant, like the
  Typst version pin).
- `technique_tags(tags)` → the `TECHNIQUE` tags with a non-empty `external_id`.
- `technique_counts(reports)` → `{external_id: (label, tactic, count)}` aggregated
  over a report list (each report contributes ≤1 per technique).
- `build_layer(*, name, description, counts)` → a schema-conformant Navigator
  layer dict: `techniques: [{techniqueID, score, comment}]`, a `gradient`
  (white→accent over `[0, maxScore]`), `versions`, `domain`, `sorting`. Score =
  occurrence count (always ≥1 for a single report).
- `report_layer(report)` and `entity_layer(tag, reports)` — thin wrappers naming
  the layer and delegating to `build_layer`.
- `coverage_matrix(reports)` → `{tactics: [{tactic, techniques: [{tag, count}]}],
  max_count}` for the heatmap template (empty-state friendly).

### 2. `api/attack.py` (new router, mounted under `/api`)
`APIRouter(prefix="/attack", tags=["attack"])`, registered in `api/__init__.py`:
- `GET /attack/reports/{report_id}/layer` → load report, `ensure_visible`, return
  `report_layer(...)` as JSON with
  `Content-Disposition: attachment; filename="navigator-report-{id}.json"`.
- `GET /attack/tags/{tag_id}/layer` → load tag (404 if missing; restrict to
  `tags.ALIASABLE_KINDS` named-threat entities → 404 otherwise), gather reports via
  `search_reports(tag_ids=[id], user=user)`, return `entity_layer(...)` with an
  attachment filename. Empty entity → a valid layer with `techniques: []`.

### 3. Web routes (`web/routes.py`)
- `GET /matrix` → aggregate `search_reports(session, user=user)` (optionally
  `?tag_id=` to scope to one entity), render `matrix.html` with
  `coverage_matrix(...)`. Handles the empty state.
- Add a **"Download Navigator layer"** link on the report view
  (`reports/{id}`) → `/api/attack/reports/{id}/layer`, shown when the report has
  technique tags.
- On `entity_profile.html`: a **mini-heatmap** (reuse the matrix partial over the
  profile's own `items`) + a **"Download Navigator layer"** link →
  `/api/attack/tags/{id}/layer`.
- Add a **Matrix** link to the `base.html` nav (writers + stakeholders; it's
  finished-product analytics).

### 4. Templates + CSS
- `templates/_attack_matrix.html` — a shared heatmap partial (tactic columns,
  cells tinted by frequency), included by both `matrix.html` and
  `entity_profile.html`.
- `templates/matrix.html` — the full page wrapping the partial + intro/empty state.
- `static/css/iceberg.css` — a few `.heat-*` cell-tint classes (oklch accent at
  graded opacity) consistent with the existing design tokens.

### 5. Tests (`tests/test_attack.py`)
Mirror the existing in-memory + dev-login style:
- Report layer is schema-valid and contains exactly the report's T-codes, score 1.
- Entity layer aggregates counts across multiple reports tagged with the same
  actor (a technique on two reports → score 2); excludes tags with no
  `external_id` and non-`TECHNIQUE` kinds.
- `coverage_matrix` groups techniques under the right tactic column; unknown
  description → "Uncategorised"; empty report set → empty matrix.
- **Access scoping:** stakeholder gets 404 on an unpublished report's layer;
  stakeholder's entity layer/matrix counts published reports only; analyst sees
  drafts.
- Entity-layer endpoint 404s for a non-named kind (e.g. a SECTOR tag) and a
  missing tag.
- Portal smoke: `/matrix` renders (populated + empty), nav link present, report
  view + entity profile expose the download link.

### 6. Docs (per the repo maintenance rule)
- **CLAUDE.md** — new "ATT&CK Navigator export & matrix" subsection under
  Rendering/knowledge layer, a `services/attack.py` entry in the project structure
  + services list, and the Testing paragraph; flip backlog A to done in the
  roadmap section.
- **README.md** — mention the Navigator export + matrix view.
- **CTI_ROADMAP.md** — mark backlog A ✅, update the sequencing note.
- Close **#28** on merge (AC: valid layer with expected T-codes ✓, heatmap with
  empty state ✓, tests + docs ✓).

## Out of scope (note in the FR / fast-follows)
- A dedicated `tactic` column on `Tag` (vs the `description` convention).
- Full ATT&CK technique import / sub-technique handling / multiple domains
  (mobile, ICS) — enterprise only, the techniques we curate.
- Per-tactic scoring tweaks, custom gradients, layer versioning UI.
