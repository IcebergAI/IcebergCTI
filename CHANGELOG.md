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
- **Shared object storage and multi-replica operation** (#315): local/S3-compatible blob adapters
  for attachments, figures and retained PDFs; staged checksum-verified writes and bounded verified
  reads; durable deletion tombstones; resumable migration and cursor-backed reconciliation;
  authenticated storage metrics; backup/restore and active-check commands; two-replica rolling
  Kubernetes workloads; and real S3-compatible concurrent/failover/tamper/recovery CI acceptance.
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
- The production Kubernetes topology now uses private S3-compatible storage instead of an app
  `ReadWriteOnce` data PVC. `pvc.yaml` remains only as a read-only source for controlled migration
  of legacy local objects.

### Security

- **The `uv` binary in the release image is digest-pinned** (#281). It was the one mutable image
  reference in a build whose base images are digest-pinned — and it resolves and installs *every*
  dependency into the release image, so a re-pushed `0.11.23` tag on GHCR would have been a direct
  supply-chain compromise. Dependabot's docker ecosystem tracks `FROM` lines, not `COPY --from`, so
  it would not have flagged a bad pin either.
- **The release workflow drops privilege on the dry-run path** (#254). It was a single job holding
  `contents/packages/id-token/attestations: write`; a `workflow_dispatch` build-only run used none
  of them but executed with all of them. Split into a `build` job (`contents: read`) and a `publish`
  job gated on it and on an actual tag push — so validation now completes in a low-privilege context
  *before* any credential-bearing job starts.
- **The workflow security auditor is no longer pinned to a yanked release.** CI pinned
  `zizmor@1.27.0`, which PyPI yanked under advisory GHSA-f42p-wjw5-97qh; moved to 1.29.0 (verified
  to report the same clean result).

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

- **Review follow-ups to the final backlog batch (#298).** The release workflow's new concurrency
  group was keyed on `github.ref` — but every tag is its own ref, so two close `v*` tags landed in
  different groups and the `:latest`-ordering race it claimed to prevent was not serialised at all;
  the group is now a constant. And the feed's "outside your current tag interests" honesty fix
  required the report to carry tags, though a subscribed reader's filter also excludes *untagged*
  reports — that case no longer falls through to a positive preference chip.

- **Design-debt cleanup across the portal** (#282). The report editor's `<noscript>` save fallback
  was non-functional *and* destructive — the hidden `version` had only an Alpine binding (posting
  `""` → 422) and the textareas rendered empty, so a "successful" no-JS submit would have blanked the
  report; all now carry real server-rendered values. The stakeholder feed eager-loads what it renders
  instead of O(N) lazy loads, and no longer computes every delivery context twice per GET. The
  dashboard's "needs you now" queue filters and orders in SQL rather than loading every draft in the
  deployment to keep five. A changed *tag* filter now gets the same honest "outside your current
  interests" treatment a changed *level* preference already had. Notebook phase tabs expose
  `aria-current`, and the dashboard's "+ Start a notebook" tile opens the form it points at.
- **Design-debt cleanup across config, AI and OIDC** (#282). The `/admin/config` validation panel
  hand-duplicated `config._guard_production`, so a future guard would silently not appear there —
  both now call one `production_guard_errors`. The AI console's embeddings toggle is labelled and
  disabled (nothing reads it; related reports always use the local fallback), its provider list is
  derived from the backend vocabulary rather than hand-maintained, its timeout is clamped
  server-side, and its settings-change audit now records `base_url`/`model` old→new — the field that
  can redirect the API key previously looked like a no-op save. `probe()` goes through a new
  `AIBackend.check()` instead of reaching into the private `_complete`. The Entra-specific
  `preferred_username` email fallback moved out of the generic OIDC base, where it was wrong for
  Okta. The multi-provider migration's downgrade now refuses up front when duplicate emails exist
  (the designed multi-provider state) rather than failing part-way through. A least-privilege role
  fallback logs a warning again. The RSS hub tile says when the poller is off instead of showing a
  green "N ACTIVE" while nothing is fetched. `/admin/oidc` gained role-gate tests — it had none.

- **The release tag guard rejects unsupported PEP 440 forms** (#253). Dev/post releases, epochs,
  local versions and zero-padded pre-releases passed through the PEP 440 → SemVer normaliser
  untouched; a genuine mismatch still failed closed, but a hand-crafted tag matching the
  un-normalised string could have minted a malformed OCI tag. They now fail with an actionable error.
- **The docker build + Trivy gate runs pre-merge on PRs that can break it** (#282). It was exempt
  from PRs entirely, so a base-digest bump that broke the build or tripped Trivy surfaced only on the
  post-merge `main` push — leaving `main` red at exactly the commit a `v*` tag would release. It now
  also runs on PRs touching `Dockerfile`, `.dockerignore`, `uv.lock` or `pyproject.toml` (detected
  with git, not a new third-party action), and stays non-required so it can't deadlock.
- **The release workflow serialises per ref** (#282) — two `v*` tags pushed close together could
  land `:latest` out of order. Added a concurrency group without `cancel-in-progress`, since a
  half-published release is worse than a queued one.
- **Tightened the documented cosign verification identity** (#282) — the example regexp accepted a
  certificate from any workflow on any ref in the repo; it now pins `release.yml@refs/tags/v*`.

- **The body-size limit returns the documented 413 on streaming bodies** (#272). On the chunked /
  no-`Content-Length` path the mid-stream abort was caught by FastAPI's broad `except Exception`
  around body parsing and reported as `400 "There was an error parsing the body"` — misleading, since
  the body was oversized rather than malformed. The overflow is now signalled out-of-band and the
  app's own response rewritten to a 413. Covered by integration tests through the real `create_app()`
  stack, which the previous bare-Starlette tests never exercised.

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
