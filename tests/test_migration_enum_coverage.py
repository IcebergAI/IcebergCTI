"""Every enum value the application can persist must exist in the migrated schema.

PostgreSQL stores these columns as **native enum types**; SQLite stores them as
plain VARCHAR with no constraint. So adding a member to a Python enum and
forgetting the ``ALTER TYPE`` passes the entire suite and then fails on the
first write in production — which is exactly what happened to
``JobKind.EDITORIAL_MENTION`` (#306), and to the IOC type enum before it (#320).

These tests read the migration chain and compare the values it declares against
the enums the code can produce, so the drift is caught on SQLite.
"""

import pathlib
import re

import pytest

from iceberg.models import IOCType, JobKind, JobStatus

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "src/iceberg/migrations/versions"

# sa.Enum("A", "B", name="thing") / postgresql.ENUM("A", "B", name="thing")
_ENUM_CALL = re.compile(
    r"(?:sa\.Enum|postgresql\.ENUM)\(\s*(?P<values>[^)]*?)name=[\"'](?P<name>\w+)[\"']",
    re.S,
)
# ALTER TYPE thing ADD VALUE [IF NOT EXISTS] 'C'
_ADD_VALUE = re.compile(
    r"ALTER\s+TYPE\s+(?P<name>\w+)\s+ADD\s+VALUE\s+(?:IF\s+NOT\s+EXISTS\s+)?'(?P<value>[^']+)'",
    re.I,
)
_LITERAL = re.compile(r"[\"']([A-Z][A-Z0-9_]*)[\"']")


def _declared(enum_name: str) -> set[str]:
    """Every value the migration chain puts into ``enum_name``."""

    values: set[str] = set()
    for path in MIGRATIONS.glob("*.py"):
        source = path.read_text()
        for match in _ENUM_CALL.finditer(source):
            if match.group("name") == enum_name:
                values.update(_LITERAL.findall(match.group("values")))
        for match in _ADD_VALUE.finditer(source):
            if match.group("name").lower() == enum_name:
                values.add(match.group("value"))
    return values


@pytest.mark.parametrize(
    ("enum", "enum_name"),
    [(JobKind, "jobkind"), (JobStatus, "jobstatus"), (IOCType, "ioctype")],
)
def test_every_enum_member_is_representable_after_migration(enum, enum_name):
    declared = _declared(enum_name)
    assert declared, f"no migration declares the {enum_name} enum"
    # SQLAlchemy persists an enum by member *name*, which is what the migration
    # declares; IOCType is the case where name and value differ ("IP_SRC" vs
    # "ip-src"), so comparing values would compare the wrong side.
    missing = {member.name for member in enum} - declared
    assert not missing, (
        f"{sorted(missing)} can be written by the application but "
        f"{enum_name} has no migration adding it — PostgreSQL will reject it"
    )


def test_the_scan_would_notice_a_missing_value():
    """Guard the guard: a value absent from the chain must not read as present."""

    assert "EDITORIAL_MENTION" in _declared("jobkind")
    assert "NOT_A_REAL_JOB_KIND" not in _declared("jobkind")
