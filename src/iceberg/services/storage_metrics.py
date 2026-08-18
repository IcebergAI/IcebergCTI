"""Small Prometheus registry for persistent storage.

The registry is deliberately process-local. Production runs one Uvicorn process
per pod and lets the metrics collector scrape each replica. Maintenance roles
persist their progress/results in the database because they run in separate
worker or CLI processes. Every label is collapsed onto a fixed vocabulary;
object keys, filenames, endpoints, buckets, URLs, and exception text never do.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

from sqlmodel import Session, select

from ..models import StorageMaintenanceState, utcnow

_BACKENDS = frozenset({"local", "s3"})
_KINDS = frozenset({"attachment", "figure", "render"})
_OPERATIONS = frozenset({"put", "get", "head", "delete", "list", "ready"})
_OUTCOMES = frozenset({"success", "missing", "conflict", "integrity", "error"})
_DIRECTIONS = frozenset({"read", "write"})
_TASKS = frozenset({"migration", "reconcile", "active_check", "deletion"})
_SCOPES = frozenset(
    {
        "default",
        "local",
        "s3",
        *{
            f"{backend}:{signature}"
            for backend in ("local", "s3")
            for signature in (
                "attachment",
                "figure",
                "render",
                "attachment,figure",
                "attachment,render",
                "figure,render",
            )
        },
    }
)
_RESULTS = frozenset(
    {
        "examined",
        "migrated",
        "verified",
        "skipped",
        "conflicts",
        "missing",
        "listed",
        "referenced",
        "young",
        "orphaned",
        "enqueued",
        "invalid",
        "processed",
        "succeeded",
        "cancelled",
        "retried",
        "failed",
    }
)
_INTEGRITY_REASONS = frozenset({"digest", "size", "metadata", "unknown"})

_lock = threading.Lock()
_operations: dict[tuple[str, str, str, str], int] = defaultdict(int)
_duration_count: dict[tuple[str, str, str], int] = defaultdict(int)
_duration_sum: dict[tuple[str, str, str], float] = defaultdict(float)
_bytes: dict[tuple[str, str, str], int] = defaultdict(int)
_integrity: dict[tuple[str, str, str], int] = defaultdict(int)


def _fixed(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "other"


def classify_error(exc: Exception) -> str:
    """Map a storage exception onto a fixed, non-sensitive outcome label."""
    name = type(exc).__name__
    if name == "ObjectMissing":
        return "missing"
    if name == "ObjectConflict":
        return "conflict"
    message = str(exc).lower()
    if "digest" in message or "size verification" in message or "metadata verification" in message:
        return "integrity"
    return "error"


@contextmanager
def measure(backend: str, kind: str, operation: str) -> Iterator[dict[str, int | str]]:
    """Record one bounded backend operation and allow the caller to report bytes."""
    started = time.monotonic()
    observation: dict[str, int | str] = {"bytes": 0, "direction": ""}
    outcome = "success"
    try:
        yield observation
    except Exception as exc:
        outcome = classify_error(exc)
        if outcome == "integrity":
            message = str(exc).lower()
            reason = "digest" if "digest" in message else "size" if "size" in message else "metadata"
            record_integrity_failure(backend, kind, reason)
        raise
    finally:
        backend_label = _fixed(backend, _BACKENDS)
        kind_label = _fixed(kind, _KINDS)
        operation_label = _fixed(operation, _OPERATIONS)
        outcome_label = _fixed(outcome, _OUTCOMES)
        duration = max(0.0, time.monotonic() - started)
        with _lock:
            _operations[(backend_label, kind_label, operation_label, outcome_label)] += 1
            duration_key = (backend_label, kind_label, operation_label)
            _duration_count[duration_key] += 1
            _duration_sum[duration_key] += duration
            direction = str(observation.get("direction") or "")
            count = max(0, int(observation.get("bytes") or 0))
            if count and direction in _DIRECTIONS:
                _bytes[(backend_label, kind_label, direction)] += count


def record_integrity_failure(backend: str, kind: str, reason: str) -> None:
    with _lock:
        _integrity[
            (
                _fixed(backend, _BACKENDS),
                _fixed(kind, _KINDS),
                _fixed(reason, _INTEGRITY_REASONS),
            )
        ] += 1


def _state_key(task: str, scope: str) -> str:
    return f"{_fixed(task, _TASKS)}:{_fixed(scope, _SCOPES)}"


def reconcile_scope(backend: str, kinds: tuple[str, ...]) -> str:
    """Return a finite state/metric scope for an operator-selected kind set."""
    signature = ",".join(sorted(set(kinds)))
    if signature == "attachment,figure,render":
        return _fixed(backend, _BACKENDS)
    return _fixed(f"{backend}:{signature}", _SCOPES)


def load_cursor(session: Session, task: str, scope: str) -> dict:
    row = session.get(StorageMaintenanceState, _state_key(task, scope))
    return dict(row.cursor) if row is not None else {}


def persist_maintenance(
    session: Session,
    task: str,
    scope: str,
    values: dict[str, int],
    *,
    success: bool,
    cursor: dict | None = None,
) -> None:
    """Persist one bounded run so other processes can expose its real state."""
    task_label = _fixed(task, _TASKS)
    scope_label = _fixed(scope, _SCOPES)
    key = _state_key(task_label, scope_label)
    row = session.get(StorageMaintenanceState, key)
    now = utcnow()
    if row is None:
        row = StorageMaintenanceState(key=key, task=task_label, scope=scope_label)
    row.last_result = {
        _fixed(name, _RESULTS): max(0, int(value))
        for name, value in values.items()
    }
    if cursor is not None:
        row.cursor = cursor
    row.status = "SUCCESS" if success else "FAILED"
    row.last_run_at = now
    if success:
        row.last_success_at = now
    session.add(row)
    session.commit()


def maintenance_states(session: Session) -> list[StorageMaintenanceState]:
    return list(session.exec(select(StorageMaintenanceState)).all())


def render_prometheus(
    *,
    maintenance: list[StorageMaintenanceState] | None = None,
    deletion_depth: int = 0,
    deletion_oldest_age: float = 0.0,
    deletion_failed: int = 0,
    deletion_failed_oldest_age: float = 0.0,
) -> str:
    """Render a stable Prometheus text exposition without sensitive dimensions."""
    with _lock:
        operations = dict(_operations)
        duration_count = dict(_duration_count)
        duration_sum = dict(_duration_sum)
        byte_totals = dict(_bytes)
        integrity = dict(_integrity)

    lines = [
        "# HELP iceberg_storage_operations_total Persistent storage operations.",
        "# TYPE iceberg_storage_operations_total counter",
    ]
    for operation_labels, operation_value in sorted(operations.items()):
        backend, kind, operation, outcome = operation_labels
        lines.append(
            'iceberg_storage_operations_total{backend="%s",kind="%s",operation="%s",outcome="%s"} %d'
            % (backend, kind, operation, outcome, operation_value)
        )
    lines.extend(
        [
            "# HELP iceberg_storage_operation_duration_seconds Storage operation elapsed time.",
            "# TYPE iceberg_storage_operation_duration_seconds summary",
        ]
    )
    for duration_labels, duration_value in sorted(duration_count.items()):
        backend, kind, operation = duration_labels
        label_text = 'backend="%s",kind="%s",operation="%s"' % (
            backend,
            kind,
            operation,
        )
        lines.append(
            "iceberg_storage_operation_duration_seconds_count{%s} %d"
            % (label_text, duration_value)
        )
        lines.append(
            "iceberg_storage_operation_duration_seconds_sum{%s} %.9f"
            % (label_text, duration_sum[duration_labels])
        )
    lines.extend(
        [
            "# HELP iceberg_storage_bytes_total Bytes transferred by persistent storage.",
            "# TYPE iceberg_storage_bytes_total counter",
        ]
    )
    for byte_labels, byte_value in sorted(byte_totals.items()):
        backend, kind, direction = byte_labels
        lines.append(
            'iceberg_storage_bytes_total{backend="%s",kind="%s",direction="%s"} %d'
            % (backend, kind, direction, byte_value)
        )
    lines.extend(
        [
            "# HELP iceberg_storage_integrity_failures_total Persistent object verification failures.",
            "# TYPE iceberg_storage_integrity_failures_total counter",
        ]
    )
    for integrity_labels, integrity_value in sorted(integrity.items()):
        backend, kind, reason = integrity_labels
        lines.append(
            'iceberg_storage_integrity_failures_total{backend="%s",kind="%s",reason="%s"} %d'
            % (backend, kind, reason, integrity_value)
        )
    lines.extend(
        [
            "# HELP iceberg_storage_maintenance_objects Last completed storage maintenance result.",
            "# TYPE iceberg_storage_maintenance_objects gauge",
        ]
    )
    for state in sorted(maintenance or [], key=lambda row: row.key):
        task = _fixed(state.task, _TASKS)
        scope = _fixed(state.scope, _SCOPES)
        for result, value in sorted(state.last_result.items()):
            lines.append(
                'iceberg_storage_maintenance_objects{task="%s",scope="%s",result="%s"} %d'
                % (task, scope, _fixed(str(result), _RESULTS), max(0, int(value)))
            )
    lines.extend(
        [
            "# HELP iceberg_storage_maintenance_last_success_timestamp_seconds Last successful maintenance completion.",
            "# TYPE iceberg_storage_maintenance_last_success_timestamp_seconds gauge",
        ]
    )
    for state in sorted(maintenance or [], key=lambda row: row.key):
        if state.last_success_at is not None:
            lines.append(
                'iceberg_storage_maintenance_last_success_timestamp_seconds{task="%s",scope="%s"} %.3f'
                % (
                    _fixed(state.task, _TASKS),
                    _fixed(state.scope, _SCOPES),
                    state.last_success_at.timestamp(),
                )
            )
    lines.extend(
        [
            "# HELP iceberg_storage_deletion_queue_depth Pending or running deletion tombstones.",
            "# TYPE iceberg_storage_deletion_queue_depth gauge",
            f"iceberg_storage_deletion_queue_depth {max(0, int(deletion_depth))}",
            "# HELP iceberg_storage_deletion_queue_oldest_age_seconds Age of the oldest queued deletion.",
            "# TYPE iceberg_storage_deletion_queue_oldest_age_seconds gauge",
            f"iceberg_storage_deletion_queue_oldest_age_seconds {max(0.0, float(deletion_oldest_age)):.3f}",
            "# HELP iceberg_storage_deletion_failed_total Terminal deletion tombstones requiring intervention.",
            "# TYPE iceberg_storage_deletion_failed_total gauge",
            f"iceberg_storage_deletion_failed_total {max(0, int(deletion_failed))}",
            "# HELP iceberg_storage_deletion_failed_oldest_age_seconds Age of the oldest terminal deletion failure.",
            "# TYPE iceberg_storage_deletion_failed_oldest_age_seconds gauge",
            f"iceberg_storage_deletion_failed_oldest_age_seconds {max(0.0, float(deletion_failed_oldest_age)):.3f}",
        ]
    )
    return "\n".join(lines) + "\n"


def reset_for_tests() -> None:
    """Clear process-local counters; intended for isolated registry tests."""
    with _lock:
        _operations.clear()
        _duration_count.clear()
        _duration_sum.clear()
        _bytes.clear()
        _integrity.clear()
