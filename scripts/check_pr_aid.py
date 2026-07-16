#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path

AID_PATTERN = re.compile(r"(?<![A-Za-z0-9])AID-([0-9]+)(?![A-Za-z0-9])", re.IGNORECASE)


def extract_aid_identifiers(*values: str) -> list[str]:
    identifiers: list[str] = []
    seen: set[str] = set()
    for value in values:
        for match in AID_PATTERN.finditer(value):
            identifier = f"AID-{int(match.group(1))}"
            if identifier not in seen:
                seen.add(identifier)
                identifiers.append(identifier)
    return identifiers


def validate_pull_request_metadata(head_ref: str, title: str, body: str) -> str:
    identifiers = extract_aid_identifiers(head_ref, title, body)
    if len(identifiers) != 1:
        rendered = ", ".join(identifiers) if identifiers else "none"
        raise ValueError(
            "pull request branch, title, and body must reference exactly one unique "
            f"AID-N identifier; found: {rendered}"
        )
    return identifiers[0]


def values_from_event(path: Path) -> tuple[str, str, str] | None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    head = pull_request.get("head") or {}
    return (
        str(head.get("ref") or ""),
        str(pull_request.get("title") or ""),
        str(pull_request.get("body") or ""),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Require exactly one Multica AID in PR metadata")
    parser.add_argument("--event-file", type=Path)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--body", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    values: tuple[str, str, str] | None
    if args.event_file:
        values = values_from_event(args.event_file)
        if values is None:
            print("AID metadata check skipped: event is not a pull request")
            return 0
    else:
        values = (args.head_ref, args.title, args.body)
    try:
        identifier = validate_pull_request_metadata(*values)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    print(f"Validated Multica trace identifier: {identifier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
