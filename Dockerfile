# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------- #
# Builder: resolve the *locked* dependency graph (uv.lock) into a venv and fetch
# the Typst binary. Build-only tooling (uv, curl, xz) stays out of the runtime.
# ---------------------------------------------------------------------------- #
FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc AS builder

# Bumping TYPST_VERSION requires updating the per-arch typst_sha checksums below
# (the build verifies the tarball and fails closed on a mismatch).
ARG TYPST_VERSION=0.15.0
# Pinned to match CI (.github/workflows/ci.yml) so the image deps == tested graph.
# Digest-pinned like the base images above: this binary resolves and installs
# EVERY dependency in the release image, so a re-pushed or compromised `0.11.23`
# tag would be a straight supply-chain compromise (OWASP CICD-SEC-3) — and
# Dependabot's docker ecosystem tracks `FROM` lines, not `COPY --from`, so it
# would not flag a bad pin either (#281). The tag is kept alongside the digest
# for human readability; the digest is what Docker resolves.
# Multi-arch index (linux/amd64 + linux/arm64), matching the base images.
COPY --from=ghcr.io/astral-sh/uv:0.11.23@sha256:d0a0a753ab981624b49c97abc98821c1c09f4ca69d1ef5cee69c501be3d88479 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

WORKDIR /app
# Install from the committed lock (--frozen) for a reproducible graph, production
# deps only (--no-dev) plus the PostgreSQL driver (--extra postgres). The project
# is installed *editable* (templates/static/data live under src and aren't wheel
# package-data), so the runtime copies src/ to the same path below.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra postgres

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl xz-utils ca-certificates; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) typst_arch="x86_64-unknown-linux-musl"; \
             typst_sha="59b207df01be2dab9f13e80f73d04d7ff8273ffd46b3dd1b9eef5c60f3eeabea" ;; \
      arm64) typst_arch="aarch64-unknown-linux-musl"; \
             typst_sha="cdf50ffc7b8ba759ed02200632eda3d78eb8b99aacb6611f4f75684990647620" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-${typst_arch}.tar.xz" -o /tmp/typst.tar.xz; \
    echo "${typst_sha}  /tmp/typst.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/typst.tar.xz -C /tmp; \
    mv "/tmp/typst-${typst_arch}/typst" /usr/local/bin/typst; \
    rm -rf /tmp/typst* /var/lib/apt/lists/*

# ---------------------------------------------------------------------------- #
# Runtime: slim image with only the venv, the Typst binary and the source tree.
# Base pinned by digest (tag + @sha256) so the build is reproducible and the exact
# bytes are auditable; Dependabot (docker ecosystem) bumps the digest as PRs.
# ---------------------------------------------------------------------------- #
FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc AS runtime

# No ICEBERG_DATABASE_URL default: the container datastore is PostgreSQL and the
# prod app refuses to boot on SQLite (config._guard_production), so the operator
# must supply a postgresql+psycopg:// URL (compose/k8s secrets do this).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    ICEBERG_ENVIRONMENT=prod \
    ICEBERG_AUTO_MIGRATE=false \
    ICEBERG_ATTACHMENTS_DIR=/data/attachments \
    ICEBERG_FIGURES_DIR=/data/figures \
    ICEBERG_RENDER_OUTPUT_DIR=/data/rendered

# Trust forwarding headers only from loopback by default. Deployments must set
# this to their actual proxy address/CIDR; wildcard trust is rejected in prod.
ENV FORWARDED_ALLOW_IPS="127.0.0.1"

# ca-certificates for outbound TLS (OIDC, RSS, SIEM/MISP/webhook/AI, Postgres TLS).
# The base image's pip (+ ensurepip bootstrap wheel) is removed: nothing installs
# packages at runtime (the venv is built by uv in the builder stage), and pip 26.2+
# ships a CycloneDX SBOM declaring its vendored libraries (msgpack, setuptools/
# pkg_resources, ...), whose CVEs the Trivy gate would otherwise fail on even
# though pip is never executed here — same "build-only tooling stays out of the
# runtime" rule as uv/curl/xz above.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/lib/python3.14/site-packages/pip* \
              /usr/local/lib/python3.14/ensurepip \
              /usr/local/bin/pip*

WORKDIR /app
COPY --from=builder /usr/local/bin/typst /usr/local/bin/typst
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/README.md /app/

RUN useradd --system --create-home --uid 10001 iceberg \
    && mkdir -p /data/attachments /data/figures /data/rendered \
    && chown -R iceberg:iceberg /data /app

USER iceberg
EXPOSE 8000
# --proxy-headers: honour X-Forwarded-For/-Proto from the trusted proxy
# (FORWARDED_ALLOW_IPS above) so the request scheme + client IP are correct.
CMD ["uvicorn", "iceberg.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
