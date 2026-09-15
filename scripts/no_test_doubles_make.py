"""正式验收 Make 与 package script 调用闭包的静态解析。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from scripts.no_test_doubles_contract import (
    AUDITED_MAKE_SHELL_LINES,
    CANONICAL_AUXILIARY_RECIPE_SHA256,
    CANONICAL_FORMAL_DISPATCHES,
    CANONICAL_FORMAL_MAKE_BINDINGS,
    CANONICAL_FORMAL_PREREQUISITES,
    CANONICAL_PRIVATE_RECIPE_SHA256,
    DYNAMIC_MAKE_RULES,
    FORMAL_TARGET_ENTRYPOINT_MARKERS,
    INTERPRETER_INVOCATION,
    LOCAL_SOURCE_REFERENCE,
    LOCAL_SOURCE_SUFFIXES,
    MAKE_RULES,
    MAKE_TARGET_PATTERN,
    MAKE_VARIABLE_ASSIGNMENT,
    MAKE_VARIABLE_REFERENCE,
    PACKAGE_SCRIPT_INVOCATION,
    PROTECTED_MAKE_VARIABLES,
    RECURSIVE_MAKE_MARKER,
    REPO_ROOT,
    REQUIRED_FORMAL_TARGET_FILES,
    Finding,
    FormalMakeInspection,
    MakeRule,
    ParsedMakefile,
)


def _logical_make_lines(lines: list[str]) -> tuple[tuple[int, str], ...]:
    logical: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        start = index + 1
        line = lines[index]
        while line.rstrip().endswith("\\") and index + 1 < len(lines):
            line = f"{line.rstrip()[:-1]} {lines[index + 1].lstrip()}"
            index += 1
        logical.append((start, line))
        index += 1
    return tuple(logical)


def _strip_make_comment(source: str) -> str:
    """按 Make 的反斜杠转义规则移除非 recipe 注释。"""

    for index, character in enumerate(source):
        if character != "#":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and source[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return source[:index].rstrip()
    return source


def _strip_shell_comment(source: str) -> str:
    """移除 shell recipe 中位于词首的非引号注释。"""

    single_quote = False
    double_quote = False
    escaped = False
    for index, character in enumerate(source):
        if escaped:
            escaped = False
            continue
        if character == "\\" and not single_quote:
            escaped = True
            continue
        if character == "'" and not double_quote:
            single_quote = not single_quote
            continue
        if character == '"' and not single_quote:
            double_quote = not double_quote
            continue
        if character == "#" and not single_quote and not double_quote and (index == 0 or source[index - 1].isspace()):
            return source[:index].rstrip()
    return source


def _normalized_make_source(source: str) -> str:
    return " ".join(source.strip().split())


def _parse_makefile(lines: list[str]) -> ParsedMakefile:
    rules: dict[str, MakeRule] = {}
    variables: dict[str, str] = {}
    current_targets: tuple[str, ...] = ()
    for line_number, line in _logical_make_lines(lines):
        if line.startswith("\t"):
            for target in current_targets:
                rules[target].recipes.append((line_number, line))
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        current_targets = ()
        uncommented = _strip_make_comment(line)
        assignment = MAKE_VARIABLE_ASSIGNMENT.match(uncommented)
        if assignment is not None:
            name = assignment.group("name")
            value = assignment.group("value")
            if assignment.group("operator") == "+=" and name in variables:
                variables[name] = f"{variables[name]} {value}"
            elif assignment.group("operator") != "?=" or name not in variables:
                variables[name] = value
            continue
        match = MAKE_TARGET_PATTERN.match(uncommented) if uncommented and not uncommented[0].isspace() else None
        if match is None:
            continue
        targets = tuple(match.group(1).split())
        remainder = match.group(2)
        prerequisite_text, separator, inline_recipe = remainder.partition(";")
        prerequisites = {item for item in prerequisite_text.split("#", maxsplit=1)[0].split() if item not in {"|", ".WAIT"}}
        for target in targets:
            rule = rules.setdefault(target, MakeRule(prerequisites=set(), recipes=[]))
            rule.prerequisites.update(prerequisites)
            if separator and inline_recipe.strip():
                rule.recipes.append((line_number, inline_recipe))
        current_targets = targets
    return ParsedMakefile(rules=rules, variables=variables)


def _expand_make_variables(source: str, variables: Mapping[str, str]) -> str:
    expanded = source
    visited: set[str] = set()
    for _ in range(len(variables) + 1):
        referenced = {
            match.group("paren") or match.group("brace")
            for match in MAKE_VARIABLE_REFERENCE.finditer(expanded)
            if (match.group("paren") or match.group("brace")) in variables
        }
        unresolved = referenced - visited
        if not unresolved:
            break
        visited.update(unresolved)

        def replace(match: re.Match[str]) -> str:
            name = match.group("paren") or match.group("brace")
            return variables.get(name, match.group(0))

        expanded = MAKE_VARIABLE_REFERENCE.sub(replace, expanded)
    return expanded


def _resolve_local_source(
    raw_reference: str,
    *,
    cwd: Path,
    repo_root: Path,
    finding_path: str,
    line_number: int,
    findings: set[Finding],
) -> Path | None:
    reference = raw_reference.strip().strip("\"'")
    if "$" in reference:
        findings.add(Finding(finding_path, line_number, "dynamic Python/Node script path is not auditable"))
        return None
    candidate = Path(reference)
    resolved = (candidate if candidate.is_absolute() else cwd / candidate).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        findings.add(Finding(finding_path, line_number, "formal Make recipe invokes source outside repository"))
        return None
    if resolved.suffix not in LOCAL_SOURCE_SUFFIXES:
        findings.add(Finding(finding_path, line_number, "formal Make recipe invokes unsupported local source"))
        return None
    if not resolved.is_file():
        findings.add(Finding(finding_path, line_number, f"formal Make recipe source is missing: {reference}"))
        return None
    return resolved


def _inspect_source_command(
    command: str,
    *,
    cwd: Path,
    repo_root: Path,
    finding_path: str,
    line_number: int,
    findings: set[Finding],
) -> set[Path]:
    files: set[Path] = set()
    invoked_source_spans: list[tuple[int, int]] = []
    for match in INTERPRETER_INVOCATION.finditer(command):
        invoked_source_spans.append(match.span("argument"))
        argument = match.group("argument").strip().strip("\"'")
        if argument in {"--version", "-V"}:
            continue
        if argument.startswith(("-c", "-e", "--eval", "-m")):
            findings.add(Finding(finding_path, line_number, "inline or module Python/Node execution is not auditable"))
        elif "$" in argument:
            findings.add(Finding(finding_path, line_number, "dynamic Python/Node script path is not auditable"))
        elif Path(argument).suffix in LOCAL_SOURCE_SUFFIXES:
            resolved = _resolve_local_source(argument, cwd=cwd, repo_root=repo_root, finding_path=finding_path, line_number=line_number, findings=findings)
            if resolved is not None:
                files.add(resolved)
        else:
            findings.add(Finding(finding_path, line_number, "Python/Node entrypoint is not an auditable source file"))
    for source_match in LOCAL_SOURCE_REFERENCE.finditer(command):
        start, end = source_match.span(1)
        if not any(span_start <= start and end <= span_end for span_start, span_end in invoked_source_spans):
            findings.add(
                Finding(
                    finding_path,
                    line_number,
                    "local source path is not an auditable interpreter invocation",
                )
            )
    return files


def _package_script_line(source: str, script_name: str) -> int:
    needle = json.dumps(script_name)
    return next((number for number, line in enumerate(source.splitlines(), start=1) if needle in line), 1)


def _inspect_package_script(
    package_dir: Path,
    script_name: str,
    *,
    repo_root: Path,
    files: set[Path],
    findings: set[Finding],
    inspected: set[tuple[Path, str]],
) -> None:
    key = (package_dir.resolve(), script_name)
    if key in inspected:
        return
    inspected.add(key)
    package_file = package_dir / "package.json"
    try:
        package_relative = package_file.relative_to(repo_root)
    except ValueError:
        findings.add(Finding("package.json", 1, "formal package command leaves repository"))
        return
    finding_path = f"{package_relative.as_posix()}#scripts.{script_name}"
    try:
        source = package_file.read_text(encoding="utf-8")
        payload = json.loads(source)
        command = payload["scripts"][script_name]
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        findings.add(Finding(finding_path, 1, f"cannot resolve formal package script: {exc}"))
        return
    if not isinstance(command, str) or not command.strip():
        findings.add(Finding(finding_path, 1, "formal package script must be a non-empty command"))
        return

    line_number = _package_script_line(source, script_name)
    cwd = package_dir.resolve()
    for segment in re.split(r"\s*(?:&&|;)\s*", command):
        cd_match = re.fullmatch(r"\s*cd\s+([\"']?)([^\"']+)\1\s*", segment)
        if cd_match is not None:
            requested = cd_match.group(2)
            if "$" in requested:
                findings.add(Finding(finding_path, line_number, "dynamic package-script working directory is not auditable"))
                continue
            cwd = (cwd / requested).resolve()
            try:
                cwd.relative_to(repo_root)
            except ValueError:
                findings.add(Finding(finding_path, line_number, "formal package script leaves repository"))
            continue
        files.update(
            _inspect_source_command(segment, cwd=cwd, repo_root=repo_root, finding_path=finding_path, line_number=line_number, findings=findings),
        )
        for invocation in PACKAGE_SCRIPT_INVOCATION.finditer(segment):
            raw_directory = invocation.group("directory")
            if raw_directory is not None and "$" in raw_directory:
                findings.add(Finding(finding_path, line_number, "dynamic package directory is not auditable"))
                continue
            nested_dir = cwd if raw_directory is None else (cwd / raw_directory.strip("\"'")).resolve()
            try:
                nested_dir.relative_to(repo_root)
            except ValueError:
                findings.add(Finding(finding_path, line_number, "formal package command leaves repository"))
                continue
            _inspect_package_script(
                nested_dir,
                invocation.group("script"),
                repo_root=repo_root,
                files=files,
                findings=findings,
                inspected=inspected,
            )


def _inspect_recipe(
    target: str,
    line_number: int,
    recipe: str,
    *,
    makefile_path: str,
    repo_root: Path,
    parsed: ParsedMakefile,
    pending: list[str],
    files: set[Path],
    findings: set[Finding],
    inspected_package_scripts: set[tuple[Path, str]],
) -> None:
    finding_path = f"{makefile_path}#{target}"
    recipe = _strip_shell_comment(recipe)
    for pattern, finding_rule in MAKE_RULES:
        if pattern.search(recipe):
            findings.add(Finding(finding_path, line_number, finding_rule))
    expanded = _expand_make_variables(recipe, parsed.variables)
    files.update(
        _inspect_source_command(
            expanded,
            cwd=repo_root,
            repo_root=repo_root,
            finding_path=finding_path,
            line_number=line_number,
            findings=findings,
        ),
    )
    if RECURSIVE_MAKE_MARKER.search(recipe) or RECURSIVE_MAKE_MARKER.search(expanded):
        recursive_targets = {
            candidate for candidate in parsed.rules if re.search(rf"(?<![A-Za-z0-9_./%+-]){re.escape(candidate)}(?![A-Za-z0-9_./%+-])", expanded)
        }
        pending.extend(recursive_targets)
        if not recursive_targets:
            findings.add(Finding(finding_path, line_number, "recursive Make target is not statically auditable"))
    for invocation in PACKAGE_SCRIPT_INVOCATION.finditer(expanded):
        raw_directory = invocation.group("directory")
        if raw_directory is not None and "$" in raw_directory:
            findings.add(Finding(finding_path, line_number, "dynamic package directory is not auditable"))
            continue
        package_dir = repo_root if raw_directory is None else (repo_root / raw_directory.strip("\"'")).resolve()
        try:
            package_dir.relative_to(repo_root)
        except ValueError:
            findings.add(Finding(finding_path, line_number, "formal package command leaves repository"))
            continue
        _inspect_package_script(
            package_dir,
            invocation.group("script"),
            repo_root=repo_root,
            files=files,
            findings=findings,
            inspected=inspected_package_scripts,
        )


def _walk_make_targets(
    requested: tuple[str, ...],
    *,
    makefile_path: str,
    repo_root: Path,
    parsed: ParsedMakefile,
) -> FormalMakeInspection:
    findings = {Finding(f"{makefile_path}#{target}", 1, "formal live Make target is missing") for target in requested if target not in parsed.rules}
    files: set[Path] = set()
    pending = list(requested)
    selected: set[str] = set()
    inspected_package_scripts: set[tuple[Path, str]] = set()
    while pending:
        target = pending.pop()
        if target in selected or target not in parsed.rules:
            continue
        selected.add(target)
        rule = parsed.rules[target]
        for prerequisite in sorted(rule.prerequisites):
            if "$" in prerequisite:
                findings.add(Finding(f"{makefile_path}#{target}", 1, "dynamic formal Make prerequisite is not auditable"))
            elif prerequisite in parsed.rules and prerequisite not in selected:
                pending.append(prerequisite)
        for line_number, recipe in rule.recipes:
            _inspect_recipe(
                target,
                line_number,
                recipe,
                makefile_path=makefile_path,
                repo_root=repo_root,
                parsed=parsed,
                pending=pending,
                files=files,
                findings=findings,
                inspected_package_scripts=inspected_package_scripts,
            )
    return FormalMakeInspection(files=tuple(sorted(files)), findings=tuple(sorted(findings)), targets=tuple(sorted(selected)))


def _protected_make_assignments(logical_lines: tuple[tuple[int, str], ...]) -> Mapping[str, list[tuple[int, str]]]:
    assignments: dict[str, list[tuple[int, str]]] = {}
    for line_number, line in logical_lines:
        uncommented = _strip_make_comment(line)
        assignment = MAKE_VARIABLE_ASSIGNMENT.match(uncommented)
        if assignment is not None and assignment.group("name") in PROTECTED_MAKE_VARIABLES:
            assignments.setdefault(assignment.group("name"), []).append((line_number, uncommented))
    return assignments


def _formal_make_binding_findings(
    assignments: Mapping[str, list[tuple[int, str]]],
    *,
    makefile_path: str,
) -> set[Finding]:
    findings: set[Finding] = set()
    for name, expected in CANONICAL_FORMAL_MAKE_BINDINGS.items():
        definitions = assignments.get(name, [])
        if len(definitions) != 1 or _normalized_make_source(definitions[0][1]) != _normalized_make_source(expected):
            line_number = definitions[-1][0] if definitions else 1
            findings.add(
                Finding(
                    makefile_path,
                    line_number,
                    f"formal Make binding {name} does not match the canonical command contract",
                )
            )
    forbidden_assignments = set(assignments) - set(CANONICAL_FORMAL_MAKE_BINDINGS)
    findings.update(
        Finding(
            makefile_path,
            definitions[-1][0],
            f"formal Make binding {name} may not be defined by the Makefile",
        )
        for name in forbidden_assignments
        for definitions in (assignments[name],)
    )
    return findings


def _public_formal_target_findings(
    target: str,
    expected_dispatch: str,
    *,
    parsed: ParsedMakefile,
    makefile_path: str,
) -> set[Finding]:
    findings: set[Finding] = set()
    actual_recipes = tuple(
        _normalized_make_source(_strip_shell_comment(recipe)) for _line_number, recipe in parsed.rules.get(target, MakeRule(set(), [])).recipes
    )
    if actual_recipes != (_normalized_make_source(expected_dispatch),):
        findings.add(
            Finding(
                f"{makefile_path}#{target}",
                1,
                "public formal Make target does not use the canonical acceptance dispatch",
            )
        )
    rule = parsed.rules.get(target, MakeRule(set(), []))
    expected_prerequisites = CANONICAL_FORMAL_PREREQUISITES.get(target)
    if expected_prerequisites is None or rule.prerequisites != expected_prerequisites:
        findings.add(
            Finding(
                f"{makefile_path}#{target}",
                1,
                "public formal Make prerequisites do not match the audited action manifest",
            )
        )
    for prerequisite in expected_prerequisites or ():
        prerequisite_rule = parsed.rules.get(prerequisite, MakeRule(set(), []))
        prerequisite_recipes = "\n".join(_normalized_make_source(_strip_shell_comment(recipe)) for _line_number, recipe in prerequisite_rule.recipes)
        expected_digest = CANONICAL_AUXILIARY_RECIPE_SHA256.get(prerequisite)
        if prerequisite_rule.prerequisites or hashlib.sha256(prerequisite_recipes.encode()).hexdigest() != expected_digest:
            findings.add(
                Finding(
                    f"{makefile_path}#{prerequisite}",
                    1,
                    "formal Make preflight does not match the audited action manifest",
                )
            )
    return findings


def _private_formal_target_findings(
    target: str,
    *,
    parsed: ParsedMakefile,
    makefile_path: str,
) -> set[Finding]:
    recipes = tuple(_normalized_make_source(_strip_shell_comment(recipe)) for _line_number, recipe in parsed.rules.get(target, MakeRule(set(), [])).recipes)
    findings: set[Finding] = set()
    gate_count = sum(recipe == "@$(REQUIRE_CONTAINER_ACCEPTANCE)" for recipe in recipes)
    if gate_count != 1:
        findings.add(
            Finding(
                f"{makefile_path}#{target}",
                1,
                "private formal Make target does not execute the canonical context gate",
            )
        )
    expected_digest = CANONICAL_PRIVATE_RECIPE_SHA256.get(target)
    actual_digest = hashlib.sha256("\n".join(recipes).encode()).hexdigest()
    if expected_digest is None or actual_digest != expected_digest:
        findings.add(
            Finding(
                f"{makefile_path}#{target}",
                1,
                "private formal Make recipes do not match the audited action manifest",
            )
        )
    return findings


def _formal_make_manifest_findings(makefile_path: str) -> set[Finding]:
    findings: set[Finding] = set()
    expected_private_targets = {target for target in REQUIRED_FORMAL_TARGET_FILES if target.startswith("_")}
    if set(CANONICAL_PRIVATE_RECIPE_SHA256) != expected_private_targets:
        findings.add(Finding(makefile_path, 1, "private formal Make recipe manifest is incomplete or stale"))
    if set(CANONICAL_FORMAL_PREREQUISITES) != set(CANONICAL_FORMAL_DISPATCHES) or set(CANONICAL_AUXILIARY_RECIPE_SHA256) != set().union(
        *CANONICAL_FORMAL_PREREQUISITES.values()
    ):
        findings.add(Finding(makefile_path, 1, "public formal Make prerequisite manifest is incomplete or stale"))
    if set(FORMAL_TARGET_ENTRYPOINT_MARKERS) != set(REQUIRED_FORMAL_TARGET_FILES):
        findings.add(Finding(makefile_path, 1, "formal Make entrypoint marker manifest is incomplete or stale"))
    return findings


def _formal_make_contract_findings(
    *,
    parsed: ParsedMakefile,
    logical_lines: tuple[tuple[int, str], ...],
    targets: tuple[str, ...],
    makefile_path: str,
) -> set[Finding]:
    if not any(target in REQUIRED_FORMAL_TARGET_FILES for target in targets):
        return set()
    assignments = _protected_make_assignments(logical_lines)
    findings = _formal_make_binding_findings(assignments, makefile_path=makefile_path)
    for target in targets:
        expected_dispatch = CANONICAL_FORMAL_DISPATCHES.get(target)
        if expected_dispatch is not None:
            findings.update(
                _public_formal_target_findings(
                    target,
                    expected_dispatch,
                    parsed=parsed,
                    makefile_path=makefile_path,
                )
            )
        elif target.startswith("_"):
            findings.update(_private_formal_target_findings(target, parsed=parsed, makefile_path=makefile_path))
    findings.update(_formal_make_manifest_findings(makefile_path))
    return findings


def _makefile_dynamic_findings(
    *,
    relative: Path,
    logical_lines: tuple[tuple[int, str], ...],
    parsed: ParsedMakefile,
    targets: tuple[str, ...],
) -> set[Finding]:
    findings = {
        Finding(relative.as_posix(), line_number, rule) for line_number, line in logical_lines for pattern, rule in DYNAMIC_MAKE_RULES if pattern.search(line)
    }
    findings.update(
        _formal_make_contract_findings(
            parsed=parsed,
            logical_lines=logical_lines,
            targets=targets,
            makefile_path=relative.as_posix(),
        )
    )
    findings.update(
        Finding(relative.as_posix(), line_number, "unaudited Make shell function")
        for line_number, line in logical_lines
        if "$(shell" in line and line not in AUDITED_MAKE_SHELL_LINES
    )
    return findings


def _formal_target_action_findings(
    target: str,
    *,
    relative: Path,
    resolved_root: Path,
    parsed: ParsedMakefile,
) -> set[Finding]:
    expected_files = REQUIRED_FORMAL_TARGET_FILES.get(target)
    if expected_files is None:
        return set()
    target_inspection = _walk_make_targets(
        (target,),
        makefile_path=relative.as_posix(),
        repo_root=resolved_root,
        parsed=parsed,
    )
    findings: set[Finding] = set()
    actual_files = frozenset(path.relative_to(resolved_root).as_posix() for path in target_inspection.files)
    if actual_files != expected_files:
        missing = ",".join(sorted(expected_files - actual_files)) or "none"
        unexpected = ",".join(sorted(actual_files - expected_files)) or "none"
        findings.add(
            Finding(
                f"{relative.as_posix()}#{target}",
                1,
                f"formal Make action closure mismatch: missing={missing}; unexpected={unexpected}",
            )
        )
    recipe_text = "\n".join(recipe for selected_target in target_inspection.targets for _line, recipe in parsed.rules[selected_target].recipes)
    required_marker = FORMAL_TARGET_ENTRYPOINT_MARKERS.get(target)
    if required_marker is None:
        return findings | {Finding(f"{relative.as_posix()}#{target}", 1, "formal Make action has no audited entrypoint marker")}
    if required_marker not in recipe_text:
        findings.add(
            Finding(
                f"{relative.as_posix()}#{target}",
                1,
                f"formal Make action is missing required {required_marker} gate",
            )
        )
    if target == "_ui-smoke" and "AGENTGOV_ACCEPTANCE_CURL" not in recipe_text:
        findings.add(Finding(f"{relative.as_posix()}#{target}", 1, "formal UI smoke is missing bound curl probe"))
    return findings


def _duplicate_formal_target_findings(
    logical_lines: tuple[tuple[int, str], ...],
    *,
    relative: Path,
    inspected_targets: tuple[str, ...],
) -> set[Finding]:
    definition_lines: dict[str, list[int]] = {}
    for line_number, line in logical_lines:
        match = MAKE_TARGET_PATTERN.match(line) if line and not line[0].isspace() else None
        if match is None:
            continue
        for target in match.group(1).split():
            definition_lines.setdefault(target, []).append(line_number)
    return {
        Finding(
            f"{relative.as_posix()}#{target}",
            lines_for_target[1],
            "formal Make target has duplicate or overriding rule definitions",
        )
        for target, lines_for_target in definition_lines.items()
        if target in inspected_targets and len(lines_for_target) > 1
    }


def inspect_make_targets(
    makefile: Path,
    targets: tuple[str, ...],
    *,
    repo_root: Path = REPO_ROOT,
) -> FormalMakeInspection:
    resolved_root = repo_root.resolve()
    resolved_makefile = (makefile if makefile.is_absolute() else resolved_root / makefile).resolve()
    try:
        relative = resolved_makefile.relative_to(resolved_root)
    except ValueError:
        finding = Finding(makefile.as_posix(), 1, "formal Makefile must stay inside repository")
        return FormalMakeInspection(files=(), findings=(finding,), targets=())
    try:
        lines = resolved_makefile.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        finding = Finding(relative.as_posix(), 1, f"cannot inspect Make targets: {exc}")
        return FormalMakeInspection(files=(), findings=(finding,), targets=())
    parsed = _parse_makefile(lines)
    inspection = _walk_make_targets(
        tuple(dict.fromkeys(targets)),
        makefile_path=relative.as_posix(),
        repo_root=resolved_root,
        parsed=parsed,
    )
    logical_lines = _logical_make_lines(lines)
    dynamic_findings = _makefile_dynamic_findings(
        relative=relative,
        logical_lines=logical_lines,
        parsed=parsed,
        targets=tuple(dict.fromkeys(targets)),
    )
    for target in targets:
        dynamic_findings.update(
            _formal_target_action_findings(
                target,
                relative=relative,
                resolved_root=resolved_root,
                parsed=parsed,
            )
        )
    dynamic_findings.update(
        _duplicate_formal_target_findings(
            logical_lines,
            relative=relative,
            inspected_targets=inspection.targets,
        )
    )
    return FormalMakeInspection(
        files=inspection.files,
        findings=tuple(sorted({*inspection.findings, *dynamic_findings})),
        targets=inspection.targets,
    )


def scan_make_targets(
    makefile: Path,
    targets: tuple[str, ...],
    *,
    repo_root: Path = REPO_ROOT,
) -> tuple[Finding, ...]:
    return inspect_make_targets(makefile, targets, repo_root=repo_root).findings
