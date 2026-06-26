#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict

TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".conf",
    ".env",
    ".example",
    ".gitignore",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

FORBIDDEN_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".runtime-volume-seeds-backups",
    ".runtime-volume-seeds-staging",
    "agent-governance",
    "agent-releases",
    "agent-versions",
    "cache",
    "langfuse",
    "logs",
    "outputs",
    "sessions",
    "telemetry",
    "transcripts",
    "uploads",
}

FORBIDDEN_TOP_LEVEL_DIRS = {
    "claude-roots",
    "data",
}

FORBIDDEN_FILE_NAMES = {
    ".env",
    ".mcp.local.json",
    ".claude.json",
    "CLAUDE.local.md",
    "settings.local.json",
}

FORBIDDEN_SUFFIXES = {
    ".bak",
    ".backup",
    ".db",
    ".key",
    ".log",
    ".pem",
    ".sqlite",
    ".sqlite3",
}

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|(?<![A-Za-z])token(?!s?[A-Za-z])|secret|password|passwd|credential|authorization|auth[_-]?header|private[_-]?key|encryption[_-]?key|salt)",
    re.IGNORECASE,
)
ENDPOINT_KEY_RE = re.compile(
    r"(^|[_-])(url|uri|endpoint|host|hostname|ip|bind[_-]?ip|port|connection[_-]?url|base[_-]?url)($|[_-])",
    re.IGNORECASE,
)
URL_RE = re.compile(r"\bhttps?://[^\s\"'`<>),}]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
INTERNAL_DOMAIN_RE = re.compile(r"\b(?:[A-Za-z0-9_-]+\.)+(?:internal|corp)\b|(?<!\S)\*\.(?:internal|corp)\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
HOME_PATH_RE = re.compile(r"(?<![\w/])(?:/home/[^/\s\"']+|/Users/[^/\s\"']+|~)(?:/[^\s\"']*)?")
TOKEN_VALUE_RE = re.compile(r"\b(?:sk|pk|ak|rk|xoxb|ghp|glpat)-[A-Za-z0-9_\-]{8,}\b", re.IGNORECASE)
BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{6,}\b", re.IGNORECASE)
SECRET_ASSIGN_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_.-]*(?:api[_-]?key|(?<![A-Za-z])token(?!s?[A-Za-z])|secret|password|credential|authorization|auth[_-]?header)[A-Za-z0-9_.-]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\"'\s,}#]+)",
    re.IGNORECASE,
)

ALLOWED_URL_PREFIXES = (
    "https://json.schemastore.org/",
)

PLACEHOLDER_RE = re.compile(r"(\$\{[A-Z0-9_]+\}|<REPLACE_WITH_[A-Z0-9_]+>|replace-me|change-me|placeholder)", re.IGNORECASE)
UNRENDERABLE_PLACEHOLDERS = {"${HOST_PATH}"}
DOC_IPV4_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    severity: str
    message: str
    snippet: str


class SanitizeResult(TypedDict):
    changed: list[str]
    skipped_forbidden: list[str]


def _repo_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_text_file(path: Path) -> bool:
    if path.suffix in TEXT_SUFFIXES or path.name in {".mcp.json", ".worktreeinclude"}:
        return True
    return path.name.endswith(".example")


def _safe_read_text(path: Path) -> str | None:
    if not _is_text_file(path):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER_RE.search(value)) or value.strip() in {"", "{}"}


def _is_allowed_url(url: str) -> bool:
    return url.startswith(ALLOWED_URL_PREFIXES) or _is_placeholder(url)


def _ipv4(value: str) -> ipaddress.IPv4Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_doc_ip(address: ipaddress.IPv4Address) -> bool:
    return any(address in network for network in DOC_IPV4_NETWORKS)


def _is_private_or_local_ip(address: ipaddress.IPv4Address) -> bool:
    return address.is_private or address.is_loopback or address.is_link_local


def _is_sample_path(rel_path: str) -> bool:
    lowered = rel_path.lower()
    return "sample" in lowered or "/evals/" in lowered or "example" in lowered


def _placeholder_for_key(key: str) -> str:
    lowered = key.lower()
    if "authorization" in lowered or "auth" in lowered:
        return "Bearer ${AUTH_TOKEN}"
    if "password" in lowered or "passwd" in lowered:
        return "${PASSWORD}"
    if "secret" in lowered:
        return "${SECRET_VALUE}"
    if "token" in lowered:
        return "${API_TOKEN}"
    if "key" in lowered:
        return "${API_KEY}"
    return "${SECRET_VALUE}"


def _placeholder_for_endpoint(key: str, value: str) -> str:
    context = f"{key} {value}".lower()
    if "langfuse" in context:
        return "${LANGFUSE_URL}"
    if "mcp" in context:
        return "${MCP_SERVER_URL}"
    if "soc" in context:
        return "${SOC_API_URL}"
    if "security_kb" in context or "security-kb" in context:
        return "${SECURITY_KB_API_URL}"
    if "s3" in context or "minio" in context:
        return "${S3_ENDPOINT_URL}"
    if "smtp" in context:
        return "${SMTP_CONNECTION_URL}"
    if "redis" in context:
        return "${REDIS_HOST}"
    if "postgres" in context or "database" in context or "clickhouse" in context:
        return "${DATABASE_URL}"
    if "port" in key.lower():
        return "${SERVICE_PORT}"
    if "host" in key.lower() or "ip" in key.lower():
        return "${SERVICE_HOST}"
    return "${SERVICE_URL}"


def _redact_snippet(line: str) -> str:
    redacted = URL_RE.sub("<URL>", line)
    redacted = IPV4_RE.sub("<IP>", redacted)
    redacted = EMAIL_RE.sub("<EMAIL>", redacted)
    redacted = BEARER_RE.sub("Bearer <TOKEN>", redacted)
    redacted = TOKEN_VALUE_RE.sub("<TOKEN>", redacted)
    redacted = SECRET_ASSIGN_RE.sub(lambda match: f"{match.group('key')}{match.group('sep')}<VALUE>", redacted)
    return redacted.strip()[:180]


def _is_prebuilt_agent_workspace_seed(parts: tuple[str, ...]) -> bool:
    """预制业务 Agent 的配置种子 data/business-agents/<id>/workspace/ 是合法模板源；
    其余 data/ 内容（runtime.sqlite3/sessions/claude-root/version 等）仍属运行态、禁止入模板。"""
    return len(parts) >= 4 and parts[0] == "data" and parts[1] == "business-agents" and parts[3] == "workspace"


def _forbidden_reason(rel: Path) -> str | None:
    parts = rel.parts
    if not parts:
        return None
    if parts[0] in FORBIDDEN_TOP_LEVEL_DIRS and not _is_prebuilt_agent_workspace_seed(parts):
        return f"top-level runtime directory '{parts[0]}' is not a template source"
    if any(part in FORBIDDEN_DIR_NAMES for part in parts):
        return "runtime/cache/state directory is forbidden in templates"
    if ".claude" in parts and ("projects" in parts or "telemetry" in parts or "agent-memory-local" in parts):
        return "Claude local state is forbidden in templates"
    name = rel.name
    if name in FORBIDDEN_FILE_NAMES:
        return f"local/private file '{name}' is forbidden in templates"
    if name.startswith(".env."):
        return "env override files are forbidden in templates"
    if ".local." in name and not name.endswith(".example"):
        return "local override files are forbidden in templates"
    if ".bak-" in name or any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return "database, key, backup, and log files are forbidden in templates"
    return None


def iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def scan_path(root: Path) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []
    for file_path in iter_files(root):
        rel = file_path.relative_to(root)
        rel_text = rel.as_posix()
        reason = _forbidden_reason(rel)
        if reason:
            findings.append(Finding(rel_text, 0, "forbidden_path", "high", reason, ""))
            continue
        text = _safe_read_text(file_path)
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            findings.extend(_scan_line(rel_text, line_number, line))
    return findings


def _scan_line(rel_path: str, line_number: int, line: str) -> list[Finding]:
    findings: list[Finding] = []
    for placeholder in sorted(UNRENDERABLE_PLACEHOLDERS):
        if placeholder in line:
            findings.append(
                Finding(
                    rel_path,
                    line_number,
                    "unrenderable_placeholder",
                    "high",
                    f"{placeholder} is a sanitization fallback, not a deployable runtime-volume-seeds placeholder",
                    _redact_snippet(line),
                )
            )
    for match in URL_RE.finditer(line):
        value = match.group(0)
        if _is_allowed_url(value):
            continue
        findings.append(Finding(rel_path, line_number, "endpoint_url", "high", "URL or endpoint must be injected by deployment config", _redact_snippet(line)))
    for match in IPV4_RE.finditer(line):
        value = match.group(0)
        address = _ipv4(value)
        if address is None or _is_doc_ip(address):
            continue
        if _is_private_or_local_ip(address):
            findings.append(Finding(rel_path, line_number, "private_ip", "high", "private, local, or environment-bound IP must not be stored in templates", _redact_snippet(line)))
    if "localhost" in line or "host.docker.internal" in line:
        findings.append(Finding(rel_path, line_number, "local_host", "high", "local host names must be deployment placeholders in templates", _redact_snippet(line)))
    if INTERNAL_DOMAIN_RE.search(line):
        findings.append(Finding(rel_path, line_number, "internal_domain", "high", "internal domains must be deployment placeholders in templates", _redact_snippet(line)))
    if EMAIL_RE.search(line):
        findings.append(Finding(rel_path, line_number, "email", "medium", "email/account values should not be stored in reusable templates", _redact_snippet(line)))
    if HOME_PATH_RE.search(line):
        findings.append(Finding(rel_path, line_number, "host_path", "high", "host-specific paths must not be stored in templates", _redact_snippet(line)))
    has_secret_assignment = any(
        not match.group("value").startswith(("$", "<")) and not _is_placeholder(match.group("value"))
        for match in SECRET_ASSIGN_RE.finditer(line)
    )
    if TOKEN_VALUE_RE.search(line) or BEARER_RE.search(line) or has_secret_assignment:
        findings.append(Finding(rel_path, line_number, "secret", "high", "secret-like values must be placeholders", _redact_snippet(line)))
    return findings


def sanitize_path(root: Path) -> SanitizeResult:
    root = root.resolve()
    changed: list[str] = []
    skipped_forbidden: list[str] = []
    for file_path in iter_files(root):
        rel = file_path.relative_to(root)
        reason = _forbidden_reason(rel)
        if reason:
            skipped_forbidden.append(rel.as_posix())
            continue
        if _sanitize_file(root, file_path):
            changed.append(rel.as_posix())
    return {"changed": changed, "skipped_forbidden": skipped_forbidden}


def _sanitize_file(root: Path, file_path: Path) -> bool:
    text = _safe_read_text(file_path)
    if text is None:
        return False
    rel_text = _repo_relative(file_path, root)
    if file_path.suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            new_text = _sanitize_text(text, rel_text)
        else:
            data = _sanitize_json_value(data, "", rel_text)
            new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        new_text = _sanitize_text(text, rel_text)
    if new_text == text:
        return False
    file_path.write_text(new_text, encoding="utf-8")
    return True


def _sanitize_json_value(value: object, key: str, rel_path: str) -> object:
    if isinstance(value, dict):
        return {item_key: _sanitize_json_value(item_value, item_key, rel_path) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_value(item, key, rel_path) for item in value]
    if isinstance(value, str):
        if key == "$schema" and _is_allowed_url(value):
            return value
        if _is_sample_path(rel_path) and _ipv4(value):
            address = _ipv4(value)
            if address and _is_doc_ip(address):
                return value
            if address and _is_private_or_local_ip(address):
                return "192.0.2.10"
        if SECRET_KEY_RE.search(key) and not _is_placeholder(value):
            return _placeholder_for_key(key)
        if ENDPOINT_KEY_RE.search(key) and not _is_placeholder(value):
            return _placeholder_for_endpoint(key, value)
        return _sanitize_text(value, rel_path)
    if isinstance(value, int) and ENDPOINT_KEY_RE.search(key) and "port" in key.lower():
        return "${SERVICE_PORT}"
    return value


def _sanitize_text(text: str, rel_path: str) -> str:
    sample = _is_sample_path(rel_path)

    def replace_secret(match: re.Match[str]) -> str:
        value = match.group("value")
        if value.startswith(("$", "<")) or _is_placeholder(value):
            return match.group(0)
        return f"{match.group('key')}{match.group('sep')}{match.group('quote')}{_placeholder_for_key(match.group('key'))}"

    def replace_url(match: re.Match[str]) -> str:
        value = match.group(0)
        if _is_allowed_url(value):
            return value
        return _placeholder_for_endpoint("", value)

    def replace_ip(match: re.Match[str]) -> str:
        value = match.group(0)
        address = _ipv4(value)
        if address is None or _is_doc_ip(address):
            return value
        if _is_private_or_local_ip(address):
            return "192.0.2.10" if sample else "${SERVICE_HOST}"
        return value

    sanitized = SECRET_ASSIGN_RE.sub(replace_secret, text)
    sanitized = BEARER_RE.sub("Bearer ${AUTH_TOKEN}", sanitized)
    sanitized = TOKEN_VALUE_RE.sub("${API_KEY}", sanitized)
    sanitized = URL_RE.sub(replace_url, sanitized)
    sanitized = IPV4_RE.sub(replace_ip, sanitized)
    sanitized = sanitized.replace("host.docker.internal", "${SERVICE_HOST}")
    sanitized = sanitized.replace("localhost", "${SERVICE_HOST}")
    sanitized = INTERNAL_DOMAIN_RE.sub("${INTERNAL_DOMAIN}", sanitized)
    sanitized = EMAIL_RE.sub("${CONTACT_EMAIL}", sanitized)
    sanitized = HOME_PATH_RE.sub("${HOST_PATH}", sanitized)
    return sanitized


def _print_report(findings: list[Finding]) -> None:
    report = {
        "ok": not any(finding.severity == "high" for finding in findings),
        "findings": [asdict(finding) for finding in findings],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan, sanitize, and verify runtime templates.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "sanitize", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.command == "scan":
        _print_report(scan_path(args.path))
        return 0
    if args.command == "sanitize":
        result = sanitize_path(args.path)
        findings = scan_path(args.path)
        print(json.dumps({"sanitize": result, "verify_ok": not any(f.severity == "high" for f in findings), "findings": [asdict(f) for f in findings]}, ensure_ascii=False, indent=2))
        return 1 if any(f.severity == "high" for f in findings) else 0
    if args.command == "verify":
        findings = scan_path(args.path)
        _print_report(findings)
        return 1 if any(finding.severity == "high" for finding in findings) else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
