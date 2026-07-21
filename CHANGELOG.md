# Changelog

All notable changes to IcebergCTI are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Note the two spellings of the same version: `pyproject.toml` carries the **PEP 440** form
(`0.1.0b1`) and the git tag carries the **SemVer** form (`v0.1.0-beta.1`). The headings below use
the SemVer form. See [docs/RELEASING.md](docs/RELEASING.md).

## [Unreleased]

No release has been cut yet. This section captures the work merged to `main` to date; the first
tagged release will snapshot it under a dated heading.

### Added

- **Collection → authoring → dissemination**, end to end: topic **notebooks** gathering
  sources / notes / attachments / figures, **report** authoring (markdown + live preview, ICD 203
  Key Judgements / Assumptions / Gaps + analytic-confidence + probability-yardstick lint), the
  `DRAFT → IN_REVIEW → APPROVED → PUBLISHED` lifecycle with snapshot-frozen, optimistically-locked
  publication, and **Typst**-rendered PDFs (full / exec-brief / one-pager).
- **Stakeholder alignment**: requirement intake (PIR / GIR / RFI) + an analyst tasking board with
  report/notebook traceability and a PIR-coverage panel; **dissemination** to per-stakeholder feeds
  by intel level + TLP ceiling + tag subscriptions + audience groups, with email/webhook
  notifications via a durable job outbox; and a stakeholder **feedback loop** (RFI satisfaction →
  auto-advance).
- **Analytic tradecraft**: Admiralty/NATO source grading (offline heuristic), and inline **Diamond
  Model**, **ACH**, **figure**, and **ATT&CK** coverage-matrix embeds (web + PDF).
- **Knowledge layer**: an admin-curated tag taxonomy (actor / campaign / malware / ATT&CK technique
  / sector / topic) with alias-aware, faceted full-text **search** (SQLite FTS5 / Postgres tsvector),
  named-threat entity profiles + attribution, ATT&CK Navigator export + coverage matrix, and STIX
  2.1 / read-only TAXII export.
- **Light-touch IOCs → MISP**: indicators staged as notebook entities (manual or governed
  AI-extraction), cited into a report's Indicators appendix, and pushed to MISP as one event.
- **Inbound collection**: SSRF-guarded RSS/Atom ingestion into a writer-only feed reader, and
  writer-triggered TAXII / MISP pull into notebook sources.
- **Governed AI assist** (off by default): pluggable `none` / `openai-compatible` / `claude` /
  `bedrock` backends, TLP-gated egress, fail-soft, proxy-aware, provenance-stamped.
- **Security & operations**: OIDC (Entra) + dev-login auth with per-user token versioning,
  same-origin CSRF + strict CSP + security headers, token-bucket rate limiting, OWASP audit logging
  → pluggable SIEM, a global outbound-proxy option, self-hosted SRI-pinned frontend assets, health
  probes, and a production container + Kubernetes manifests on PostgreSQL.
- **Engineering**: CI with ruff / bandit / vulture / **mypy** / djlint / biome / pip-audit,
  **CodeQL** SAST, **zizmor + actionlint** workflow SAST, and a tag-driven **release workflow**
  publishing a signed, SBOM- and provenance-attested GHCR image (this section).

[Unreleased]: https://github.com/IcebergAI/IcebergCTI/commits/main
