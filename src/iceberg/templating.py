"""Shared Jinja2 templates instance with common globals/filters."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .config import get_settings
from .models import (
    AnalyticConfidence,
    IntelLevel,
    Priority,
    ProductFormat,
    ReportStatus,
    RequirementStatus,
    TagKind,
    TLP,
    source_credibility_label,
    source_grade_label,
    source_reliability_label,
    tlp_label,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Globals available in every template.
templates.env.globals["app_name"] = get_settings().app_name
templates.env.globals["tlp_label"] = tlp_label
templates.env.globals["IntelLevel"] = IntelLevel
templates.env.globals["AnalyticConfidence"] = AnalyticConfidence
templates.env.globals["TLP"] = TLP
templates.env.globals["ReportStatus"] = ReportStatus
templates.env.globals["ProductFormat"] = ProductFormat
templates.env.globals["Priority"] = Priority
templates.env.globals["RequirementStatus"] = RequirementStatus
templates.env.globals["TagKind"] = TagKind
templates.env.globals["source_grade_label"] = source_grade_label
templates.env.globals["source_reliability_label"] = source_reliability_label
templates.env.globals["source_credibility_label"] = source_credibility_label
