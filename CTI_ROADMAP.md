# Iceberg — CTI Tradecraft Advisory Roadmap

> **Type:** Strategic advisory roadmap (assessment + prioritised recommendations). This document is a forward-looking deliverable; the field/model proposals below are illustrative of direction, not an implementation spec.

## Context

Iceberg is a **finished-intelligence production** platform (collect in notebooks → author narrative reports → review → disseminate), deliberately *not* an IOC/indicator store ("Iceberg doesn't deal directly in IOCs"). This roadmap answers: as a senior threat-intel specialist, how would I evolve it to better align with contemporary CTI best practice?

The guiding principle is **respect that identity**. The highest-leverage moves are *not* to bolt on an indicator repository (that is OpenCTI/MISP territory and would dilute the product). They are to raise the **analytic rigour** of the finished product to professional standards (ICD 203 / structured analytic techniques) and to give the **knowledge layer** the entity/relationship structure modern CTI consumers expect — while keeping reports narrative-first.

This roadmap **prioritises two themes** — *Analytic Tradecraft (ICD 203)* and *Knowledge Graph / Actor Profiles* — and summarises the remaining opportunities as a secondary backlog.

> **Status (2026-06-18).** Both prioritised themes are now **fully shipped** — Priority 1 (1a source grading, 1b estimative language, 1c judgement scaffolding **incl. the ACH stretch**) and Priority 2 (2a aliases, 2b attribution profiles, 2c entity relationships). From the secondary backlog, **A (ATT&CK Navigator export + matrix)** has also shipped. What remains is the secondary backlog **B–I** plus two scoped follow-ups, all tracked as GitHub FRs (referenced inline below). The roadmap's centre of gravity has moved from *analytic rigour / knowledge graph* to **interop (STIX), dissemination/process, need-to-know governance, program effectiveness, and inbound collection**.

## Current-state assessment (grounded in the code)

**Strengths — keep and build on:**
- **TLP 2.0 done correctly** — `CLEAR` + `AMBER_STRICT`, restrictiveness-ranked for dissemination gating ([models.py:36-78](src/iceberg/models.py#L36-L78)). Ahead of many platforms.
- **Diamond Model** as a first-class per-notebook analytic artefact: four core features + ordinal confidence pip-meter + labelled meta-axes ([models.py:330-353](src/iceberg/models.py#L330-L353), [services/diamond.py](src/iceberg/services/diamond.py)).
- **Source reliability grading** — Admiralty/NATO-style reliability + credibility chips on notebook sources, report citations, report source lists, and the PDF appendix, with opt-in LLM grading, safe URL fetch, heuristic fallback, and manual override ([services/source_grading.py](src/iceberg/services/source_grading.py)).
- **ATT&CK identifiers** carried on a controlled, admin-curated taxonomy — T-codes (technique), G-codes (actor), S-codes (malware) in `Tag.external_id` ([models.py:468-481](src/iceberg/models.py#L468-L481), [data/starter_tags.json](src/iceberg/data/starter_tags.json)).
- Clean lifecycle, requirement→product traceability, FTS5 + faceted search, multi-format Typst PDFs.

**Gaps that matter for a *finished-intelligence* platform:**
- ~~**No estimative language.**~~ **Addressed (§1b):** reports carry an optional analytic-confidence marking, and a standardised probability yardstick is shipped as an authoring aid (likelihood expressed in prose). The optional hedging lint is deferred.
- ~~**Limited structured analytic techniques.**~~ **Addressed (§1c stretch):** alongside Key Judgements / Key Assumptions / Intelligence Gaps, **Analysis of Competing Hypotheses (ACH)** now ships as a second analytic model beside the Diamond Model — a per-notebook hypotheses × evidence matrix scored by inconsistency, embedded into reports via a `[[ach:ID]]` token.
- **Flat knowledge layer (addressed — 2a + 2b + 2c).** Actor/malware/campaign `Tag` rows now carry a structured `aliases` list (search resolves any alias to the canonical entity — see §2a) **and** structured attribution (sponsor/country, motivation, first/last seen), with `/tags/{id}` promoted to a real entity profile page (§2b), **and** STIX-shaped `EntityRelationship` edges between entities surfaced as profile chips + an SVG mini-graph (§2c). The knowledge layer is now a graph; **STIX export (backlog B)** is the natural next payoff.
- **Machine-readable interop** — **ATT&CK Navigator layer export is now shipped** (backlog A: per-report + per-entity `.json` + a coverage heatmap); STIX/TAXII (backlog B) remains. **Email/feed-only dissemination** is noted as secondary backlog below.
- **Need-to-know gap:** stakeholders consume published products, but the published report library is not yet compartmented by named sharing, tags, teams, or entitlement groups.

---

## Priority 1 — Analytic tradecraft rigour (ICD 203 / Analytic Standards) — ✅ **complete**

*The single biggest uplift for a finished-intelligence platform. Three sub-initiatives, each independently shippable — all now shipped (only 1b's optional hedging lint, [#25](../../issues/25), remains deferred).*

### 1a. Source reliability grading (Admiralty / NATO System) — ✅ **implemented**
- Shipped: `Source` carries reliability (`A-F`), credibility (`1-6`), grading origin, engine, rationale, warning, and timestamp. Auto grading safely fetches public source URLs, uses configured OpenAI/Anthropic/OpenAI-compatible grading when available, falls back to `heuristic:v1`, and leaves credibility as `6` when source content cannot be assessed. Analysts can manually override, clear, and regrade. Chips render in notebook sources, report citations, report source lists, and the FULL PDF source appendix.
- Implemented on `Source` ([models.py:298-313](src/iceberg/models.py#L298-L313)):
  - **`reliability`** — A–F (A *completely reliable* … E *unreliable*, F *cannot be judged*).
  - **`credibility`** — 1–6 (1 *confirmed* … 5 *improbable*, 6 *cannot be judged*).
- Surfaced as a compact **"B2"-style chip** in the notebook source list, the report citation list, and the **PDF source appendix** (`typst/product.typ`). Existing rows remain ungraded until manually graded or regraded.
- **Impact:** High / **Effort:** shipped. New enums + source columns + schema field ([schemas.py](src/iceberg/schemas.py)) + template/PDF chip.

### 1b. Estimative language — analytic confidence *and* likelihood — ✅ **implemented** (lint deferred)
- ICD 203 requires two *separate* expressions: **analytic confidence** in the judgement, and the **likelihood/probability** of the event.
- Shipped: a Report-level optional **`analytic_confidence`** (`AnalyticConfidence` LOW/MODERATE/HIGH, nullable = "not stated") — stamped as a calm neutral marking on the report masthead beside TLP/status (web view `confidence_badge` macro + `product.typ`, all formats). Optional so analysts assert it deliberately rather than implying a confidence on every draft.
- Shipped: the **standardised probability yardstick** as an authoring aid — a controlled lexicon mapped to percentage bands (*almost no chance 01–05 · very unlikely 05–20 · unlikely 20–45 · roughly even chance 45–55 · likely 55–80 · very likely 80–95 · almost certain 95–99*), single-sourced in `help_content.py` (`PROBABILITY_YARDSTICK`) and shown as a collapsible reference panel in the editor + the `estimative-language` glossary entry. Likelihood stays prose (phrased via the yardstick), not a structured field.
- **Deferred:** the optional **lint** flagging vague hedging ("could", "might") in `body_md` on save/preview ([rendering/markdown.py](src/iceberg/rendering/markdown.py) preview path) — a clean follow-up.
- **Impact:** High / **Effort:** shipped (the deferred lint is the remaining optional part).

### 1c. Structured judgement scaffolding (KJ / KA / Gaps) — ✅ **implemented**
> Shipped: `key_judgements` / `key_assumptions` / `intelligence_gaps` markdown fields on `Report`, editable in the report editor (publish-immutable), rendered as discrete sections in the web view and PDF; EXEC_BRIEF / ONE_PAGER are Key-Judgements-only. ACH is deferred. Plan: [docs/plans/1c-judgement-scaffolding.md](docs/plans/1c-judgement-scaffolding.md).
- Promote **Key Judgements**, **Key Assumptions**, and **Intelligence Gaps** to first-class optional markdown fields on `Report` ([models.py:312-344](src/iceberg/models.py#L312-L344)), rendered as standard sections in the web view and PDF — and let the **EXEC_BRIEF / ONE_PAGER** formats render *just* the Key Judgements (this is what those formats are for).
- **Stretch — ✅ implemented:** **ACH** ships as a second analytic model alongside Diamond, reusing the exact `services/diamond.py` pattern (per-notebook `ACHModel` → server-rendered SVG matrix → `[[ach:ID]]` inline token in web view, live preview and Typst PDF). Hypotheses × evidence with a 5-point + N/A consistency scale (Heuer); the analytic payload is the per-hypothesis inconsistency score (least inconsistent = most tenable). Admin-free, writer-only, notebook-scoped; edited on an Alpine grid with a live preview. Migration `b232d8f39c4b`. See `services/ach.py`.
- **Impact:** Medium–High / **Effort:** shipped (Medium fields + Medium-Large ACH).

---

## Priority 2 — Knowledge graph / actor profiles — ✅ **complete**

*Move the actor/malware/campaign vocabulary from flat labels to a real entity layer. Sequenced so value lands early — all three sub-initiatives shipped; **STIX export (backlog B, [#29](../../issues/29))** is the natural next payoff.*

### 2a. Aliases (ship first — fixes the naming problem, cheap) — ✅ **implemented**
- Shipped: a structured **`aliases`** list (a JSON column) on ACTOR/MALWARE/CAMPAIGN tags (`tags.ALIASABLE_KINDS`) so APT28 / Fancy Bear / Sofacy / STRONTIUM resolve to one entity. Admin-curated in `/admin/tags` (comma-separated input, shown only for named-threat kinds); normalised case-insensitively with the canonical label dropped as an alias. Starter taxonomy backfilled (aliases lifted out of the description strings).
- Shipped: **search is alias-aware** ([services/search.py](src/iceberg/services/search.py)) — `tags.resolve_alias_report_ids` resolves a query against tag labels + aliases and appends the matching entity's reports after the bm25 body matches, so any alias matches the canonical entity even when the body never names it. Tag text is *not* denormalised into FTS (no `report_fts` DDL change); resolution is a query-time tag lookup. Aliases surface as "Also known as" on the `/tags/{id}` detail page.
- **Impact:** High / **Effort:** shipped (Low–Medium). New JSON column + service helpers + schema/API/admin-form threading + the search union + migration `c5c560ff65be`.

### 2b. Entity attribution profile — ✅ **implemented**
- Shipped: the named-threat tag kinds (`tags.ALIASABLE_KINDS`) carry structured attribution on `Tag` — `suspected_attribution` (free-text sponsor/country), `motivations` (a JSON list validated against a new `Motivation` enum: ESPIONAGE/FINANCIAL/HACKTIVISM/DESTRUCTIVE/INFLUENCE, multi-valued), and fuzzy free-text `first_seen`/`last_seen`. Admin-curated in `/admin/tags` (gated to named-threat kinds, like aliases) and seeded from `data/starter_tags.json` (starter ACTORs backfilled — attribution lifted out of the description gloss).
- Shipped: `/tags/{id}` now renders a dedicated **entity profile** (`templates/entity_profile.html`) for named-threat kinds — attribution panel + motivation chips + "Also known as" aliases + an ATT&CK link off `external_id` (G-/S-code) + the reports-tagged list. Other kinds keep the plain `search.html` drill-down. Migration `b3d9a4e21c7f`.
- **Impact:** Medium / **Effort:** shipped (Medium). New `Motivation` enum + four `Tag` columns + `normalise_motivations` + schema/API/admin-form threading + profile template + route split.

### 2c. Entity relationships (the graph) — ✅ **implemented**
- Shipped: an **`EntityRelationship`** table — `(source_tag, target_tag, relation_type)` using STIX-aligned verbs (**uses, attributed-to, variant-of, targets, related-to**, `models.RelationType`) so you can express *actor → uses → malware*, *campaign → attributed-to → actor*, *actor → targets → sector*. Admin-curated; *loose* scoping — source is a named-threat kind (`tags.ALIASABLE_KINDS`), target is a named-threat kind or SECTOR (`relationships.TARGETABLE_KINDS`) — no per-verb kind matrix.
- Shipped: relationships surface on the `/tags/{id}` entity profile as **inbound + outbound chips grouped by verb** plus a hand-rendered **SVG mini-graph** (`services/relationships.render_relationship_graph_svg`, same XML-escaping discipline as the Diamond Model SVG). Curated on a dedicated **`/admin/relationships`** admin page. Deliberately STIX-shaped so it doubles as the foundation for STIX export (backlog item B). Migration `8db5b15e1537`.
- **Impact:** Medium-High / **Effort:** shipped (Medium-Large). New `RelationType` enum + `EntityRelationship` table + `services/relationships.py` + `api/relationships.py` + admin page + profile chips/mini-graph.

> **Design note:** prefer *extending* the existing `Tag` model incrementally (aliases → attribution → relationships) over a disruptive new `Entity` model. TECHNIQUE/SECTOR/TOPIC stay as plain tags; only the "named-threat" kinds graduate to entities. This keeps the report editor's tag-selection UX intact.

---

## Secondary backlog (not prioritised now — listed for completeness)

| # | FR | Opportunity | Why it matters | Impact / Effort |
|---|---|---|---|---|
| A | [#28](../../issues/28) ✅ | **ATT&CK Navigator layer export + matrix view** — ✅ **implemented** | Emits a schema-conformant Navigator `.json` layer per report (techniques scored 1) and per named-threat entity (aggregated across its reports, scored by occurrence), plus a `/matrix` technique-coverage heatmap (global + per-entity) grouped by ATT&CK tactic. A **pure derivation** over existing `TECHNIQUE` tags (`Tag.external_id` for the T-code, `Tag.description` for the tactic) — no new model, no migration. Access-scoped like search (stakeholders → published only). See `services/attack.py`. *Open follow-up: an inline `[[attack]]` report embed ([#41](../../issues/41)).* | High / **Low** — shipped |
| B | [#29](../../issues/29) | **STIX 2.1 / TAXII interop** | Export the finished product as a STIX `report` SDO referencing actor/malware/attack-pattern SDOs + relationships **derived from the Priority-2 entity layer**. Makes products downstream-consumable without becoming an IOC store. **The Priority-2 graph is now in place, so this is the most natural next build.** | High / Medium-Large (rides on Priority 2) |
| C | [#30](../../issues/30) | **Dissemination channels + subscription matching** | Slack/Teams/webhook alongside email; match stakeholders on shared **tags/entities**, not just intel_level (already a CLAUDE.md fast-follow). | Medium / Medium |
| D | [#31](../../issues/31) ✅ | **Intelligence-cycle feedback loop** — ✅ **implemented** | Stakeholders leave feedback (usefulness + optional RFI-satisfaction verdict + comment) on products **disseminated to them**; a **Met** verdict from the owning stakeholder auto-advances their linked requirement to `SATISFIED` (closing the cycle). Feedback surfaces on the report view (writers) and requirement detail (analysts), and feeds new effectiveness metrics (response / satisfaction / useful rates) into the maturity dashboard. New `ProductFeedback` model (one migration). See `services/feedback.py`. | Medium / Medium — shipped |
| E | [#32](../../issues/32) | **PAP (Permissible Actions Protocol)** beside TLP | Governs *actions* on intel, complementing TLP's *sharing* marking. | Low / Low |
| F | [#33](../../issues/33) | **Need-to-know / compartmentation fix** | Stakeholders can currently read all raw notebook material — a handling-discipline gap. | High (security) / Medium |
| G | [#42](../../issues/42) | **Intelligence-requirement kinds (PIR / GIR / RFI) + PIR coverage** | Splits the single undifferentiated `Requirement` into doctrine kinds — **PIR** (decision-tied, time-bound), **GIR** (standing baseline), **RFI** (ad-hoc) — so intake is classified, PIRs outrank on the tasking board, and a PIR **collection-coverage/gap** view surfaces requirements nothing yet addresses. Additive model extension; no dissemination change. Natural groundwork for D. | Medium / Medium |
| H | [#49](../../issues/49) ✅ | **CTI program maturity & effectiveness dashboard** — ✅ **implemented** | Writer-only `/maturity` view deriving program-health indicators — requirement coverage (extends `pir_coverage` to all kinds), production metrics, dissemination reach, tradecraft-adoption share — from existing data, plus an **indicative** [CTI-CMM](https://cti-cmm.org/)-style maturity rollup (four capability dimensions scored CTI0–CTI3 by thresholds, framed as evidence for a self-assessment, not a substitute). **Pure aggregation, no schema change** (`services/maturity.py`). Leadership-facing evidence; pairs with D once feedback exists. *Inspired by [zsazsa](https://github.com/cudeso/zsazsa).* | High / Medium — shipped |
| I | [#50](../../issues/50) | **Inbound collection — external feed ingestion into notebooks** | A pluggable ingestion backend (RSS/Atom first; TAXII / MISP-pull later) landing pulled *reporting* in a writer-only triage inbox, from which an analyst promotes an item into a notebook as a `Source` (auto-grading via the existing path). Optional opt-in LLM relevance triage. Closes the collection half of the cycle — the recurring corporate ask — while staying **IOC-free** (ingests reporting, not indicators). *Inspired by [zsazsa](https://github.com/cudeso/zsazsa)'s MISP-backed collection.* | High / Large |

---

## Suggested sequencing

**Done:** ~~A (Navigator export)~~, ~~Priority 1 (1a/1b/1c incl. ACH)~~, ~~Priority 2 (2a/2b/2c)~~, ~~G (requirement kinds)~~, ~~D (feedback loop)~~, ~~H (maturity dashboard)~~ — all ✅.

**What's next, in recommended order:**
1. **B — STIX 2.1 / TAXII export ([#29](../../issues/29)).** The highest-leverage remaining build: the Priority-2 entity graph (aliases + attribution + STIX-shaped relationships) and backlog A's ATT&CK derivation are exactly the SDO/relationship substrate a STIX `report` export rides on. Turns the finished product into a downstream-consumable artefact without becoming an IOC store.
2. **Process / intelligence-cycle: ~~G → D~~ ✅ done.** G (requirement kinds PIR/GIR/RFI + PIR coverage, [#42](../../issues/42)) and D (feedback loop / RFI-satisfaction signal, [#31](../../issues/31)) are both shipped. Still to pair: C — dissemination channels + tag/entity subscription matching ([#30](../../issues/30)).
3. **Governance: F (need-to-know / compartmentation, [#33](../../issues/33))** when multi-team / entitlement handling becomes a real requirement; **E (PAP marking, [#32](../../issues/32))** is a low-effort marking addition whenever convenient.

**Loose follow-ups:** 1b's hedging lint ([#25](../../issues/25)) and the inline `[[attack]]` report embed ([#41](../../issues/41)) — both small, pick up opportunistically.

## Validation approach (when these are built)

When each item is implemented, validate in the style of the existing suite (in-memory SQLite + dev-login, per CLAUDE.md *Testing*):
- **1a/1b/1c:** model/enum round-trip + schema validation tests; assert the grading chip / confidence marking / KJ sections appear in both the web view and a Typst **render smoke test** (skips when the binary is absent, like the current one).
- **2a:** regression test that an alias query returns the canonical entity's reports (extends `services/search.py` coverage).
- **2c:** relationship CRUD + scoping tests mirroring the Diamond Model test pattern.
- **B (STIX export):** schema-conformant SDO/relationship output for a report + its tagged entities (validate against a STIX 2.1 validator or the library's own checks); access-scoped like search.
- **G (requirement kinds):** kind round-trip (API + portal), PIR-first tasking-board ordering, and the PIR coverage/gap aggregation (uncovered + overdue + empty state), with unchanged ownership/role rules.
- **H (maturity dashboard):** ✅ done — `program_maturity` aggregation (production/coverage/dissemination/tradecraft counts + rates), the CTI-CMM `_level` band thresholds, empty-DB safety, writer-only route gating (stakeholder → 403), and the template render (`tests/test_maturity.py`).
- Update **CLAUDE.md** (domain model + roadmap) and **README.md** alongside any implementation, per the repo's maintenance rule.

