"""Generic, baseline-free OpenAPI request-input documentation audit."""

from __future__ import annotations

import re
from collections.abc import Mapping

OpenApiObject = dict[str, object]
HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})
RESPONSES_PATH = "/v1/responses"
_PATH_TOKEN = re.compile(r"{([^{}]+)}")


def audit_request_input_documentation(schema: OpenApiObject) -> list[str]:
    """Require useful documentation and valid examples for every live input."""

    issues: list[str] = []
    components = _component_schemas(schema)
    paths = _mapping(schema.get("paths"))
    reachable: set[str] = set()
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, Mapping):
                continue
            issues.extend(_audit_parameters(path, method, operation, components))
            request_body = operation.get("requestBody")
            if not isinstance(request_body, Mapping):
                continue
            prefix = f"{method.upper()} {path} requestBody"
            if not _meaningful(request_body.get("description")):
                issues.append(f"{prefix} missing description")
            content = request_body.get("content")
            if not isinstance(content, Mapping) or not content:
                issues.append(f"{prefix} has no media type schema")
                continue
            for media_type, media in content.items():
                if not isinstance(media, Mapping):
                    issues.append(f"{prefix} media {media_type} is not an object")
                    continue
                body_schema = media.get("schema")
                if not isinstance(body_schema, Mapping):
                    issues.append(f"{prefix} media {media_type} missing schema")
                    continue
                reachable.update(_references(body_schema))
                if "$ref" not in body_schema:
                    issues.extend(_audit_inline_properties(body_schema, f"{prefix} {media_type}", components))

    queue = list(reachable)
    while queue:
        name = queue.pop()
        component = components.get(name)
        if not isinstance(component, Mapping):
            issues.append(f"request component {name} is missing")
            continue
        nested = _references(component) - reachable
        reachable.update(nested)
        queue.extend(nested)

    for name in sorted(reachable):
        component = components.get(name)
        if not isinstance(component, Mapping):
            continue
        if not _meaningful(component.get("description")):
            issues.append(f"request component {name} missing description")
        properties = component.get("properties")
        if not isinstance(properties, Mapping):
            continue
        for field_name, field_schema in properties.items():
            if not isinstance(field_name, str) or not isinstance(field_schema, Mapping):
                continue
            issues.extend(
                _audit_documented_example(
                    field_schema,
                    f"request component {name}.{field_name}",
                    components,
                )
            )

    issues.extend(_audit_responses_guide(schema, components))
    return issues


def _audit_parameters(
    path: str,
    method: str,
    operation: Mapping[str, object],
    components: Mapping[str, object],
) -> list[str]:
    issues: list[str] = []
    parameters = operation.get("parameters", [])
    if not isinstance(parameters, list):
        return [f"{method.upper()} {path} parameters is not an array"]
    declared_path_names: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, Mapping):
            issues.append(f"{method.upper()} {path} contains a non-object parameter")
            continue
        name = parameter.get("name")
        location = parameter.get("in")
        label = f"{method.upper()} {path} parameter {location}:{name}"
        if not isinstance(name, str) or not name:
            issues.append(f"{label} missing name")
            continue
        if location == "path":
            declared_path_names.add(name)
            if parameter.get("required") is not True:
                issues.append(f"{label} must be required")
        if not _meaningful(parameter.get("description")):
            issues.append(f"{label} missing description")
        examples = _examples(parameter)
        if not examples:
            issues.append(f"{label} missing example")
            continue
        parameter_schema = parameter.get("schema")
        if not isinstance(parameter_schema, Mapping):
            issues.append(f"{label} missing schema")
            continue
        for index, example in enumerate(examples):
            issues.extend(_example_issues(example, parameter_schema, components, f"{label} example[{index}]"))

    path_names = set(_PATH_TOKEN.findall(path))
    if path_names != declared_path_names:
        issues.append(f"{method.upper()} {path} path parameter names {sorted(declared_path_names)} do not match template names {sorted(path_names)}")
    return issues


def _audit_inline_properties(
    fragment: Mapping[str, object],
    label: str,
    components: Mapping[str, object],
) -> list[str]:
    issues: list[str] = []
    properties = fragment.get("properties")
    if not isinstance(properties, Mapping):
        return issues
    for field_name, field_schema in properties.items():
        if isinstance(field_name, str) and isinstance(field_schema, Mapping):
            issues.extend(_audit_documented_example(field_schema, f"{label}.{field_name}", components))
    return issues


def _audit_documented_example(
    field_schema: Mapping[str, object],
    label: str,
    components: Mapping[str, object],
) -> list[str]:
    issues: list[str] = []
    if not _meaningful(field_schema.get("description")):
        issues.append(f"{label} missing description")
    examples = _examples(field_schema)
    if not examples:
        issues.append(f"{label} missing example")
        return issues
    for index, example in enumerate(examples):
        issues.extend(_example_issues(example, field_schema, components, f"{label} example[{index}]"))
    return issues


def _example_issues(
    value: object,
    fragment: Mapping[str, object],
    components: Mapping[str, object],
    label: str,
) -> list[str]:
    placeholder = _placeholder_reason(value)
    if placeholder:
        return [f"{label}: {placeholder}"]
    valid, reason = _matches_schema(value, fragment, components, frozenset())
    return [] if valid else [f"{label} violates schema: {reason}"]


def _matches_schema(
    value: object,
    fragment: Mapping[str, object],
    components: Mapping[str, object],
    stack: frozenset[str],
) -> tuple[bool, str]:
    reference = fragment.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
        name = reference.rsplit("/", 1)[-1]
        if name in stack:
            return True, ""
        target = components.get(name)
        if not isinstance(target, Mapping):
            return False, f"missing reference {name}"
        return _matches_schema(value, target, components, stack | {name})

    all_of = fragment.get("allOf")
    if isinstance(all_of, list):
        for child in all_of:
            if isinstance(child, Mapping):
                valid, reason = _matches_schema(value, child, components, stack)
                if not valid:
                    return False, reason
    for keyword in ("anyOf", "oneOf"):
        children = fragment.get(keyword)
        if isinstance(children, list):
            reasons: list[str] = []
            for child in children:
                if isinstance(child, Mapping):
                    valid, reason = _matches_schema(value, child, components, stack)
                    if valid:
                        break
                    reasons.append(reason)
            else:
                return False, f"matches no {keyword} branch ({'; '.join(reasons)})"

    if "const" in fragment and value != fragment["const"]:
        return False, f"{value!r} != const {fragment['const']!r}"
    enum = fragment.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False, f"{value!r} not in enum {enum!r}"
    return _matches_typed_value(value, fragment, components, stack)


def _matches_typed_value(
    value: object,
    fragment: Mapping[str, object],
    components: Mapping[str, object],
    stack: frozenset[str],
) -> tuple[bool, str]:
    expected_type = fragment.get("type")
    if expected_type == "null":
        return (value is None, "expected null")
    if expected_type == "string":
        if not isinstance(value, str):
            return False, "expected string"
        minimum = fragment.get("minLength")
        maximum = fragment.get("maxLength")
        pattern = fragment.get("pattern")
        if isinstance(minimum, int) and len(value) < minimum:
            return False, f"string shorter than minLength {minimum}"
        if isinstance(maximum, int) and len(value) > maximum:
            return False, f"string longer than maxLength {maximum}"
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            return False, f"string does not match pattern {pattern!r}"
    elif expected_type == "boolean" and not isinstance(value, bool):
        return False, "expected boolean"
    elif expected_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return False, "expected integer"
    elif expected_type == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        return False, "expected number"
    elif expected_type == "array":
        if not isinstance(value, list):
            return False, "expected array"
        minimum = fragment.get("minItems")
        maximum = fragment.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            return False, f"array shorter than minItems {minimum}"
        if isinstance(maximum, int) and len(value) > maximum:
            return False, f"array longer than maxItems {maximum}"
        item_schema = fragment.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                valid, reason = _matches_schema(item, item_schema, components, stack)
                if not valid:
                    return False, f"item {index}: {reason}"
    elif expected_type == "object":
        if not isinstance(value, dict):
            return False, "expected object"
        required = fragment.get("required")
        if isinstance(required, list):
            missing = [name for name in required if name not in value]
            if missing:
                return False, f"missing required properties {missing}"
        properties = fragment.get("properties")
        if isinstance(properties, Mapping):
            for name, item in value.items():
                child = properties.get(name)
                if isinstance(child, Mapping):
                    valid, reason = _matches_schema(item, child, components, stack)
                    if not valid:
                        return False, f"property {name}: {reason}"

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for keyword, predicate in (
            ("minimum", lambda actual, bound: actual >= bound),
            ("maximum", lambda actual, bound: actual <= bound),
            ("exclusiveMinimum", lambda actual, bound: actual > bound),
            ("exclusiveMaximum", lambda actual, bound: actual < bound),
        ):
            bound = fragment.get(keyword)
            if isinstance(bound, (int, float)) and not predicate(value, bound):
                return False, f"{value} violates {keyword} {bound}"
    return True, ""


def _audit_responses_guide(
    schema: OpenApiObject,
    components: Mapping[str, object],
) -> list[str]:
    issues: list[str] = []
    operation = _mapping(_mapping(_mapping(schema.get("paths")).get(RESPONSES_PATH)).get("post"))
    description = operation.get("description")
    wording = description if isinstance(description, str) else ""
    if "Parameters → No parameters" not in wording or "Request-body field guide" not in wording:
        issues.append("POST /v1/responses missing Swagger no-parameters explanation or flattened request field guide")
    expected_paths = _flattened_field_paths(_mapping(components.get("ResponsesRequest")), components)
    if len(expected_paths) != 22:
        issues.append(f"POST /v1/responses request field graph has {len(expected_paths)} fields instead of reviewed 22")
    for path in expected_paths:
        if f"`{path}`" not in wording:
            issues.append(f"POST /v1/responses flattened request guide missing {path}")
    return issues


def _flattened_field_paths(
    root: Mapping[str, object],
    components: Mapping[str, object],
) -> list[str]:
    paths: list[str] = []

    def walk(fragment: Mapping[str, object], prefix: str, stack: frozenset[str]) -> None:
        properties = fragment.get("properties")
        if not isinstance(properties, Mapping):
            return
        for name, field_schema in properties.items():
            if not isinstance(name, str) or not isinstance(field_schema, Mapping):
                continue
            path = f"{prefix}.{name}" if prefix else name
            paths.append(path)
            for reference, array_item in _nested_references(field_schema):
                if reference in stack:
                    continue
                nested = components.get(reference)
                if isinstance(nested, Mapping):
                    walk(nested, f"{path}[]" if array_item else path, stack | {reference})

    walk(root, "", frozenset({"ResponsesRequest"}))
    return paths


def _nested_references(fragment: object, *, array_item: bool = False) -> list[tuple[str, bool]]:
    found: list[tuple[str, bool]] = []
    if isinstance(fragment, Mapping):
        reference = fragment.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            found.append((reference.rsplit("/", 1)[-1], array_item))
        for key, child in fragment.items():
            found.extend(_nested_references(child, array_item=array_item or key == "items"))
    elif isinstance(fragment, list):
        for child in fragment:
            found.extend(_nested_references(child, array_item=array_item))
    return list(dict.fromkeys(found))


def _references(fragment: object) -> set[str]:
    return {reference for reference, _ in _nested_references(fragment)}


def _examples(fragment: Mapping[str, object]) -> list[object]:
    examples = fragment.get("examples")
    if isinstance(examples, list):
        return examples
    if isinstance(examples, Mapping):
        values: list[object] = []
        for example in examples.values():
            if isinstance(example, Mapping) and "value" in example:
                values.append(example["value"])
        return values
    return [fragment["example"]] if "example" in fragment else []


def _placeholder_reason(value: object) -> str | None:
    if value is None:
        return "null placeholder is forbidden; document a concrete non-null value"
    if isinstance(value, str) and value.strip().lower() == "string":
        return "generic string placeholder is forbidden"
    if isinstance(value, list):
        for item in value:
            reason = _placeholder_reason(item)
            if reason:
                return reason
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).startswith("additionalProp") and str(key)[14:].isdigit():
                return "generated additionalProp placeholder is forbidden"
            reason = _placeholder_reason(item)
            if reason:
                return reason
    return None


def _component_schemas(schema: OpenApiObject) -> Mapping[str, object]:
    return _mapping(_mapping(schema.get("components")).get("schemas"))


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _meaningful(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
