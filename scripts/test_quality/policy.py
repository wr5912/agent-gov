from __future__ import annotations

import fnmatch
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .collection import CollectionResult, collect_pytest_nodes, expand_selectors
from .models import Classification, Lifecycle, QualityPolicy


@dataclass(frozen=True)
class PolicyValidation:
    collection: CollectionResult
    classifications: Mapping[str, Classification]
    errors: tuple[str, ...]


def load_quality_policy(path: Path) -> QualityPolicy:
    try:
        return QualityPolicy.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid test quality policy {path}: {exc}") from exc


class _HasId(Protocol):
    id: str


def _unique_ids(items: Sequence[_HasId], label: str) -> list[str]:
    values = [str(item.id) for item in items]
    return [f"duplicate {label} id: {value}" for value in sorted({value for value in values if values.count(value) > 1})]


def _selector_matches(selector: str, nodeid: str) -> bool:
    path = nodeid.split("::", 1)[0]
    target = nodeid if "::" in selector else path
    return fnmatch.fnmatchcase(target, selector)


def classify_nodes(policy: QualityPolicy, collection: CollectionResult) -> tuple[Mapping[str, Classification], list[str]]:
    errors: list[str] = []
    classifications: dict[str, Classification] = {}
    matched_rules: dict[str, int] = {rule.id: 0 for rule in policy.portfolio.rules}
    for nodeid in collection.nodeids:
        matches = []
        for rule in policy.portfolio.rules:
            included = any(_selector_matches(selector, nodeid) for selector in rule.selectors)
            excluded = any(_selector_matches(selector, nodeid) for selector in rule.exclude_selectors)
            if included and not excluded:
                matches.append(rule)
        if len(matches) != 1:
            ids = ", ".join(rule.id for rule in matches) or "none"
            errors.append(f"pytest leaf must have exactly one portfolio classification: {nodeid} (matched: {ids})")
            continue
        rule = matches[0]
        matched_rules[rule.id] += 1
        classifications[nodeid] = rule.classification
    for rule_id, count in matched_rules.items():
        if count == 0:
            errors.append(f"portfolio rule expands to zero pytest leaves: {rule_id}")
    return classifications, errors


def main_flow_bindings(policy: QualityPolicy) -> tuple[list[str], list[str]]:
    pytest_selectors: list[str] = []
    ui_scripts: list[str] = []
    for flow in policy.main_flows:
        for scenario in flow.scenarios:
            for selector in scenario.pytest:
                if selector not in pytest_selectors:
                    pytest_selectors.append(selector)
            for script in scenario.ui_scripts:
                if script not in ui_scripts:
                    ui_scripts.append(script)
    return pytest_selectors, ui_scripts


def _frontend_scripts(repo_root: Path) -> set[str]:
    package_path = repo_root / "frontend/package.json"
    if not package_path.is_file():
        return set()
    data = json.loads(package_path.read_text(encoding="utf-8"))
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    return {key for key, value in scripts.items() if isinstance(key, str) and isinstance(value, str)}


def _main_flow_errors(policy: QualityPolicy, collected: CollectionResult, repo_root: Path) -> list[str]:
    errors: list[str] = []
    frontend_scripts = _frontend_scripts(repo_root)
    for flow in policy.main_flows:
        scenario_ids = [scenario.id for scenario in flow.scenarios]
        for duplicate in sorted({item for item in scenario_ids if scenario_ids.count(item) > 1}):
            errors.append(f"duplicate scenario id in {flow.id}: {duplicate}")
        for scenario in flow.scenarios:
            context = f"{flow.id}.{scenario.id}"
            if not scenario.pytest and not scenario.ui_scripts:
                errors.append(f"main flow scenario {context} has no pytest or ui_scripts binding")
            if len(scenario.pytest) != len(set(scenario.pytest)):
                errors.append(f"main flow scenario {context} contains duplicate pytest selectors")
            for selector in scenario.pytest:
                if not expand_selectors([selector], collected.nodeids):
                    errors.append(f"pytest selector expands to zero leaf nodeids: {context}: {selector}")
            for script in scenario.ui_scripts:
                if script not in frontend_scripts:
                    errors.append(f"frontend script {script} referenced by {context} is not defined")
    return errors


def _lifecycle_errors(
    policy: QualityPolicy,
    collected: CollectionResult,
    classifications: Mapping[str, Classification],
    owner_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    quarantine_matches: dict[str, int] = {}
    for item in policy.quarantines:
        matches = expand_selectors([item.selector], collected.nodeids)
        if item.owner not in owner_ids:
            errors.append(f"quarantine has unknown owner: {item.selector}: {item.owner}")
        if item.expires_at <= item.quarantined_at:
            errors.append(f"quarantine expiry must be after quarantine date: {item.selector}")
        if (item.expires_at - item.quarantined_at).days > policy.budgets.max_quarantine_days:
            errors.append(f"quarantine exceeds {policy.budgets.max_quarantine_days}-day budget: {item.selector}")
        if item.expires_at < date.today():
            errors.append(f"quarantine expired: {item.selector} on {item.expires_at.isoformat()}")
        if not matches:
            errors.append(f"quarantine selector expands to zero leaf nodeids: {item.selector}")
        for nodeid in matches:
            quarantine_matches[nodeid] = quarantine_matches.get(nodeid, 0) + 1
    delete_matches: dict[str, int] = {}
    for item in policy.delete_candidates:
        matches = expand_selectors([item.selector], collected.nodeids)
        if item.owner not in owner_ids:
            errors.append(f"delete candidate has unknown owner: {item.selector}: {item.owner}")
        if not matches:
            errors.append(f"delete candidate selector expands to zero leaf nodeids: {item.selector}")
        for nodeid in matches:
            delete_matches[nodeid] = delete_matches.get(nodeid, 0) + 1
    blocking_lanes = {lane.id for lane in policy.lanes if lane.enforcement == "blocking"}
    scheduled_lanes = {lane.id for lane in policy.lanes if lane.enforcement == "scheduled"}
    for nodeid, classification in classifications.items():
        quarantined = quarantine_matches.get(nodeid, 0)
        delete_candidate = delete_matches.get(nodeid, 0)
        if quarantined > 1:
            errors.append(f"pytest leaf belongs to multiple quarantine records: {nodeid}")
        if delete_candidate > 1:
            errors.append(f"pytest leaf belongs to multiple delete-candidate records: {nodeid}")
        if (classification.lifecycle == Lifecycle.QUARANTINE) != bool(quarantined):
            errors.append(f"QUARANTINE lifecycle and registry disagree: {nodeid}")
        if (classification.lifecycle == Lifecycle.DELETE_CANDIDATE) != bool(delete_candidate):
            errors.append(f"DELETE-CANDIDATE lifecycle and registry disagree: {nodeid}")
        if quarantined and blocking_lanes.intersection(classification.lanes):
            errors.append(f"quarantined pytest leaf remains in a blocking lane: {nodeid}")
        if quarantined and not scheduled_lanes.intersection(classification.lanes):
            errors.append(f"quarantined pytest leaf is missing a scheduled lane: {nodeid}")
    return errors


def validate_quality_policy(
    policy: QualityPolicy,
    *,
    repo_root: Path,
    collection: CollectionResult | None = None,
) -> PolicyValidation:
    collected = collection or collect_pytest_nodes(repo_root=repo_root)
    errors: list[str] = []
    errors.extend(_unique_ids(policy.owners, "owner"))
    errors.extend(_unique_ids(policy.capabilities, "capability"))
    errors.extend(_unique_ids(policy.lanes, "lane"))
    errors.extend(_unique_ids(policy.portfolio.rules, "portfolio rule"))
    errors.extend(_unique_ids(policy.main_flows, "main flow"))
    owner_ids = {owner.id for owner in policy.owners}
    capability_ids = {capability.id for capability in policy.capabilities}
    lane_ids = {lane.id for lane in policy.lanes}
    blocking_lane_ids = {lane.id for lane in policy.lanes if lane.enforcement == "blocking"}
    classifications, classification_errors = classify_nodes(policy, collected)
    errors.extend(classification_errors)
    for nodeid, classification in classifications.items():
        if classification.owner not in owner_ids:
            errors.append(f"unknown owner {classification.owner} for {nodeid}")
        for capability in classification.capabilities:
            if capability not in capability_ids:
                errors.append(f"unknown capability {capability} for {nodeid}")
        for lane in classification.lanes:
            if lane not in lane_ids:
                errors.append(f"unknown lane {lane} for {nodeid}")
    for capability in policy.capabilities:
        if capability.risk not in {"critical", "high"}:
            continue
        protected = any(
            capability.id in classification.capabilities and blocking_lane_ids.intersection(classification.lanes) for classification in classifications.values()
        )
        if not protected:
            errors.append(f"{capability.risk} capability has no blocking pytest protection: {capability.id}")
    errors.extend(_main_flow_errors(policy, collected, repo_root))
    errors.extend(_lifecycle_errors(policy, collected, classifications, owner_ids))
    for gap in policy.gaps:
        if gap.owner not in owner_ids:
            errors.append(f"gap has unknown owner: {gap.id}: {gap.owner}")
        if gap.capability not in capability_ids:
            errors.append(f"gap has unknown capability: {gap.id}: {gap.capability}")
        if gap.target_lane not in lane_ids:
            errors.append(f"gap has unknown target lane: {gap.id}: {gap.target_lane}")
    if policy.impact.unknown_change_lane not in lane_ids:
        errors.append(f"impact policy has unknown fail-closed lane: {policy.impact.unknown_change_lane}")
    if policy.mutation.lane not in lane_ids:
        errors.append(f"mutation policy has unknown lane: {policy.mutation.lane}")
    return PolicyValidation(collection=collected, classifications=classifications, errors=tuple(sorted(set(errors))))


def selected_lane_nodes(validation: PolicyValidation, lane: str) -> tuple[str, ...]:
    return tuple(sorted(nodeid for nodeid, classification in validation.classifications.items() if lane in classification.lanes))
