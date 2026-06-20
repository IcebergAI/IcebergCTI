#!/usr/bin/env python3
"""Vendor + build the portal's first-party frontend assets (Tailwind/Alpine/fonts).

The portal serves all runtime frontend dependencies from ``/static`` (first-party)
rather than third-party CDNs, with Subresource Integrity (SRI) on the vendored
assets. This script is the **reproducible regenerator**: it pins exact upstream
versions (below), downloads/builds each asset into ``src/iceberg/static/``, and
writes ``static/assets.lock.json`` (version + path + ``sha384`` integrity) which the
templates read to emit ``integrity=`` attributes.

No Node toolchain: Alpine is a single downloaded file, fonts are downloaded woff2,
and Tailwind is compiled by the **standalone Tailwind CLI binary** (same "no-npm,
standalone binary" pattern as biome).

Usage (after editing a pin to bump a version):
    python scripts/vendor_assets.py

CI re-runs this and ``git diff --exit-code``s, so the committed assets can't drift
from the pins; a pytest guard re-checks the SRI hashes against the lock.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import re
import stat
import subprocess  # nosec B404 — runs the pinned Tailwind binary we just downloaded
import sys
from pathlib import Path

import httpx

# --------------------------------------------------------------------------- #
# Pinned versions — bump here, then re-run this script and commit the result.
# --------------------------------------------------------------------------- #
TAILWIND_VERSION = "4.3.1"   # latest major; CSS-first config in frontend/input.css
ALPINE_VERSION = "3.15.12"   # latest major (Alpine has no v4 yet); exact pin

# Google Fonts request mirroring the historical base.html <link>. Only the
# latin + latin-ext subsets are kept (the portal is an English-language app).
FONTS_CSS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Archivo:wght@400;500;600;700;800&"
    "family=JetBrains+Mono:wght@400;500;600;700&"
    "family=Spectral:ital,wght@0,400;0,500;0,600;1,400&display=swap"
)
KEEP_FONT_SUBSETS = {"latin", "latin-ext"}
# A modern desktop UA so the css2 endpoint returns woff2 @font-face blocks.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "iceberg" / "static"
FRONTEND = ROOT / "frontend"
LOCK_PATH = STATIC / "assets.lock.json"
TIMEOUT = 60.0


def _sri(data: bytes) -> str:
    """Subresource-Integrity digest string: ``sha384-<base64>``."""
    digest = hashlib.sha384(data).digest()
    return "sha384-" + base64.b64encode(digest).decode("ascii")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _get(url: str, **kw) -> httpx.Response:
    resp = httpx.get(url, timeout=TIMEOUT, follow_redirects=True, **kw)
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------- #
# Alpine.js
# --------------------------------------------------------------------------- #
def vendor_alpine() -> dict:
    url = f"https://cdn.jsdelivr.net/npm/alpinejs@{ALPINE_VERSION}/dist/cdn.min.js"
    data = _get(url).content
    rel = "js/vendor/alpine.min.js"
    _write(STATIC / rel, data)
    print(f"  alpine     {ALPINE_VERSION}  ({len(data):,} bytes)")
    return {"version": ALPINE_VERSION, "path": rel, "integrity": _sri(data)}


# --------------------------------------------------------------------------- #
# Tailwind (standalone CLI build)
# --------------------------------------------------------------------------- #
def _tailwind_asset_name() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x64", "amd64": "x64",
        "aarch64": "arm64", "arm64": "arm64",
        "armv7l": "armv7",
    }.get(machine, machine)
    if sysname == "linux":
        return f"tailwindcss-linux-{arch}"
    if sysname == "darwin":
        return f"tailwindcss-macos-{arch}"
    if sysname == "windows":
        return f"tailwindcss-windows-{arch}.exe"
    raise SystemExit(f"unsupported platform for the Tailwind binary: {sysname}/{machine}")


def _tailwind_binary() -> Path:
    asset = _tailwind_asset_name()
    cache = FRONTEND / ".bin" / f"{asset}-{TAILWIND_VERSION}"
    if not cache.exists():
        url = (
            "https://github.com/tailwindlabs/tailwindcss/releases/download/"
            f"v{TAILWIND_VERSION}/{asset}"
        )
        _write(cache, _get(url).content)
        cache.chmod(cache.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return cache


def build_tailwind() -> dict:
    binary = _tailwind_binary()
    rel = "css/vendor/tailwind.css"
    out = STATIC / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    # v4 is CSS-first (no -c config file); the theme + scanned sources live in
    # frontend/input.css. Run from the repo root so its @source paths resolve.
    subprocess.run(  # nosec B603 — fixed argv, the binary we just pinned/downloaded
        [
            str(binary),
            "-i", str(FRONTEND / "input.css"),
            "-o", str(out),
            "--minify",
        ],
        cwd=ROOT,
        check=True,
    )
    data = out.read_bytes()
    print(f"  tailwind   {TAILWIND_VERSION}  ({len(data):,} bytes)")
    return {"version": TAILWIND_VERSION, "path": rel, "integrity": _sri(data)}


# --------------------------------------------------------------------------- #
# Google Fonts (self-hosted woff2 + generated @font-face)
# --------------------------------------------------------------------------- #
_FACE_RE = re.compile(r"/\*\s*(?P<subset>[\w-]+)\s*\*/\s*(?P<block>@font-face\s*\{.*?\})", re.S)
_FAMILY_RE = re.compile(r"font-family:\s*'([^']+)'")
_WEIGHT_RE = re.compile(r"font-weight:\s*(\d+)")
_STYLE_RE = re.compile(r"font-style:\s*(\w+)")
_URL_RE = re.compile(r"url\((https://fonts\.gstatic\.com/[^)]+\.woff2)\)")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def vendor_fonts() -> dict:
    css = _get(FONTS_CSS_URL, headers={"User-Agent": _UA}).text
    kept: list[str] = []
    seen: set[str] = set()
    for m in _FACE_RE.finditer(css):
        subset = m.group("subset")
        if subset not in KEEP_FONT_SUBSETS:
            continue
        block = m.group("block")
        url_m = _URL_RE.search(block)
        if not url_m:
            continue
        family = _FAMILY_RE.search(block).group(1)
        weight = (_WEIGHT_RE.search(block) or [None, "400"])[1]
        style = (_STYLE_RE.search(block) or [None, "normal"])[1]
        fname = f"{_slug(family)}-{weight}-{style}-{subset}.woff2"
        if fname not in seen:
            _write(STATIC / "fonts" / fname, _get(url_m.group(1)).content)
            seen.add(fname)
        kept.append(
            f"/* {family} {weight} {style} {subset} */\n"
            + block.replace(url_m.group(1), f"/static/fonts/{fname}")
        )
    if not kept:
        raise SystemExit("no font faces parsed — did the css2 response shape change?")
    body = (
        "/* Generated by scripts/vendor_assets.py — self-hosted Google Fonts.\n"
        "   Do not edit by hand; re-run the script to regenerate. */\n\n"
        + "\n\n".join(kept)
        + "\n"
    )
    data = body.encode("utf-8")
    rel = "css/vendor/fonts.css"
    _write(STATIC / rel, data)
    print(f"  fonts      {len(seen)} woff2  ({len(data):,} bytes css)")
    return {"version": "google-fonts", "path": rel, "integrity": _sri(data), "files": sorted(seen)}


def main() -> int:
    print("Vendoring first-party frontend assets…")
    lock = {
        "alpine": vendor_alpine(),
        "tailwind": build_tailwind(),
        "fonts": vendor_fonts(),
    }
    LOCK_PATH.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {LOCK_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
