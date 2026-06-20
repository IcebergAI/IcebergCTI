"""Guards for the self-hosted, SRI-protected frontend assets (FR #58).

These run offline (no network): they prove the committed vendored assets match
the recorded SRI hashes (tamper / lock-mismatch guard) and that no template has
regressed back to a third-party CDN. The full rebuild-from-pins drift check lives
in CI (re-runs scripts/vendor_assets.py and git-diffs).
"""

import base64
import hashlib
import json
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "src" / "iceberg"
_STATIC = _PKG / "static"
_LOCK = _STATIC / "assets.lock.json"
_TEMPLATES = _PKG / "templates"

_CDN_ORIGINS = (
    "cdn.tailwindcss.com",
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "unpkg.com",
)


def _lock() -> dict:
    return json.loads(_LOCK.read_text())


def _sri(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


def test_lock_exists_and_covers_the_three_assets():
    lock = _lock()
    assert set(lock) == {"alpine", "tailwind", "fonts"}
    for entry in lock.values():
        assert entry["version"] and entry["path"] and entry["integrity"]


@pytest.mark.parametrize("name", ["alpine", "tailwind", "fonts"])
def test_vendored_asset_integrity_matches_lock(name):
    entry = _lock()[name]
    path = _STATIC / entry["path"]
    assert path.exists(), f"vendored asset missing: {entry['path']}"
    assert _sri(path.read_bytes()) == entry["integrity"], (
        f"{name}: file does not match its recorded SRI hash — re-run "
        "scripts/vendor_assets.py and commit the result"
    )


def test_self_hosted_font_files_present():
    files = _lock()["fonts"].get("files", [])
    assert files, "no font files recorded in the lock"
    for fname in files:
        assert (_STATIC / "fonts" / fname).exists(), f"missing font file: {fname}"


def test_no_cdn_origins_in_templates():
    for html in _TEMPLATES.rglob("*.html"):
        text = html.read_text()
        for origin in _CDN_ORIGINS:
            assert origin not in text, f"{html.name} still references CDN origin {origin}"


def test_base_html_uses_first_party_assets_with_sri():
    base = (_TEMPLATES / "base.html").read_text()
    assert "/static/{{ assets.tailwind.path }}" in base
    assert "/static/{{ assets.alpine.path }}" in base
    assert "/static/{{ assets.fonts.path }}" in base
    assert "integrity=" in base
