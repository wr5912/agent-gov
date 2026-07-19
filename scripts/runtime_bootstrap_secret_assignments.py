from __future__ import annotations

import re

SECRET_ASSIGN_RE = re.compile(
    r"(?P<bracket_open>\[\s*)?(?P<key_quote>[\"']?)"
    r"(?P<key>[A-Za-z0-9_.-]*(?:api[_-]?key|(?<![A-Za-z])token(?!s?[A-Za-z])|secret|password|credential|authorization|auth[_-]?header)[A-Za-z0-9_.-]*)"
    r"(?P=key_quote)(?P<bracket_close>\s*\])?(?P<sep>\s*[:=]\s*)"
    r"(?P<raw_value>\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\"'\s,}#]+)",
    re.IGNORECASE,
)


def secret_assignment_key(match: re.Match[str]) -> str:
    return f"{match.group('bracket_open') or ''}{match.group('key_quote')}{match.group('key')}{match.group('key_quote')}{match.group('bracket_close') or ''}"


def secret_assignment_value(match: re.Match[str]) -> str:
    raw_value = match.group("raw_value")
    return raw_value[1:-1] if raw_value[:1] in {'"', "'"} else raw_value
