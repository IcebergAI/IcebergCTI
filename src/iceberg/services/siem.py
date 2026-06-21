"""Pluggable SIEM emission for audit events.

Modelled on ``services/email.py``: one ``emit`` entry point dispatches a single
audit event to every **enabled** delivery method, chosen by the runtime
``AuditSettings`` row (admin-editable). Three methods are supported:

- ``stdout`` — a structured-JSON line on the ``iceberg.audit`` logger (and an
  optional file), the robust baseline a sidecar shipper (Filebeat/Fluentd/
  Vector) forwards.
- ``syslog`` — an RFC 5424 message over UDP/TCP, the JSON event in the MSG body.
- ``http`` — a JSON ``POST`` to an HTTP event collector / webhook (Splunk HEC,
  Elastic, generic), authenticated with the env-only ``ICEBERG_AUDIT_HTTP_TOKEN``.

Every method is wrapped so a failing/unreachable sink is logged locally but
**never** raises — auditing must not break the request that triggered it. Emit
runs off the response path (a background task), so the cost is hidden from the
caller.
"""

import json
import logging
import socket
from datetime import datetime, timezone

import httpx

from ..config import get_settings
from ..models import AuditSettings, ProxySettings, audit_severity_rank
from . import proxy as proxy_service

logger = logging.getLogger("iceberg.audit")

# Inspectable by tests — the stdout backend appends each emitted payload here.
OUTBOX: list[dict] = []

# A short ceiling so a slow/unreachable SIEM can't pile up background work.
_HTTP_TIMEOUT = 5.0
_SOCKET_TIMEOUT = 5.0


def emit(
    event: dict,
    settings: AuditSettings,
    proxy_settings: ProxySettings | None = None,
) -> None:
    """Dispatch one OWASP-shaped event dict to every enabled method.

    No-ops when auditing is disabled or the event is below the configured
    minimum severity. Each method is isolated: one failing sink never stops the
    others and never propagates. ``proxy_settings`` (when given) routes the HTTP
    sink through the global outbound proxy.
    """
    if not settings.enabled:
        return
    if audit_severity_rank(event.get("severity", "INFO")) < audit_severity_rank(
        settings.min_severity
    ):
        return

    methods = {m.lower() for m in settings.methods}
    if "stdout" in methods:
        _safe(_emit_stdout, event, settings)
    if "syslog" in methods:
        _safe(_emit_syslog, event, settings)
    if "http" in methods:
        _safe(_emit_http, event, settings, proxy_settings)


def _safe(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 — a failing sink must never break the caller
        logger.exception("audit SIEM emit failed via %s", fn.__name__)


def _emit_stdout(event: dict, settings: AuditSettings) -> None:
    line = json.dumps(event, separators=(",", ":"), sort_keys=True)
    OUTBOX.append(event)
    logger.info("%s", line)
    if settings.file_path:
        with open(settings.file_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _emit_syslog(event: dict, settings: AuditSettings) -> None:
    # RFC 5424: <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED MSG
    # severity numeric per RFC 5424 (we map INFO=6 informational, WARNING=4,
    # CRITICAL=2) combined with the configured facility into the PRI value.
    sev = {"INFO": 6, "WARNING": 4, "CRITICAL": 2}.get(event.get("severity", "INFO"), 6)
    pri = settings.syslog_facility * 8 + sev
    ts = datetime.now(timezone.utc).isoformat()
    host = socket.gethostname() or "-"
    body = json.dumps(event, separators=(",", ":"), sort_keys=True)
    msg = f"<{pri}>1 {ts} {host} iceberg - audit - {body}"
    data = msg.encode("utf-8")
    kind = socket.SOCK_STREAM if settings.syslog_protocol.upper() == "TCP" else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.settimeout(_SOCKET_TIMEOUT)
        sock.connect((settings.syslog_host, settings.syslog_port))
        if kind == socket.SOCK_STREAM:
            sock.sendall(data + b"\n")
        else:
            sock.send(data)


def _emit_http(
    event: dict,
    settings: AuditSettings,
    proxy_settings: ProxySettings | None = None,
) -> None:
    if not settings.http_endpoint:
        return
    token = get_settings().audit_http_token
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    proxy_kwargs = (
        proxy_service.resolve(proxy_settings, settings.http_endpoint)
        if proxy_settings is not None
        else {}
    )
    resp = httpx.post(
        settings.http_endpoint,
        content=json.dumps(event, separators=(",", ":"), sort_keys=True),
        headers=headers,
        timeout=_HTTP_TIMEOUT,
        verify=settings.http_verify_tls,
        **proxy_kwargs,
    )
    resp.raise_for_status()
