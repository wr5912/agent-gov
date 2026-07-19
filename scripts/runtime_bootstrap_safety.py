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
from urllib.parse import parse_qsl, urlsplit

import yaml

from runtime_bootstrap_secret_assignments import (
    SECRET_ASSIGN_RE,
    secret_assignment_key,
    secret_assignment_value,
)

FORBIDDEN_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".runtime-bootstrap-backups",
    ".runtime-bootstrap-staging",
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

SQLITE_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SQLITE_SIDECAR_SUFFIXES = tuple(
    f"{database_suffix}{sidecar_suffix}" for database_suffix in SQLITE_DATABASE_SUFFIXES for sidecar_suffix in ("-journal", "-shm", "-wal")
)

FORBIDDEN_SUFFIXES = {
    ".bak",
    ".backup",
    ".key",
    ".log",
    ".pem",
    *SQLITE_DATABASE_SUFFIXES,
    *SQLITE_SIDECAR_SUFFIXES,
}

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|(?<![A-Za-z])token(?!s?[A-Za-z])|secret|password|passwd|credential|authorization|auth[_-]?header|private[_-]?key|encryption[_-]?key|salt)",
    re.IGNORECASE,
)
JSON_SECRET_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:api[_.-]?key|token|access[_.-]?token|refresh[_.-]?token|secret|password|passwd|credential|authorization|auth[_.-]?header|private[_.-]?key)(?:$|[_.-])",
    re.IGNORECASE,
)
ENDPOINT_KEY_RE = re.compile(
    r"(^|[_-])(url|uri|endpoint|host|hostname|ip|bind[_-]?ip|port|connection[_-]?url|base[_-]?url)($|[_-])",
    re.IGNORECASE,
)
SCHEME_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'`<>),}]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
INTERNAL_DOMAIN_RE = re.compile(r"\b(?:[A-Za-z0-9_-]+\.)+(?:internal|corp)\b|(?<!\S)\*\.(?:internal|corp)\b")
ALLOWED_DOMAINS_BLOCK_RE = re.compile(r'"allowedDomains"\s*:\s*\[(?P<body>.*?)\]', re.DOTALL)
JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
HOME_PATH_RE = re.compile(
    r"(?<![\w/])(?:/home/[^/\s\"']+|/Users/[^/\s\"']+|/root|~)(?:/[^\s\"']*)?"
    r"|(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s\"']+(?:[\\/][^\s\"']*)?",
    re.IGNORECASE,
)
TOKEN_VALUE_RE = re.compile(
    r"\b(?:"
    r"(?:sk|pk|ak|rk|xox[a-z]?|glpat)[_-][A-Za-z0-9][A-Za-z0-9_-]{7,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r")\b",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{6,}\b", re.IGNORECASE)
PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN (?:(?:RSA|DSA|EC|OPENSSH) )?PRIVATE KEY-----"
    r"|-----BEGIN PGP PRIVATE KEY BLOCK-----",
    re.IGNORECASE,
)
ALLOWED_URL_PREFIXES = ("https://json.schemastore.org/",)
ENVIRONMENT_URL_SCHEMES = {
    "amqp",
    "amqps",
    "http",
    "https",
    "jdbc",
    "mariadb",
    "mongodb",
    "mongodb+srv",
    "mysql",
    "postgres",
    "postgresql",
    "redis",
    "rediss",
    "s3",
}

PLACEHOLDER_RE = re.compile(r"(\$\{[A-Z0-9_]+\}|<REPLACE_WITH_[A-Z0-9_]+>|replace-me|change-me|placeholder)", re.IGNORECASE)
UNRENDERABLE_PLACEHOLDERS = {"${HOST_PATH}"}
DOC_IPV4_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
MODE_NEUTRAL_SANDBOX_DOMAINS = {
    "localhost",
    "127.0.0.1",
    "host.docker.internal",
    "*.internal",
    "*.corp",
}


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


def _safe_read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER_RE.search(value)) or value.strip() in {"", "{}"}


def _is_allowed_url(url: str) -> bool:
    return url.startswith(ALLOWED_URL_PREFIXES) or _is_placeholder(url)


def _is_environment_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() in ENVIRONMENT_URL_SCHEMES
    except ValueError:
        return False


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
    if any(value in context for value in ("mysql", "mariadb", "mongodb", "jdbc")):
        return "${DATABASE_URL}"
    if "port" in key.lower():
        return "${SERVICE_PORT}"
    if "host" in key.lower() or "ip" in key.lower():
        return "${SERVICE_HOST}"
    return "${SERVICE_URL}"


def _redact_snippet(line: str) -> str:
    redacted = SCHEME_URL_RE.sub("<URL>", line)
    redacted = IPV4_RE.sub("<IP>", redacted)
    redacted = EMAIL_RE.sub("<EMAIL>", redacted)
    redacted = HOME_PATH_RE.sub("<PATH>", redacted)
    redacted = BEARER_RE.sub("Bearer <TOKEN>", redacted)
    redacted = TOKEN_VALUE_RE.sub("<TOKEN>", redacted)
    redacted = PRIVATE_KEY_BLOCK_RE.sub("<PRIVATE_KEY>", redacted)
    redacted = SECRET_ASSIGN_RE.sub(
        lambda match: f"{secret_assignment_key(match)}{match.group('sep')}<VALUE>",
        redacted,
    )
    return redacted.strip()[:180]


def _is_builtin_business_agent_workspace(parts: tuple[str, ...]) -> bool:
    """仅 business-agents/<id>/workspace/ 是合法内置业务 Agent Workspace。"""
    return len(parts) >= 3 and parts[0] == "business-agents" and parts[2] == "workspace"


def _is_builtin_business_agent_workspace_path(rel_path: str) -> bool:
    return _is_builtin_business_agent_workspace(Path(rel_path).parts)


def _environment_bound_severity(rel_path: str) -> str:
    return "medium" if _is_builtin_business_agent_workspace_path(rel_path) else "high"


def _environment_bound_message(rel_path: str, strict_message: str) -> str:
    if not _is_builtin_business_agent_workspace_path(rel_path):
        return strict_message
    return "built-in Workspace keeps this value byte-for-byte; review whether it is intentional or should use deployment config"


def _url_contains_secret(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return True
    return any(SECRET_KEY_RE.search(key) and value and not _is_placeholder(value) for key, value in parse_qsl(parsed.query, keep_blank_values=True))


def _forbidden_reason(rel: Path) -> str | None:
    parts = rel.parts
    if not parts:
        return None
    if parts[0] in FORBIDDEN_TOP_LEVEL_DIRS and not _is_builtin_business_agent_workspace(parts):
        return f"top-level runtime directory '{parts[0]}' is not an initialization source"
    if any(part in FORBIDDEN_DIR_NAMES for part in parts):
        return "runtime/cache/state directory is forbidden in the initialization source"
    if ".claude" in parts and ("projects" in parts or "telemetry" in parts or "agent-memory-local" in parts):
        return "Claude local state is forbidden in the initialization source"
    name = rel.name
    if name in FORBIDDEN_FILE_NAMES:
        return f"local/private file '{name}' is forbidden in the initialization source"
    if name.startswith(".env."):
        return "env override files are forbidden in the initialization source"
    if ".local." in name and not name.endswith(".example"):
        return "local override files are forbidden in the initialization source"
    if ".bak-" in name or any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return "database, key, backup, and log files are forbidden in the initialization source"
    return None


def iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_symlink() and path.is_file():
            yield path


def scan_path(root: Path) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []
    for file_path in sorted(root.rglob("*")):
        rel = file_path.relative_to(root)
        rel_text = rel.as_posix()
        if file_path.is_symlink():
            findings.append(
                Finding(
                    rel_text,
                    0,
                    "unsafe_file_type",
                    "high",
                    "symbolic links are forbidden in the runtime initialization source",
                    "",
                )
            )
            continue
        if not file_path.is_file():
            continue
        reason = _forbidden_reason(rel)
        if reason:
            findings.append(Finding(rel_text, 0, "forbidden_path", "high", reason, ""))
            continue
        text = _safe_read_text(file_path)
        if text is None:
            continue
        if file_path.suffix in {".json", ".yaml", ".yml"} or file_path.name in {
            ".mcp.json",
            "settings.json",
        }:
            findings.extend(_scan_structured_secret_values(rel_text, text))
        findings.extend(_scan_builtin_workspace_wide_permissions(rel_text, text))
        mode_neutral_domain_lines = _mode_neutral_sandbox_domain_lines(rel_text, text)
        for line_number, line in enumerate(text.splitlines(), start=1):
            findings.extend(
                _scan_line(
                    rel_text,
                    line_number,
                    line,
                    mode_neutral_domain=line_number in mode_neutral_domain_lines,
                )
            )
    return findings


def _json_secret_value_present(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and not _is_placeholder(value)
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _structured_key_line(text: str, key: str) -> int:
    match = re.search(
        rf"(?m)^\s*[\"']?{re.escape(key)}[\"']?\s*:",
        text,
    )
    return text.count("\n", 0, match.start()) + 1 if match else 0


def _scan_structured_secret_values(rel_path: str, text: str) -> list[Finding]:
    try:
        payload = json.loads(text) if Path(rel_path).suffix == ".json" or rel_path.endswith(".mcp.json") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError):
        return []

    findings: list[Finding] = []

    def visit(value: object, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                item_path = (*path, key_text)
                if JSON_SECRET_KEY_RE.search(key_text) and _json_secret_value_present(item):
                    findings.append(
                        Finding(
                            rel_path,
                            _structured_key_line(text, key_text),
                            "secret",
                            "high",
                            "credential-bearing structured fields must use an empty value or deployment placeholder",
                            f"{'.'.join(item_path)}=<VALUE>",
                        )
                    )
                visit(item, item_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, (*path, str(index)))

    visit(payload, ())
    return findings


def _scan_builtin_workspace_wide_permissions(rel_path: str, text: str) -> list[Finding]:
    if not _is_builtin_business_agent_workspace_path(rel_path) or not rel_path.endswith("/.claude/settings.json"):
        return []
    try:
        settings = json.loads(text)
    except json.JSONDecodeError:
        return []
    permissions = settings.get("permissions") if isinstance(settings, dict) else None
    allowed = permissions.get("allow") if isinstance(permissions, dict) else None
    if not isinstance(allowed, list):
        return []

    findings: list[Finding] = []
    for rule in allowed:
        if not isinstance(rule, str):
            continue
        is_wide_bash = rule == "Bash(*)"
        is_wide_mcp = rule.startswith("mcp__") and rule.endswith("__*")
        if not is_wide_bash and not is_wide_mcp:
            continue
        encoded_rule = json.dumps(rule)
        position = text.find(encoded_rule)
        line_number = text.count("\n", 0, position) + 1 if position >= 0 else 0
        findings.append(
            Finding(
                rel_path,
                line_number,
                "wide_permission",
                "medium",
                "built-in Workspace keeps this broad allow rule byte-for-byte; review whether the scope is intentional",
                rule,
            )
        )
    return findings


def _scan_line(
    rel_path: str,
    line_number: int,
    line: str,
    *,
    mode_neutral_domain: bool = False,
) -> list[Finding]:
    findings: list[Finding] = []
    for placeholder in sorted(UNRENDERABLE_PLACEHOLDERS):
        if placeholder in line:
            findings.append(
                Finding(
                    rel_path,
                    line_number,
                    "unrenderable_placeholder",
                    "high",
                    f"{placeholder} is a sanitization fallback, not a deployable runtime-bootstrap placeholder",
                    _redact_snippet(line),
                )
            )
    findings.extend(_scan_line_urls(rel_path, line_number, line))
    findings.extend(
        _scan_line_environment(
            rel_path,
            line_number,
            line,
            mode_neutral_domain=mode_neutral_domain,
        )
    )
    findings.extend(_scan_line_sensitive_values(rel_path, line_number, line))
    return findings


def _scan_line_urls(
    rel_path: str,
    line_number: int,
    line: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for match in SCHEME_URL_RE.finditer(line):
        value = match.group(0)
        if _is_allowed_url(value):
            continue
        if _url_contains_secret(value):
            findings.append(
                Finding(
                    rel_path,
                    line_number,
                    "secret",
                    "high",
                    "URL userinfo or secret query values must not be stored in the runtime initialization source",
                    _redact_snippet(line),
                )
            )
            continue
        if not _is_environment_url(value):
            continue
        findings.append(
            Finding(
                rel_path,
                line_number,
                "endpoint_url",
                _environment_bound_severity(rel_path),
                _environment_bound_message(rel_path, "URL or endpoint must be injected by deployment config"),
                _redact_snippet(line),
            )
        )
    return findings


def _scan_line_environment(
    rel_path: str,
    line_number: int,
    line: str,
    *,
    mode_neutral_domain: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    for match in IPV4_RE.finditer(line):
        value = match.group(0)
        address = _ipv4(value)
        if address is None or _is_doc_ip(address):
            continue
        if _is_private_or_local_ip(address) and not mode_neutral_domain:
            findings.append(
                Finding(
                    rel_path,
                    line_number,
                    "private_ip",
                    _environment_bound_severity(rel_path),
                    _environment_bound_message(rel_path, "private, local, or environment-bound IP must not be stored in the initialization source"),
                    _redact_snippet(line),
                )
            )
    if ("localhost" in line or "host.docker.internal" in line) and not mode_neutral_domain:
        findings.append(
            Finding(
                rel_path,
                line_number,
                "local_host",
                _environment_bound_severity(rel_path),
                _environment_bound_message(rel_path, "local host names must be deployment placeholders in the initialization source"),
                _redact_snippet(line),
            )
        )
    if INTERNAL_DOMAIN_RE.search(line) and not mode_neutral_domain:
        findings.append(
            Finding(
                rel_path,
                line_number,
                "internal_domain",
                _environment_bound_severity(rel_path),
                _environment_bound_message(rel_path, "internal domains must be deployment placeholders in the initialization source"),
                _redact_snippet(line),
            )
        )
    return findings


def _scan_line_sensitive_values(
    rel_path: str,
    line_number: int,
    line: str,
) -> list[Finding]:
    findings: list[Finding] = []
    if EMAIL_RE.search(line):
        findings.append(
            Finding(rel_path, line_number, "email", "medium", "email/account values should not be stored in the initialization source", _redact_snippet(line))
        )
    if HOME_PATH_RE.search(line):
        findings.append(
            Finding(rel_path, line_number, "host_path", "high", "host-specific paths must not be stored in the initialization source", _redact_snippet(line))
        )
    has_secret_assignment = any(
        not secret_assignment_value(match).startswith(("$", "<")) and not _is_placeholder(secret_assignment_value(match))
        for match in SECRET_ASSIGN_RE.finditer(line)
    )
    if PRIVATE_KEY_BLOCK_RE.search(line):
        findings.append(
            Finding(rel_path, line_number, "private_key", "high", "private key material must not be stored in the initialization source", _redact_snippet(line))
        )
    if TOKEN_VALUE_RE.search(line) or BEARER_RE.search(line) or has_secret_assignment:
        findings.append(Finding(rel_path, line_number, "secret", "high", "secret-like values must be placeholders", _redact_snippet(line)))
    return findings


def _mode_neutral_sandbox_domain_lines(rel_path: str, text: str) -> set[int]:
    if not rel_path.endswith("/.claude/settings.json"):
        return set()
    try:
        settings = json.loads(text)
    except json.JSONDecodeError:
        return set()
    sandbox = settings.get("sandbox") if isinstance(settings, dict) else None
    network = sandbox.get("network") if isinstance(sandbox, dict) else None
    domains = network.get("allowedDomains") if isinstance(network, dict) else None
    approved = {value for value in domains if isinstance(value, str) and value in MODE_NEUTRAL_SANDBOX_DOMAINS} if isinstance(domains, list) else set()
    if not approved:
        return set()

    lines: set[int] = set()
    for block in ALLOWED_DOMAINS_BLOCK_RE.finditer(text):
        body = block.group("body")
        try:
            block_domains = json.loads(f"[{body}]")
        except json.JSONDecodeError:
            continue
        if block_domains != domains:
            continue
        body_offset = block.start("body")
        for token in JSON_STRING_RE.finditer(body):
            try:
                value = json.loads(token.group(0))
            except json.JSONDecodeError:
                continue
            if value in approved:
                lines.add(text.count("\n", 0, body_offset + token.start()) + 1)
    return lines


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


def _sanitize_json_value(
    value: object,
    key: str,
    rel_path: str,
    json_path: tuple[str, ...] = (),
) -> object:
    if isinstance(value, dict):
        return {
            item_key: _sanitize_json_value(
                item_value,
                item_key,
                rel_path,
                (*json_path, item_key),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json_value(item, key, rel_path, json_path) for item in value]
    if isinstance(value, str):
        if rel_path.endswith("/.claude/settings.json") and json_path == ("sandbox", "network", "allowedDomains") and value in MODE_NEUTRAL_SANDBOX_DOMAINS:
            return value
        if key == "$schema" and _is_allowed_url(value):
            return value
        if _is_sample_path(rel_path) and _ipv4(value):
            address = _ipv4(value)
            if address and _is_doc_ip(address):
                return value
            if address and _is_private_or_local_ip(address):
                return "192.0.2.10"
        if JSON_SECRET_KEY_RE.search(key) and not _is_placeholder(value):
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
        value = secret_assignment_value(match)
        if value.startswith(("$", "<")) or _is_placeholder(value):
            return match.group(0)
        raw_value = match.group("raw_value")
        quote = raw_value[0] if raw_value[:1] in {'"', "'"} else ""
        return f"{secret_assignment_key(match)}{match.group('sep')}{quote}{_placeholder_for_key(match.group('key'))}{quote}"

    def replace_url(match: re.Match[str]) -> str:
        value = match.group(0)
        if _is_allowed_url(value) or (not _url_contains_secret(value) and not _is_environment_url(value)):
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
    sanitized = SCHEME_URL_RE.sub(replace_url, sanitized)
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
    parser = argparse.ArgumentParser(description="Scan, sanitize, and verify the runtime initialization source.")
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
        print(
            json.dumps(
                {"sanitize": result, "verify_ok": not any(f.severity == "high" for f in findings), "findings": [asdict(f) for f in findings]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if any(f.severity == "high" for f in findings) else 0
    if args.command == "verify":
        findings = scan_path(args.path)
        _print_report(findings)
        return 1 if any(finding.severity == "high" for finding in findings) else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
