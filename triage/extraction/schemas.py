"""Bound and validate caller schemas with local-only reference resolution."""

import json
import math
from dataclasses import dataclass

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from pydantic import JsonValue, ValidationError

from triage.errors import TriageError
from triage.json_values import parse_json

type Schema = dict[str, JsonValue] | bool

SCHEMA_DRAFTS = {
    "http://json-schema.org/draft-07/schema#",
    "https://json-schema.org/draft-07/schema",
    "https://json-schema.org/draft/2019-09/schema",
    "https://json-schema.org/draft/2020-12/schema",
}
SINGLE_SCHEMAS = {
    "items",
    "additionalItems",
    "additionalProperties",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "unevaluatedProperties",
    "unevaluatedItems",
}
SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}


@dataclass(frozen=True)
class SchemaPlan:
    schema: dict[str, JsonValue]
    validator: Validator

    def valid(self, value: JsonValue, schema: Schema) -> bool:
        """Validate one field using the same draft and format policy as its document."""
        return bool(self.validator.evolve(schema=schema).is_valid(value))


def compile_schema(value: str | dict[str, JsonValue] | None) -> SchemaPlan:
    """Bound caller schemas and permit null for unavailable document fields."""
    try:
        if isinstance(value, str) and len(value) > 100_000:
            raise ValueError("Schema exceeds 100,000 characters.")
        if value is None or (isinstance(value, str) and not value.strip()):
            raw: JsonValue = {"type": "object", "additionalProperties": True}
        elif isinstance(value, str):
            raw = parse_json(value)
        else:
            raw = value
        if not isinstance(raw, dict):
            raise ValueError("Schema must describe an object.")
        _bound_json(raw)
        if len(json.dumps(raw, ensure_ascii=False)) > 100_000:
            raise ValueError("Schema exceeds 100,000 characters.")
        draft = raw.get("$schema")
        if draft is not None and (not isinstance(draft, str) or draft not in SCHEMA_DRAFTS):
            raise ValueError("Use JSON Schema draft-07, 2019-09 or 2020-12.")
        validator_type = validator_for(raw)
        validator_type.check_schema(raw)
        expanded = _expand(raw, raw, frozenset(), [10_000])
        _bound_json(expanded)
        if not isinstance(expanded, dict) or expanded.get("type") != "object":
            raise ValueError("The root schema must have type object.")
        plan = SchemaPlan(expanded, validator_type(expanded, format_checker=FormatChecker()))
        _allow_null_fields(expanded, plan, root=True)
        return plan
    except (ValueError, SchemaError, ValidationError, RecursionError, KeyError) as exc:
        raise TriageError(
            "INVALID_EXTRACTION_SCHEMA",
            "Use a bounded object schema with local non-recursive references and simple "
            "bounded patterns, or omit json_schema for schema-free extraction.",
            422,
        ) from exc


def _bound_json(value: JsonValue) -> None:
    stack = [(value, 0)]
    count = 0
    characters = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 24 or count > 10_000:
            raise ValueError("Schema is too complex.")
        if isinstance(item, dict):
            characters += sum(len(key) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            characters += len(item)
            if len(item) > 20_000:
                raise ValueError("Schema string is too long.")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Schema numbers must be finite.")
        if characters > 100_000:
            raise ValueError("Schema text budget exceeded after expansion.")


def _expand(
    schema: JsonValue, root: dict[str, JsonValue], refs: frozenset[str], budget: list[int]
) -> JsonValue:
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("Expanded schema is too complex.")
    if not isinstance(schema, dict):
        return schema
    if "$dynamicRef" in schema or "$recursiveRef" in schema or "$id" in schema:
        raise ValueError("Dynamic references and schema IDs are unsupported.")
    if "pattern" in schema and isinstance(schema["pattern"], str):
        _check_pattern(schema["pattern"])
    patterns = schema.get("patternProperties")
    if isinstance(patterns, dict):
        for pattern in patterns:
            _check_pattern(pattern)
    result: dict[str, JsonValue] = {}
    referenced: JsonValue = None
    for key, child in schema.items():
        if key == "$ref":
            if not isinstance(child, str) or not child.startswith("#/") or child in refs:
                raise ValueError("Only acyclic local JSON Pointer references are supported.")
            target: JsonValue = root
            for segment in child[2:].split("/"):
                if not isinstance(target, dict):
                    raise ValueError("Invalid local schema reference.")
                target = target[segment.replace("~1", "/").replace("~0", "~")]
            if not isinstance(target, (dict, bool)):
                raise ValueError("Reference must identify a schema.")
            referenced = _expand(target, root, refs | {child}, budget)
            if len(schema) == 1:
                return referenced
        elif key in SCHEMA_MAPS and isinstance(child, dict):
            result[key] = {name: _expand(sub, root, refs, budget) for name, sub in child.items()}
        elif key in SCHEMA_LISTS and isinstance(child, list):
            result[key] = [_expand(sub, root, refs, budget) for sub in child]
        elif key in SINGLE_SCHEMAS:
            if isinstance(child, list):
                result[key] = [_expand(sub, root, refs, budget) for sub in child]
            else:
                result[key] = _expand(child, root, refs, budget)
        elif key == "dependencies":
            raise ValueError("Use dependentSchemas/dependentRequired rather than dependencies.")
        else:
            result[key] = child
    if referenced is not None:
        branches = result.get("allOf", [])
        if isinstance(branches, list):
            result["allOf"] = [referenced, *branches]
    return result


def _check_pattern(pattern: str) -> None:
    if len(pattern) > 256:
        raise ValueError("Pattern is too long.")
    variable = 0
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif not in_class:
            if char in "()|":
                raise ValueError("Pattern groups and alternation are unsupported.")
            if char in "*+?":
                variable += 1
            if char == "{":
                end = pattern.find("}", index)
                if end != -1:
                    bounds = pattern[index + 1 : end].split(",")
                    if any(part.isdigit() and int(part) > 10_000 for part in bounds):
                        raise ValueError("Pattern repetition is too large.")
                    variable += int(len(bounds) > 1 and bounds[0] != bounds[-1])
                    index = end
        index += 1
    if variable > 1 or (variable and not pattern.startswith("^")):
        raise ValueError("Use simple anchored patterns with at most one variable repetition.")


def _allow_null_fields(schema: JsonValue, plan: SchemaPlan, *, root: bool = False) -> None:
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            if isinstance(child, (dict, bool)):
                _allow_null_fields(child, plan)
                if not (root and name == "document_id") and not plan.valid(None, child):
                    properties[name] = {"anyOf": [child, {"type": "null"}]}
    for keyword in SCHEMA_LISTS:
        children = schema.get(keyword)
        if isinstance(children, list):
            for child in children:
                _allow_null_fields(child, plan, root=root)
    for keyword in SINGLE_SCHEMAS:
        children = schema.get(keyword)
        if isinstance(children, list):
            for child in children:
                _allow_null_fields(child, plan)
        else:
            _allow_null_fields(children, plan)
    for keyword in SCHEMA_MAPS - {"properties"}:
        children = schema.get(keyword)
        if isinstance(children, dict):
            for child in children.values():
                _allow_null_fields(child, plan)
