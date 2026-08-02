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

### Changed

- **PostgreSQL 17 → 18** across the Compose stack, Kubernetes StatefulSet, and CI service
  containers. The `postgres:18` image relocated its data volume from `/var/lib/postgresql/data`
  to `/var/lib/postgresql` (PGDATA now lives in a versioned subdir to enable future in-place
  `pg_upgrade`), so the volume mounts moved with it — a bare image bump would have stranded the
  cluster on an anonymous volume. Existing 17 volumes/PVCs require a dump/restore; procedure
  documented in the deployment docs and `deploy/k8s/README.md`.

### Security

- **OIDC role mapping no longer escalates on an uncurated group name** (#269). A claim value that
  spells a role is honoured only on an app-roles claim (`roles`) with no `role_map` configured — the
  legacy single-Entra flow. A directory `groups` claim (the Authentik/Okta default) now always
  requires an explicit `role_map` entry, so a pre-existing org group called `Admin` provisions a
  read-only `STAKEHOLDER` rather than an Iceberg administrator.
- **OIDC login and callback routes are rate-limited** (#268). The `auth-oidc` policy now matches the
  parametrised `/auth/oidc/{provider}/{login,callback}` routes as well as the legacy Entra aliases,
  closing an unauthenticated audit/SIEM-write flood on the callback endpoint.
- **The generic `openai-compatible` AI backend is base-URL pinned** (#270). Its target is pinned to
  the new `ICEBERG_AI_OPENAI_COMPATIBLE_BASE_URL` operator env value exactly as `ollama` already
  was — enforced at save *and* at call time, with an unset pin refusing the backend. Previously any
  non-empty DB value was accepted, so a config edit could ship `ICEBERG_AI_API_KEY` plus TLP-gated
  report content to an arbitrary host, or turn every assist into an authenticated request to an
  internal address.
- **An OIDC email change can no longer shadow an unbound local account** (#276). The unbound-owner
  collision guard, previously enforced only when creating a new identity, now also covers the update
  path; bound co-owners remain allowed for the designed cross-provider case.

- **`/admin/config` no longer prints credentials carried inside a URL** (#273). Inline proxy
  userinfo (`http://user:pass@proxy:3128`) is scrubbed from every value, and a bearer-equivalent
  webhook URL is reduced to its origin. Redaction is now deny-by-default: a URL/DSN-shaped field
  renders in full only once it is acknowledged as plaintext, and a guard test fails on any that is
  in none of the classification sets — on the DB settings rows as well as `Settings`.

### Fixed

- **Retention pruners delete in committed batches** (#280). The audit and feed-item pruners did a
  single unbatched `DELETE ... WHERE cutoff` in one transaction against the fastest-growing tables;
  a first run over a year-long window could match millions of rows, hold locks for the whole
  statement, and on failure roll back entirely — making no progress, so the next run faced the same
  oversized delete and retention never advanced. They now share a portable key-paged batch loop
  (`services/retention.delete_in_batches`) and return the rows actually deleted rather than a
  pre-delete `SELECT count(*)`.
- **Retention CronJobs can no longer wedge their own schedule** (#279). With `concurrencyPolicy:
  Forbid`, one stuck job suppressed every later run — silently stopping the retention it exists to
  provide. Both now carry `activeDeadlineSeconds`, a `backoffLimit` and resource requests;
  `iceberg-prune-audit` drops the `iceberg-data` mount it never needed (that PVC is ReadWriteOnce and
  attached to the app pod, so a cron pod on another node hung on volume attach), and
  `iceberg-prune-renders`, which does need it, gains pod affinity onto the app.

- **OIDC outbound HTTP honours the global proxy** (#277). Discovery, JWKS and the token exchange
  went direct, so SSO simply timed out in the egress-restricted deployment the proxy feature exists
  for. The OAuth registry cache is versioned on the proxy row too, so a `/admin/proxy` change takes
  effect without an SSO edit.
- **A bad AI settings row disables the backend instead of 500ing every endpoint** (#275).
  `validate_selection` now covers `max_tlp` and `timeout` — the two fields `resolve` overlays past
  pydantic's validators — so a row written outside the admin form fails closed as the resolver
  promises, rather than raising outside the fail-soft path.
- **`/admin/config` capability tiles report resolved state, not stored flags** (#274). An enabled
  MISP push with no URL or no env API key, and an SSO provider missing its client secret or its
  locator (tenant id / domain / base URL), now read amber instead of green — the `/admin` hub was
  already correct, so the page an operator opens *to debug* was the one disagreeing.
- **A lifecycle transition can no longer race the autosave debounce** (#278). Clicking "Publish &
  disseminate" within ~1.2 s of typing dropped the pending save and froze the pre-edit text into the
  immutable snapshot, unrecoverably. Transitions now flush the autosave first and refuse to move the
  report forward if that flush fails.
- **The `ruff` gate pins its rule set explicitly** (`[tool.ruff.lint] select`). It previously
  inherited ruff's implicit default; ruff 0.16 widened that default to include
  isort/pyupgrade/bugbear and more, so a routine dependency bump reported 313 findings on
  unchanged code. The gate now means the same thing across ruff releases, and broadening it is a
  deliberate, reviewable change rather than an upgrade side effect.
- **Review follow-ups to the #268–#278 backlog batches.** The transition flush now repeats while a
  save is in flight — a single `await saveNow()` returned the in-flight promise, whose form snapshot
  predated keystrokes typed after it started, so the #278 publish race was narrowed but not closed.
  `/admin/config` no longer 500s on a malformed URL-shaped value (bad port, unbalanced IPv6
  bracket): an unparseable value that may carry a credential is hidden wholesale instead. And a save
  queued behind a 409'd save no longer fires a redundant re-post after conflict recovery.
- **Report editor: a stale-write conflict is reported and recoverable** (#271). With autosave as the
  only save path, an optimistic-lock 409 was indistinguishable from a network blip: the editor
  re-posted the same stale version forever, so a second writer's work was silently discarded. The
  conflict now stops the autosave loop and surfaces a distinct state with two explicit ways out —
  reload the saved version, or overwrite with yours.

[Unreleased]: https://github.com/IcebergAI/IcebergCTI/commits/main
