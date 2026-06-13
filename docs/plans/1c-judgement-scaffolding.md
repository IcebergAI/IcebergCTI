# Plan — 1c: Structured judgement scaffolding (Key Judgements / Assumptions / Intelligence Gaps)

## Context

Per the CTI roadmap ([CTI_ROADMAP.md](../../CTI_ROADMAP.md) §1c), finished intelligence under ICD 203 leads with **Key Judgements** and explicitly surfaces **Key Assumptions** and **Intelligence Gaps**. Today these live, if at all, as freeform prose inside `body_md` — not first-class, not separable into briefs. This change promotes them to first-class optional markdown fields on `Report`, renders them in the web view and PDF, and makes the EXEC_BRIEF / ONE_PAGER formats *Key-Judgements-only* products (their actual purpose).

**Locked decisions (from clarification):**
- **Briefs:** `EXEC_BRIEF` and `ONE_PAGER` render **only Key Judgements** + markings/masthead — the narrative body is omitted. `FULL` renders body + all three sections.
- **ACH excluded** — scoped to the three judgement fields; ACH (a Diamond-style per-notebook model) is a separate follow-up.
- Fields are **optional markdown** (default `""`), edited in the report editor, and **immutable after publish** (reuse `ensure_editable`, exactly like `body_md`).
- Diamonds stay **body-only**; the three fields render through `render_markdown` (nh3-sanitised), no `[[diamond:ID]]` resolution.

## Changes (in dependency order)

### 1. Data model — [models.py](../../src/iceberg/models.py) `Report` (after `body_md`, ~L318)
Add three columns:
```python
key_judgements: str = ""
key_assumptions: str = ""
intelligence_gaps: str = ""
```
> No Alembic yet (project uses `create_all`); existing dev DBs need a fresh file or a manual `ALTER TABLE ... ADD COLUMN`. The in-memory test engine builds fresh, so tests are unaffected.

### 2. API schema — [schemas.py](../../src/iceberg/schemas.py) `ReportUpdate` (L45-49)
Add the three as `str | None = None`. `update_report` already does `setattr` from `model_dump(exclude_unset=True)` ([api/reports.py:104-106](../../src/iceberg/api/reports.py#L104-L106)), so the JSON API path is complete with no handler change. `ReportCreate` is left unchanged (reports start blank).

### 3. Portal save — [web/routes.py](../../src/iceberg/web/routes.py) `report_save` (L503-522)
Add three `Annotated[str, Form()] = ""` params and assign to the report. `ensure_editable` already guards author + not-published. (These post from the same editor form as the body, so one "Save draft" persists everything.)

### 4. Portal editor — [report_edit.html](../../src/iceberg/templates/report_edit.html)
Inside the existing body `<form action="/reports/{id}">` (L100-119), add three labelled `<textarea>`s (`key_judgements`, `key_assumptions`, `intelligence_gaps`) under the body, gated on `can_edit` like the body. Mono field styling + a one-line helper ("Markdown · leads the brief / records caveats"). No live-preview wiring for these (follow-up).

### 5. Portal view — route + [report_view.html](../../src/iceberg/templates/report_view.html)
- Route `report_view` ([web/routes.py:441-460](../../src/iceberg/web/routes.py#L441-L460)): add `import` of `render_markdown` and pass `key_judgements_html` / `key_assumptions_html` / `intelligence_gaps_html` via `render_markdown(report.<field>)`.
- Template: render a **Key Judgements** section as a BLUF callout **before** the `.md` body (L32); render **Key Assumptions** and **Intelligence Gaps** **after** the body, before the Sources block (L34). Each wrapped in `{% if report.<field> %}`.

### 6. PDF data — [rendering/typst.py](../../src/iceberg/rendering/typst.py) `_build_data` (L63-93)
Add the three raw markdown strings to the returned dict (`report.<field> or ""`). No diamond rewrite for them.

### 7. PDF template — [product.typ](../../src/iceberg/typst/product.typ)
- Add a small section helper (sans heading + `title-rule` + `cmarker.render(field)`), styled like `appendix-heading`.
- **Key Judgements:** render in **all formats**, positioned right after the tag row / before the body (L308-310) — the BLUF.
- **Body** (`cmarker.render(data.body_md …)` L319-324): wrap in `if fmt == "FULL"` so briefs omit it.
- **Key Assumptions + Intelligence Gaps:** render **FULL only**, after the body and before the appendix pagebreak (L329-331).
- Edge case: a brief with empty Key Judgements → render an italic "No key judgements recorded." placeholder so the product is never blank.

### 8. Tests (mirror existing suites, per CLAUDE.md *Testing*)
- [test_api.py](../../tests/test_api.py): `PATCH /reports/{id}` sets/round-trips the three fields; confirm they're covered by the published-immutability 409 (they route through `ensure_editable`).
- [test_portal.py](../../tests/test_portal.py): `report_save` persists the fields; `report_view` emits a "Key Judgements" heading when set and omits the section when empty.
- [test_render.py](../../tests/test_render.py): unit-assert `_build_data` carries the three fields; extend the Typst smoke test (skips without the binary) to compile an `EXEC_BRIEF` and a `FULL` — asserting both return a PDF path. (Body-presence assertions on PDF bytes are brittle; the FULL-vs-brief omission is covered by a unit test on the template-data/format branch rather than PDF text extraction.)

### 9. Docs (maintenance rule)
Update [CLAUDE.md](../../CLAUDE.md) Report domain-model entry (three new fields) + the product-format note (briefs = KJ-only), mark roadmap 1c done; refresh [README.md](../../README.md) feature list. Update [CTI_ROADMAP.md](../../CTI_ROADMAP.md) §1c status.

## Verification
1. `pytest` — full suite incl. the new tests.
2. Manual (`uvicorn iceberg.main:app --reload`, dev-login as `ANALYST`):
   - Create a report, fill Key Judgements / Assumptions / Gaps, Save → reopen editor and confirm persistence.
   - View the report page: KJ as a lead callout, KA/Gaps after the body.
   - With Typst installed, render `FULL` (body + all three) and `EXEC_BRIEF` (KJ only, no body) and eyeball both PDFs.

## Follow-ups since shipped (done)
- **FTS indexing** — `key_judgements` / `key_assumptions` / `intelligence_gaps` added to the `report_fts` columns + triggers, so the core assessment is discoverable via search.
- **Live preview** — `POST /api/preview/product` + a shared `diamond.render_report_product_html` / `preview_report_product_html` assembler; the editor previews the whole finished product (all four fields) and the **read-only editor pane** now shows the scaffolding too, not just the body. The report view reuses the same assembler.

## Out of scope / follow-ups
- **ACH** analytic model (Diamond-style; separate effort).
- **Per-judgement confidence/probability** — couples to roadmap §1b (estimative language).
- **Alembic migration** — deferred project-wide; new/fresh DB or manual `ALTER` for existing dev data (now also the `report_fts` columns).
