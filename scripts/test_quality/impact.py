from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from .collection import CollectionResult
from .models import ImpactPolicy


@dataclass(frozen=True)
class ImpactSelection:
    mode: str
    nodeids: tuple[str, ...]
    matched_rules: tuple[str, ...]
    reasons: tuple[str, ...]


def _matches(pattern: str, path: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)


def _expand_test_pattern(pattern: str, collection: CollectionResult) -> set[str]:
    return {nodeid for nodeid in collection.nodeids if _matches(pattern, nodeid if "::" in pattern else nodeid.split("::", 1)[0])}


def select_impacted_nodes(
    *,
    changed_paths: list[str],
    policy: ImpactPolicy,
    collection: CollectionResult,
    eligible_nodes: tuple[str, ...],
) -> ImpactSelection:
    eligible = set(eligible_nodes)
    normalized = sorted(set(changed_paths))
    full_reasons = [path for path in normalized if any(_matches(pattern, path) for pattern in policy.always_full_paths)]
    if not normalized:
        full_reasons.append("no changed paths were discoverable")
    matched_rules: list[str] = []
    selected: set[str] = set()
    unknown: list[str] = []
    for path in normalized:
        path_rules = [rule for rule in policy.rules if any(_matches(pattern, path) for pattern in rule.changed_paths)]
        if not path_rules and not any(_matches(pattern, path) for pattern in policy.always_full_paths):
            unknown.append(path)
        for rule in path_rules:
            if rule.id not in matched_rules:
                matched_rules.append(rule.id)
            for selector in rule.test_selectors:
                selected.update(_expand_test_pattern(selector, collection))
    if unknown:
        full_reasons.append(f"unknown changed paths: {', '.join(unknown)}")
    if full_reasons:
        return ImpactSelection(
            mode="full",
            nodeids=tuple(sorted(eligible)),
            matched_rules=tuple(matched_rules),
            reasons=tuple(full_reasons),
        )
    selected.intersection_update(eligible)
    if not selected:
        return ImpactSelection(
            mode="full",
            nodeids=tuple(sorted(eligible)),
            matched_rules=tuple(matched_rules),
            reasons=("impact rules expanded to zero eligible tests",),
        )
    return ImpactSelection(
        mode="impacted",
        nodeids=tuple(sorted(selected)),
        matched_rules=tuple(matched_rules),
        reasons=(),
    )
