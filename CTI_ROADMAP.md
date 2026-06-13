# Iceberg — CTI Tradecraft Advisory Roadmap

> **Type:** Strategic advisory roadmap (assessment + prioritised recommendations). This document is a forward-looking deliverable; the field/model proposals below are illustrative of direction, not an implementation spec.

## Context

Iceberg is a **finished-intelligence production** platform (collect in notebooks → author narrative reports → review → disseminate), deliberately *not* an IOC/indicator store ("Iceberg doesn't deal directly in IOCs"). This roadmap answers: as a senior threat-intel specialist, how would I evolve it to better align with contemporary CTI best practice?

The guiding principle is **respect that identity**. The highest-leverage moves are *not* to bolt on an indicator repository (that is OpenCTI/MISP territory and would dilute the product). They are to raise the **analytic rigour** of the finished product to professional standards (ICD 203 / structured analytic techniques) and to give the **knowledge layer** the entity/relationship structure modern CTI consumers expect — while keeping reports narrative-first.

This roadmap **prioritises two themes** — *Analytic Tradecraft (ICD 203)* and *Knowledge Graph / Actor Profiles* — and summarises the remaining opportunities as a secondary backlog.

## Current-state assessment (grounded in the code)

**Strengths — keep and build on:**
- **TLP 2.0 done correctly** — `CLEAR` + `AMBER_STRICT`, restrictiveness-ranked for dissemination gating ([models.py:36-78](src/iceberg/models.py#L36-L78)). Ahead of many platforms.
- **Diamond Model** as a first-class per-notebook analytic artefact: four core features + ordinal confidence pip-meter + labelled meta-axes ([models.py:260-284](src/iceberg/models.py#L260-L284), [services/diamond.py](src/iceberg/services/diamond.py)).
- **ATT&CK identifiers** carried on a controlled, admin-curated taxonomy — T-codes (technique), G-codes (actor), S-codes (malware) in `Tag.external_id` ([models.py:392-410](src/iceberg/models.py#L392-L410), [data/starter_tags.json](src/iceberg/data/starter_tags.json)).
- Clean lifecycle, requirement→product traceability, FTS5 + faceted search, multi-format Typst PDFs.

**Gaps that matter for a *finished-intelligence* platform:**
- **No source reliability grading.** `Source` is title/reference/summary/captured_at only ([models.py:236-246](src/iceberg/models.py#L236-L246)) — no Admiralty/NATO grading, the bedrock of CTI source evaluation.
- **No estimative language.** No Report-level analytic confidence, no standardised probability/likelihood lexicon. ICD 203 keeps *confidence* and *likelihood* distinct; Iceberg expresses neither (Diamond confidence is the only confidence anywhere).
- **No structured judgement scaffolding.** Key Judgements / Key Assumptions / Intelligence Gaps live (if at all) as freeform prose, not first-class, not renderable into briefs.
- **Flat knowledge layer.** Actor/malware/campaign are flat `Tag` rows; aliases are concatenated into a description string ("Fancy Bear / Sofacy — Russia (GRU)"). No aliasing, no relationships, no entity profiles — the classic APT28/Fancy Bear/Sofacy naming problem is unmodelled.
- **No machine-readable interop** (STIX/TAXII/Navigator) and **email/feed-only dissemination** — noted as secondary backlog below.
- **Need-to-know gap (already logged):** stakeholders can read all raw notebook material — a compartmentation issue relevant to CTI handling discipline.

---

## Priority 1 — Analytic tradecraft rigour (ICD 203 / Analytic Standards)

*The single biggest uplift for a finished-intelligence platform. Three sub-initiatives, each independently shippable.*

### 1a. Source reliability grading (Admiralty / NATO System)
- Add to `Source` ([models.py:236-246](src/iceberg/models.py#L236-L246)) two graded fields:
  - **`reliability`** — A–F (A *completely reliable* … E *unreliable*, F *cannot be judged*).
  - **`credibility`** — 1–6 (1 *confirmed* … 5 *improbable*, 6 *cannot be judged*).
- Surface as a compact **"B2"-style chip** in the notebook source list, the report citation list, and the **PDF source appendix** (`typst/product.typ`). Default to F6 ("cannot be judged") so existing rows remain valid.
- **Impact:** High / **Effort:** Low. New enums + two columns + schema field ([schemas.py](src/iceberg/schemas.py)) + template/PDF chip. Highest impact-to-effort item in the whole roadmap.

### 1b. Estimative language — analytic confidence *and* likelihood
- ICD 203 requires two *separate* expressions: **analytic confidence** in the judgement, and the **likelihood/probability** of the event.
- Add a Report-level **`analytic_confidence`** (LOW/MODERATE/HIGH) — stamp it on the report masthead beside TLP/status (web view + `product.typ`), so every product carries a confidence marking.
- Ship the **standardised probability yardstick** as an authoring aid: a controlled lexicon mapped to percentage bands (*almost no chance 01–05 · very unlikely 05–20 · unlikely 20–45 · roughly even chance 45–55 · likely 55–80 · very likely 80–95 · almost certain 95–99*), shown as a reference panel in the editor, with an optional **lint** that flags vague hedging ("could", "might") in `body_md` on save/preview ([rendering/markdown.py](src/iceberg/rendering/markdown.py) preview path).
- **Impact:** High / **Effort:** Low–Medium. The confidence field is small; the lexicon panel + optional lint is the larger (but optional) part.

### 1c. Structured judgement scaffolding (KJ / KA / Gaps)
- Promote **Key Judgements**, **Key Assumptions**, and **Intelligence Gaps** to first-class optional markdown fields on `Report` ([models.py:312-344](src/iceberg/models.py#L312-L344)), rendered as standard sections in the web view and PDF — and let the **EXEC_BRIEF / ONE_PAGER** formats render *just* the Key Judgements (this is what those formats are for).
- **Stretch:** add **Analysis of Competing Hypotheses (ACH)** as a second analytic model alongside Diamond, reusing the exact `services/diamond.py` pattern (per-notebook model → server-rendered artefact → `[[ach:ID]]` inline token). This squarely advances "structured analytic techniques".
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

1. **Quick wins first:** 1a (Admiralty grading) → A (Navigator export) → 2a (aliases). All Low-effort, High-impact, low blast-radius.
2. **Core rigour:** 1b (estimative language) → 1c (KJ/KA/Gaps).
3. **Knowledge layer:** 2b (profiles) → 2c (relationships) → then B (STIX export) becomes a natural payoff.
4. **Process/governance:** D (feedback loop), C (channels), F (need-to-know) as capacity allows.

## Validation approach (when these are built)

When each item is implemented, validate in the style of the existing suite (in-memory SQLite + dev-login, per CLAUDE.md *Testing*):
- **1a/1b/1c:** model/enum round-trip + schema validation tests; assert the grading chip / confidence marking / KJ sections appear in both the web view and a Typst **render smoke test** (skips when the binary is absent, like the current one).
- **2a:** regression test that an alias query returns the canonical entity's reports (extends `services/search.py` coverage).
- **2c:** relationship CRUD + scoping tests mirroring the Diamond Model test pattern.
- Update **CLAUDE.md** (domain model + roadmap) and **README.md** alongside any implementation, per the repo's maintenance rule.
