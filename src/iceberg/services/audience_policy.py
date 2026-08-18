"""Versioned dissemination policy: previewable, explainable recipient resolution.

Audience groups and TLP markings already decide *who may* receive a product.
This module adds a reviewed, versioned layer on top so the same routing decision
is applied consistently to similar products, and so a publisher can see exactly
who will receive one — and why everyone else will not — before sending it.

Two invariants shape the whole module:

* **A policy can only narrow.** Resolution starts from the base authorization
  (``dissemination.match_decisions``: TLP ceiling, intel-level preference, tag
  subscriptions, audience scope). Every rule effect removes recipients from that
  set; none can add one, so a policy edit can never widen access to a product
  or weaken its marking.
* **A rule set is immutable once approved.** Editing means creating the next
  version, so a publication can name the exact ``slug@vN`` that decided it.

The resolution is fingerprinted over both its inputs and its outcome. Publishing
with a fingerprint from a stale preview — a membership change, a new policy
version, a re-tagged report — is refused, so nobody sends to a recipient list
they never saw.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..models import (
    AudienceGroup,
    DisseminationPolicy,
    DisseminationPolicyVersion,
    IntelLevel,
    Report,
    RequirementKind,
    TLP,
    User,
    utcnow,
)
from . import dissemination as dissemination_service

# A rule either restricts delivery to a named audience, or withholds it from one.
# Both are subtractive by construction -- there is deliberately no "ADD" effect.
EFFECTS = ("LIMIT_TO", "EXCLUDE")
# Report-side selector: which products the rule speaks about ("" = all of them).
WHEN_KEYS = ("tlp", "intel_level", "tag_ids", "requirement_kinds")
# Recipient-side selector: which stakeholders the effect applies to.
AUDIENCE_KEYS = ("audience_group_ids", "organisations", "stakeholder_ids")

_MAX_RULES = 100


class PolicyError(HTTPException):
    """An actionable rule-authoring or lifecycle failure (422 by default)."""

    def __init__(self, detail: str, code: int = status.HTTP_422_UNPROCESSABLE_CONTENT):
        super().__init__(code, detail)


# --------------------------------------------------------------------------- #
# Rule validation
# --------------------------------------------------------------------------- #
def _str_list(rule_ref: str, key: str, value: object, allowed: tuple[str, ...] | None) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PolicyError(f"{rule_ref}: '{key}' must be a list of strings")
    items = [item.strip().upper() if allowed else item.strip() for item in value]
    if any(not item for item in items):
        raise PolicyError(f"{rule_ref}: '{key}' must not contain empty values")
    if allowed is not None:
        unknown = [item for item in items if item not in allowed]
        if unknown:
            raise PolicyError(
                f"{rule_ref}: '{key}' has unknown value(s) {', '.join(sorted(unknown))} "
                f"— choose from {', '.join(allowed)}"
            )
    return list(dict.fromkeys(items))


def _int_list(rule_ref: str, key: str, value: object) -> list[int]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise PolicyError(f"{rule_ref}: '{key}' must be a list of integer ids")
    return list(dict.fromkeys(value))


def validate_rules(rules: object) -> list[dict]:
    """Normalise and validate a rule list, raising an actionable 422 on any error.

    Validation is strict on purpose: an unknown key is far more likely to be a
    silently-ineffective rule than a forward-compatible one, and a rule that
    quietly does nothing is the worst outcome for a routing policy.
    """

    if not isinstance(rules, list):
        raise PolicyError("Rules must be a JSON list of rule objects")
    if len(rules) > _MAX_RULES:
        raise PolicyError(f"A policy version may hold at most {_MAX_RULES} rules")

    tlps = tuple(t.value for t in TLP)
    levels = tuple(level.value for level in IntelLevel)
    kinds = tuple(kind.value for kind in RequirementKind)

    normalised: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(rules):
        ref = f"Rule {index + 1}"
        if not isinstance(raw, dict):
            raise PolicyError(f"{ref}: each rule must be a JSON object")
        unknown = set(raw) - {"id", "description", "effect", "when", "audience"}
        if unknown:
            raise PolicyError(f"{ref}: unknown field(s) {', '.join(sorted(unknown))}")

        rule_id = str(raw.get("id", "") or f"rule-{index + 1}").strip()
        if not rule_id:
            raise PolicyError(f"{ref}: 'id' must not be blank")
        if rule_id in seen_ids:
            raise PolicyError(f"{ref}: duplicate rule id '{rule_id}'")
        seen_ids.add(rule_id)
        ref = f"Rule '{rule_id}'"

        effect = str(raw.get("effect", "")).strip().upper()
        if effect not in EFFECTS:
            raise PolicyError(f"{ref}: 'effect' must be one of {', '.join(EFFECTS)}")

        when_raw = raw.get("when") or {}
        if not isinstance(when_raw, dict):
            raise PolicyError(f"{ref}: 'when' must be a JSON object")
        unknown = set(when_raw) - set(WHEN_KEYS)
        if unknown:
            raise PolicyError(
                f"{ref}: unknown 'when' key(s) {', '.join(sorted(unknown))} "
                f"— choose from {', '.join(WHEN_KEYS)}"
            )
        when: dict = {}
        if "tlp" in when_raw:
            when["tlp"] = _str_list(ref, "tlp", when_raw["tlp"], tlps)
        if "intel_level" in when_raw:
            when["intel_level"] = _str_list(ref, "intel_level", when_raw["intel_level"], levels)
        if "requirement_kinds" in when_raw:
            when["requirement_kinds"] = _str_list(
                ref, "requirement_kinds", when_raw["requirement_kinds"], kinds
            )
        if "tag_ids" in when_raw:
            when["tag_ids"] = _int_list(ref, "tag_ids", when_raw["tag_ids"])

        audience_raw = raw.get("audience") or {}
        if not isinstance(audience_raw, dict):
            raise PolicyError(f"{ref}: 'audience' must be a JSON object")
        unknown = set(audience_raw) - set(AUDIENCE_KEYS)
        if unknown:
            raise PolicyError(
                f"{ref}: unknown 'audience' key(s) {', '.join(sorted(unknown))} "
                f"— choose from {', '.join(AUDIENCE_KEYS)}"
            )
        audience: dict = {}
        if "audience_group_ids" in audience_raw:
            audience["audience_group_ids"] = _int_list(
                ref, "audience_group_ids", audience_raw["audience_group_ids"]
            )
        if "stakeholder_ids" in audience_raw:
            audience["stakeholder_ids"] = _int_list(
                ref, "stakeholder_ids", audience_raw["stakeholder_ids"]
            )
        if "organisations" in audience_raw:
            audience["organisations"] = _str_list(
                ref, "organisations", audience_raw["organisations"], None
            )
        if not audience:
            raise PolicyError(
                f"{ref}: 'audience' must name at least one of "
                f"{', '.join(AUDIENCE_KEYS)} — an empty audience would silently "
                "withhold the product from everyone"
            )

        normalised.append(
            {
                "id": rule_id,
                "description": str(raw.get("description", "")).strip(),
                "effect": effect,
                "when": when,
                "audience": audience,
            }
        )
    return normalised


def parse_rules(raw: str) -> list[dict]:
    """Parse an admin-submitted JSON rule set (blank means "no rules")."""

    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"Rules are not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    return validate_rules(parsed)


# --------------------------------------------------------------------------- #
# Policy + version lifecycle
# --------------------------------------------------------------------------- #
def list_policies(session: Session) -> list[DisseminationPolicy]:
    return list(
        session.exec(select(DisseminationPolicy).order_by(col(DisseminationPolicy.name))).all()
    )


def get_policy_or_404(session: Session, policy_id: int) -> DisseminationPolicy:
    policy = session.get(DisseminationPolicy, policy_id)
    if policy is None:
        raise PolicyError("Dissemination policy not found", status.HTTP_404_NOT_FOUND)
    return policy


def versions(session: Session, policy: DisseminationPolicy) -> list[DisseminationPolicyVersion]:
    return list(
        session.exec(
            select(DisseminationPolicyVersion)
            .where(DisseminationPolicyVersion.policy_id == policy.id)
            .order_by(col(DisseminationPolicyVersion.version).desc())
        ).all()
    )


def create_policy(
    session: Session, *, name: str, slug: str, description: str = ""
) -> DisseminationPolicy:
    if not name.strip() or not slug:
        raise PolicyError("Policy name is required")
    if session.exec(
        select(DisseminationPolicy).where(DisseminationPolicy.slug == slug)
    ).first():
        raise PolicyError("A policy with that name already exists", status.HTTP_409_CONFLICT)
    policy = DisseminationPolicy(
        name=name.strip(), slug=slug, description=description.strip()
    )
    session.add(policy)
    session.commit()
    session.refresh(policy)
    return policy


def create_version(
    session: Session,
    policy: DisseminationPolicy,
    *,
    rules: list[dict],
    note: str = "",
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    actor: User | None = None,
) -> DisseminationPolicyVersion:
    """Draft the next version of a policy. Approved versions are never rewritten."""

    if effective_from and effective_to and effective_to <= effective_from:
        raise PolicyError("The effective end must be after the effective start")
    latest = versions(session, policy)
    if latest and latest[0].approved_at is None:
        raise PolicyError(
            f"Version {latest[0].version} is still a draft — edit or approve it "
            "before drafting another",
            status.HTTP_409_CONFLICT,
        )
    version = DisseminationPolicyVersion(
        policy_id=policy.id,
        version=(latest[0].version + 1) if latest else 1,
        rules=validate_rules(rules),
        note=note.strip(),
        effective_from=effective_from,
        effective_to=effective_to,
        created_by_id=actor.id if actor else None,
    )
    session.add(version)
    policy.updated_at = utcnow()
    session.add(policy)
    session.commit()
    session.refresh(version)
    return version


def update_draft(
    session: Session,
    version: DisseminationPolicyVersion,
    *,
    rules: list[dict],
    note: str = "",
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> DisseminationPolicyVersion:
    if version.approved_at is not None:
        raise PolicyError(
            "An approved version is immutable — draft the next version instead",
            status.HTTP_409_CONFLICT,
        )
    if effective_from and effective_to and effective_to <= effective_from:
        raise PolicyError("The effective end must be after the effective start")
    version.rules = validate_rules(rules)
    version.note = note.strip()
    version.effective_from = effective_from
    version.effective_to = effective_to
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


def approve(
    session: Session, version: DisseminationPolicyVersion, *, actor: User
) -> DisseminationPolicyVersion:
    if version.approved_at is not None:
        raise PolicyError("Version is already approved", status.HTTP_409_CONFLICT)
    version.approved_at = utcnow()
    version.approved_by_id = actor.id
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


def retire(
    session: Session, version: DisseminationPolicyVersion, *, at: datetime | None = None
) -> DisseminationPolicyVersion:
    """End an approved version's effective window without deleting the record."""

    if version.approved_at is None:
        raise PolicyError("Only an approved version can be retired", status.HTTP_409_CONFLICT)
    version.effective_to = at or utcnow()
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


def delete_draft(session: Session, version: DisseminationPolicyVersion) -> None:
    if version.approved_at is not None:
        raise PolicyError(
            "An approved version is part of the publication record and cannot be "
            "deleted — retire it instead",
            status.HTTP_409_CONFLICT,
        )
    session.delete(version)
    session.commit()


def _aware(value: datetime | None) -> datetime | None:
    """A DB round-trip drops ``tzinfo`` — read it back as the UTC it was written as."""

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def applicable_versions(
    session: Session, *, at: datetime | None = None
) -> list[DisseminationPolicyVersion]:
    """Every approved version whose effective window contains ``at``.

    Rules only ever subtract recipients, so applying several policies at once is
    order-independent: the result is the intersection of what each one allows.
    """

    moment = _aware(at) or utcnow()
    rows = session.exec(
        select(DisseminationPolicyVersion).where(
            col(DisseminationPolicyVersion.approved_at).is_not(None)
        )
    ).all()
    live = [
        version
        for version in rows
        if ((start := _aware(version.effective_from)) is None or start <= moment)
        and ((end := _aware(version.effective_to)) is None or end > moment)
    ]
    return sorted(live, key=lambda v: (v.policy.slug, v.version))


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecipientDecision:
    """One stakeholder's outcome, with the reason a human can act on."""

    user_id: int
    display_name: str
    email: str
    organisation: str
    included: bool
    reason: str
    explanation: str
    rule: str = ""


@dataclass(frozen=True)
class AudienceResolution:
    report_id: int
    resolved_at: datetime
    applied_versions: list[str]
    included: list[RecipientDecision]
    excluded: list[RecipientDecision]
    exceptions: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def recipients_ids(self) -> list[int]:
        return [decision.user_id for decision in self.included]

    @property
    def fingerprint(self) -> str:
        """Stable digest of the inputs *and* the outcome of this resolution.

        Anything that would change who receives the product — a membership edit,
        a new policy version, a re-tagged report — changes the digest, so a
        publish carrying an older one is refused rather than silently sending to
        a list the publisher never saw.
        """

        material = {
            "report_id": self.report_id,
            "versions": sorted(self.applied_versions),
            "included": sorted(self.recipients_ids),
            "excluded": sorted(
                (decision.user_id, decision.reason, decision.rule)
                for decision in self.excluded
            ),
            "exceptions": sorted(self.exceptions),
        }
        canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    def as_record(self) -> dict:
        """The immutable audience record frozen onto a publication snapshot."""

        return {
            "fingerprint": self.fingerprint,
            "resolved_at": self.resolved_at.isoformat(),
            "policy_versions": list(self.applied_versions),
            "exceptions": list(self.exceptions),
            "warnings": list(self.warnings),
            "included": [asdict(decision) for decision in self.included],
            "excluded": [asdict(decision) for decision in self.excluded],
        }


def _rule_applies_to_report(rule: dict, report: Report) -> bool:
    when = rule.get("when") or {}
    if "tlp" in when and str(TLP(report.tlp).value) not in when["tlp"]:
        return False
    if "intel_level" in when and str(report.intel_level) not in when["intel_level"]:
        return False
    if "tag_ids" in when and not (set(when["tag_ids"]) & {tag.id for tag in report.tags}):
        return False
    if "requirement_kinds" in when:
        kinds = {str(requirement.kind) for requirement in report.requirements}
        if not (set(when["requirement_kinds"]) & kinds):
            return False
    return True


def _matches_audience(rule: dict, user: User, group_ids: set[int]) -> bool:
    audience = rule.get("audience") or {}
    if user.id in set(audience.get("stakeholder_ids", [])):
        return True
    if set(audience.get("audience_group_ids", [])) & group_ids:
        return True
    organisations = {value.casefold() for value in audience.get("organisations", [])}
    return bool(organisations) and user.company_name.casefold() in organisations


def _describe(rule: dict, version: DisseminationPolicyVersion) -> tuple[str, str]:
    label = f"{version.label}:{rule['id']}"
    described = rule.get("description") or (
        "restricted to a named audience"
        if rule["effect"] == "LIMIT_TO"
        else "withheld from a named audience"
    )
    return label, described


def _decision(
    user: User, *, included: bool, reason: str, explanation: str, rule: str = ""
) -> RecipientDecision:
    return RecipientDecision(
        user_id=user.id or 0,
        display_name=user.display_name,
        email=user.email,
        organisation=user.company_name,
        included=included,
        reason=reason,
        explanation=explanation,
        rule=rule,
    )


def _known_group_ids(session: Session) -> set[int]:
    return {
        group_id
        for group_id in session.exec(select(col(AudienceGroup.id))).all()
        if group_id is not None
    }


def resolve(
    session: Session,
    report: Report,
    *,
    at: datetime | None = None,
    exceptions: list[int] | None = None,
) -> AudienceResolution:
    """Resolve the recipients of a report, explaining every inclusion/exclusion."""

    moment = _aware(at) or utcnow()
    withheld = sorted(dict.fromkeys(exceptions or []))
    live_versions = applicable_versions(session, at=moment)
    warnings: list[str] = []

    known_groups = _known_group_ids(session)
    for version in live_versions:
        for rule in version.rules:
            missing = sorted(
                set((rule.get("audience") or {}).get("audience_group_ids", [])) - known_groups
            )
            if missing:
                warnings.append(
                    f"{version.label}:{rule['id']} references audience group(s) "
                    f"{', '.join(str(item) for item in missing)} that no longer exist"
                )

    included: list[RecipientDecision] = []
    excluded: list[RecipientDecision] = []
    for base in dissemination_service.match_decisions(session, report):
        user = base.user
        if user.id is None:
            continue
        if not base.included:
            excluded.append(
                _decision(user, included=False, reason=base.reason, explanation=base.explanation)
            )
            continue
        if user.id in set(withheld):
            excluded.append(
                _decision(
                    user,
                    included=False,
                    reason="exception",
                    explanation="Withheld by an explicit publisher exception",
                )
            )
            continue

        group_ids = {group.id for group in user.audience_groups if group.id is not None}
        blocked: RecipientDecision | None = None
        for version in live_versions:
            for rule in version.rules:
                if not _rule_applies_to_report(rule, report):
                    continue
                matched = _matches_audience(rule, user, group_ids)
                if rule["effect"] == "EXCLUDE" and matched:
                    label, described = _describe(rule, version)
                    blocked = _decision(
                        user,
                        included=False,
                        reason="policy_exclude",
                        explanation=f"Policy rule withholds this product: {described}",
                        rule=label,
                    )
                elif rule["effect"] == "LIMIT_TO" and not matched:
                    label, described = _describe(rule, version)
                    blocked = _decision(
                        user,
                        included=False,
                        reason="policy_limit",
                        explanation=(
                            f"Policy rule limits this product to another audience: {described}"
                        ),
                        rule=label,
                    )
                if blocked is not None:
                    break
            if blocked is not None:
                break
        if blocked is not None:
            excluded.append(blocked)
            continue
        included.append(
            _decision(
                user, included=True, reason=base.reason, explanation=base.explanation
            )
        )

    if not included:
        warnings.append("No stakeholder currently resolves as a recipient of this product")
    if not live_versions:
        warnings.append(
            "No approved policy version applies right now — recipients come from "
            "the report's own marking and audience scope alone"
        )
    return AudienceResolution(
        report_id=report.id or 0,
        resolved_at=moment,
        applied_versions=[version.label for version in live_versions],
        included=included,
        excluded=excluded,
        exceptions=withheld,
        warnings=warnings,
    )
