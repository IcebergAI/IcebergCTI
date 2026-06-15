# Plan — 1b: Estimative language (analytic confidence + probability yardstick)

## Context

Per the CTI roadmap ([CTI_ROADMAP.md](../../CTI_ROADMAP.md) §1b), ICD 203 keeps two
expressions deliberately separate: **analytic confidence** in a judgement, and the
**likelihood** of the assessed event. Iceberg expressed neither at the report level
(Diamond confidence was the only confidence anywhere). This change adds an optional
report-level confidence marking and a standardised probability yardstick authoring
aid, mirroring the shipped 1c judgement-scaffolding pipeline end-to-end.

**Locked decisions (from clarification):**
- **Confidence is optional/nullable** (`AnalyticConfidence | None`, default `None` =
  "not stated"); the masthead marking only appears once an analyst sets LOW/MODERATE/
  HIGH. Mirrors the optional KJ/KA/Gaps fields; avoids implying a confidence on drafts.
- **Hedging lint deferred.** We ship the static probability-yardstick reference panel,
  not the body-text linter (a clean follow-up).
- **Likelihood is prose, not a field** — expressed in the body via the yardstick lexicon;
  only *confidence* is structured.

## Changes (in dependency order)

1. **Model** — [models.py](../../src/iceberg/models.py): new `AnalyticConfidence`
   StrEnum (LOW/MODERATE/HIGH) + `Report.analytic_confidence: AnalyticConfidence | None`.
2. **Migration** — real Alembic revision (`97f13b59967e`) adding the nullable enum column
   (batch ALTER for SQLite). `tests/test_migrations.py`'s `alembic check` proves no drift.
3. **Schema** — [schemas.py](../../src/iceberg/schemas.py): `ReportUpdate.analytic_confidence`.
   The JSON API's `setattr`/`exclude_unset` loop carries it (omit = untouched, `null` = clear).
4. **Portal save** — [web/routes.py](../../src/iceberg/web/routes.py) `report_save`: a
   `Form()` param accepted as `str` and coerced `"" → None` (the "— Not stated —" option).
5. **Editor** — [report_edit.html](../../src/iceberg/templates/report_edit.html): a
   confidence `<select>` (with "— Not stated —"), the masthead chip in the badge row, and
   a collapsible probability-yardstick panel; both carry a `/help#estimative-language` link.
6. **Web view** — [_macros.html](../../src/iceberg/templates/_macros.html) `confidence_badge`
   (renders nothing when `None`) added to the [report_view.html](../../src/iceberg/templates/report_view.html)
   marker strip. Calm neutral chip (`.conf` in `iceberg.css`) — only the dot carries colour,
   so it never competes with the TLP/status stamps.
7. **PDF** — [rendering/typst.py](../../src/iceberg/rendering/typst.py) `_build_data` carries
   the value (`""` when unset); [product.typ](../../src/iceberg/typst/product.typ) appends a
   neutral confidence `stamp(...)` to the masthead markings stack, only when set, all formats.
8. **Yardstick content** — single-sourced in [help_content.py](../../src/iceberg/help_content.py)
   as `PROBABILITY_YARDSTICK` (`ProbabilityBand` dataclass) + an `estimative-language` glossary
   `Concept` (its points built from the yardstick); referenced from the analyst/reviewer guides.

## Tests
- [test_api.py](../../tests/test_api.py): set/round-trip/null-clear `analytic_confidence`;
  publish-immutability 409 (routes through `ensure_editable`).
- [test_portal.py](../../tests/test_portal.py): editor select persists; `"" → None`; masthead
  chip shown only when set; yardstick panel + deep-link present in the editor.
- [test_render.py](../../tests/test_render.py): `_build_data` carries the value (and `""` when
  unset); the Typst smoke test (skips without binary) compiles a report with confidence set.
- Existing help content-invariants still hold (the new concept slug resolves).

## Out of scope / follow-ups
- **Hedging lint** (vague-estimative-word flagging in `body_md`).
- Surfacing confidence in the FTS facets / search & feed lists.
- ACH (the §1c stretch) — a separate Diamond-style analytic model.
