"""Shared Jinja2 templates instance with common globals/filters."""

import json
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .config import get_settings
from .models import (
    AnalyticConfidence,
    IntelLevel,
    IOCType,
    Priority,
    ProductFormat,
    ReportStatus,
    RequirementKind,
    RequirementStatus,
    TagKind,
    TLP,
    ioc_type_label,
    source_credibility_label,
    source_grade_label,
    source_reliability_label,
    tlp_label,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# First-party frontend asset manifest (paths + SRI), produced by
# scripts/vendor_assets.py. base.html renders the vendored Tailwind/Alpine/fonts
# from it with integrity= attributes. Empty fallback keeps templates rendering
# even if the assets haven't been vendored yet (dev convenience).
_ASSETS_LOCK = Path(__file__).resolve().parent / "static" / "assets.lock.json"
_assets: dict = json.loads(_ASSETS_LOCK.read_text()) if _ASSETS_LOCK.exists() else {}

# Globals available in every template.
templates.env.globals["app_name"] = get_settings().app_name
templates.env.globals["assets"] = _assets
templates.env.globals["tlp_label"] = tlp_label
templates.env.globals["IntelLevel"] = IntelLevel
templates.env.globals["AnalyticConfidence"] = AnalyticConfidence
templates.env.globals["TLP"] = TLP
templates.env.globals["ReportStatus"] = ReportStatus
templates.env.globals["ProductFormat"] = ProductFormat
templates.env.globals["Priority"] = Priority
templates.env.globals["RequirementKind"] = RequirementKind
templates.env.globals["RequirementStatus"] = RequirementStatus
templates.env.globals["TagKind"] = TagKind
templates.env.globals["IOCType"] = IOCType
templates.env.globals["ioc_type_label"] = ioc_type_label
templates.env.globals["source_grade_label"] = source_grade_label
templates.env.globals["source_reliability_label"] = source_reliability_label
templates.env.globals["source_credibility_label"] = source_credibility_label
