FROM python:3.14-slim AS runtime

ARG TYPST_VERSION=0.15.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ICEBERG_ENVIRONMENT=prod \
    ICEBERG_AUTO_MIGRATE=false \
    ICEBERG_DATABASE_URL=sqlite:////data/iceberg.db \
    ICEBERG_ATTACHMENTS_DIR=/data/attachments \
    ICEBERG_FIGURES_DIR=/data/figures \
    ICEBERG_RENDER_OUTPUT_DIR=/data/rendered

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY frontend ./frontend
COPY scripts ./scripts
RUN pip install --no-cache-dir .

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) typst_arch="x86_64-unknown-linux-musl" ;; \
      arm64) typst_arch="aarch64-unknown-linux-musl" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-${typst_arch}.tar.xz" -o /tmp/typst.tar.xz; \
    tar -xJf /tmp/typst.tar.xz -C /tmp; \
    mv "/tmp/typst-${typst_arch}/typst" /usr/local/bin/typst; \
    rm -rf /tmp/typst*

RUN useradd --system --create-home --uid 10001 iceberg \
    && mkdir -p /data/attachments /data/figures /data/rendered \
    && chown -R iceberg:iceberg /data /app

USER iceberg
EXPOSE 8000
CMD ["uvicorn", "iceberg.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
