# Iceberg — CTI Tradecraft Advisory Roadmap

> **Type:** Strategic advisory roadmap (assessment + prioritised recommendations). This document is a forward-looking deliverable; the field/model proposals below are illustrative of direction, not an implementation spec.

## Context

Iceberg is a **finished-intelligence production** platform (collect in notebooks → author narrative reports → review → disseminate), deliberately *not* an IOC/indicator store ("Iceberg doesn't deal directly in IOCs"). This roadmap answers: as a senior threat-intel specialist, how would I evolve it to better align with contemporary CTI best practice?

The guiding principle is **respect that identity**. The highest-leverage moves are *not* to bolt on an indicator repository (that is OpenCTI/MISP territory and would dilute the product). They are to raise the **analytic rigour** of the finished product to professional standards (ICD 203 / structured analytic techniques) and to give the **knowledge layer** the entity/relationship structure modern CTI consumers expect — while keeping reports narrative-first.

This roadmap **prioritises two themes** — *Analytic Tradecraft (ICD 203)* and *Knowledge Graph / Actor Profiles* — and summarises the remaining opportunities as a secondary backlog.

## Current-state assessment (grounded in the code)

**Strengths — keep and build on:**
- **TLP 2.0 done correctly** — `CLEAR` + `AMBER_STRICT`, restrictiveness-ranked for dissemination gating ([models.py:36-78](src/iceberg/models.py#L36-L78)). Ahead of many platforms.
- **Diamond Model** as a first-class per-notebook analytic artefact: four core features + ordinal confidence pip-meter + labelled meta-axes ([models.py:330-353](src/iceberg/models.py#L330-L353), [services/diamond.py](src/iceberg/services/diamond.py)).
- **Source reliability grading** — Admiralty/NATO-style reliability + credibility chips on notebook sources, report citations, report source lists, and the PDF appendix, with opt-in LLM grading, safe URL fetch, heuristic fallback, and manual override ([services/source_grading.py](src/iceberg/services/source_grading.py)).
- **ATT&CK identifiers** carried on a controlled, admin-curated taxonomy — T-codes (technique), G-codes (actor), S-codes (malware) in `Tag.external_id` ([models.py:468-481](src/iceberg/models.py#L468-L481), [data/starter_tags.json](src/iceberg/data/starter_tags.json)).
- Clean lifecycle, requirement→product traceability, FTS5 + faceted search, multi-format Typst PDFs.

**Gaps that matter for a *finished-intelligence* platform:**
- ~~**No estimative language.**~~ **Addressed (§1b):** reports carry an optional analytic-confidence marking, and a standardised probability yardstick is shipped as an authoring aid (likelihood expressed in prose). The optional hedging lint is deferred.
- **Limited structured analytic techniques.** Key Judgements / Key Assumptions / Intelligence Gaps are implemented, but deeper structured analytic techniques like Analysis of Competing Hypotheses (ACH) are still missing.
- **Flat knowledge layer.** Actor/malware/campaign are flat `Tag` rows; aliases are concatenated into a description string ("Fancy Bear / Sofacy — Russia (GRU)"). No aliasing, no relationships, no entity profiles — the classic APT28/Fancy Bear/Sofacy naming problem is unmodelled.
- **No machine-readable interop** (STIX/TAXII/Navigator) and **email/feed-only dissemination** — noted as secondary backlog below.
- **Need-to-know gap:** stakeholders consume published products, but the published report library is not yet compartmented by named sharing, tags, teams, or entitlement groups.

---

## Priority 1 — Analytic tradecraft rigour (ICD 203 / Analytic Standards)

*The single biggest uplift for a finished-intelligence platform. Three sub-initiatives, each independently shippable.*

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
- **Stretch:** add **ACH** as a second analytic model alongside Diamond, reusing the exact `services/diamond.py` pattern (per-notebook model → server-rendered artefact → `[[ach:ID]]` inline token). This squarely advances "structured analytic techniques".
- **Impact:** Medium–High / **Effort:** Medium (fields) + Medium-Large (ACH stretch).

---

## Priority 2 — Knowledge graph / actor profiles

*Move the actor/malware/campaign vocabulary from flat labels to a real entity layer. Sequenced so value lands early.*

### 2a. Aliases (ship first — fixes the naming problem, cheap)
- Add a structured **`aliases`** list to ACTOR/MALWARE/CAMPAIGN tags (or a small linked `TagAlias` table) so APT28 / Fancy Bear / Sofacy / STRONTIUM resolve to one entity.
- Make **search alias-aware** ([services/search.py](src/iceberg/services/search.py)) so any alias matches the canonical entity — immediate recall win.
- **Impact:** High / **Effort:** Low–Medium.

### 2b. Entity attribution profile
- Enrich actor entities with structured attribution: suspected **country/sponsor**, **motivation** (espionage/financial/hacktivist), **first/last seen**. Turn `/tags/{id}` into a proper **actor profile page** rather than just a tag drill-down.
- **Impact:** Medium / **Effort:** Medium.

### 2c. Entity relationships (the graph)
- Introduce an **`EntityRelationship`** table — `(source, target, relation_type)` using STIX-aligned verbs (**uses, attributed-to, variant-of, targets, related-to**) so you can express *actor → uses → malware*, *campaign → attributed-to → actor*, *actor → targets → sector*.
- Surface as relationship chips on the profile page and a simple related-entities list/mini-graph. Deliberately STIX-shaped so it doubles as the foundation for STIX export (backlog item B).
- **Impact:** Medium-High / **Effort:** Medium-Large.

> **Design note:** prefer *extending* the existing `Tag` model incrementally (aliases → attribution → relationships) over a disruptive new `Entity` model. TECHNIQUE/SECTOR/TOPIC stay as plain tags; only the "named-threat" kinds graduate to entities. This keeps the report editor's tag-selection UX intact.

---

## Secondary backlog (not prioritised now — listed for completeness)

| # | Opportunity | Why it matters | Impact / Effort |
|---|---|---|---|
| A | **ATT&CK Navigator layer export + matrix view** | You already store T-codes; emit a Navigator `.json` per report/actor + a heatmap. Pairs Kill Chain with ATT&CK (the Diamond paper's own pairing). | High / **Low** — strong quick win |
| B | **STIX 2.1 / TAXII interop** | Export the finished product as a STIX `report` SDO referencing actor/malware/attack-pattern SDOs + relationships **derived from the Priority-2 entity layer**. Makes products downstream-consumable without becoming an IOC store. | High / Medium-Large (rides on Priority 2) |
| C | **Dissemination channels + subscription matching** | Slack/Teams/webhook alongside email; match stakeholders on shared **tags/entities**, not just intel_level (already a CLAUDE.md fast-follow). | Medium / Medium |
| D | **Intelligence-cycle feedback loop** | Stakeholder feedback / RFI-satisfaction signal on disseminated products — closes the cycle and measures effectiveness. Builds on existing requirement traceability. | Medium / Medium |
| E | **PAP (Permissible Actions Protocol)** beside TLP | Governs *actions* on intel, complementing TLP's *sharing* marking. | Low / Low |
| F | **Need-to-know / compartmentation fix** | Stakeholders can currently read all raw notebook material — a handling-discipline gap. | High (security) / Medium |

---

## Suggested sequencing

1. **Quick wins first:** A (Navigator export) → 2a (aliases). All Low-effort, High-impact, low blast-radius.
2. **Core rigour:** 1a, 1b and 1c are shipped (1b's hedging lint and 1c's ACH stretch remain as follow-ups).
3. **Knowledge layer:** 2b (profiles) → 2c (relationships) → then B (STIX export) becomes a natural payoff.
4. **Process/governance:** D (feedback loop), C (channels), F (need-to-know) as capacity allows.

## Validation approach (when these are built)

When each item is implemented, validate in the style of the existing suite (in-memory SQLite + dev-login, per CLAUDE.md *Testing*):
- **1a/1b/1c:** model/enum round-trip + schema validation tests; assert the grading chip / confidence marking / KJ sections appear in both the web view and a Typst **render smoke test** (skips when the binary is absent, like the current one).
- **2a:** regression test that an alias query returns the canonical entity's reports (extends `services/search.py` coverage).
- **2c:** relationship CRUD + scoping tests mirroring the Diamond Model test pattern.
- Update **CLAUDE.md** (domain model + roadmap) and **README.md** alongside any implementation, per the repo's maintenance rule.

