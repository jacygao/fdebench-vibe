"""Conservative field normalization with nullable, isolated failure handling."""

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import JsonValue

from triage.errors import TriageError
from triage.extraction.schemas import Schema, SchemaPlan
from triage.json_values import parse_json


def normalize_record(content: str, plan: SchemaPlan, document_id: str) -> dict[str, JsonValue]:
    """Keep good fields, fill requested keys, discard extras and validate the final record."""
    try:
        raw = parse_json(content)
    except (ValueError, RecursionError) as exc:
        raise TriageError(
            "MODEL_INVALID_RESPONSE", "Vision output must be a JSON object.", 502
        ) from exc
    if not isinstance(raw, dict):
        raise TriageError("MODEL_INVALID_RESPONSE", "Vision output must be a JSON object.", 502)
    raw["document_id"] = document_id
    normalized = _normalize(raw, plan.schema, plan, root=True)
    if not isinstance(normalized, dict):
        raise TriageError(
            "EXTRACTION_SCHEMA_CONFLICT",
            "The normalized record cannot satisfy the supplied schema.",
            502,
        )
    if not plan.validator.is_valid(normalized):
        raise TriageError(
            "EXTRACTION_SCHEMA_CONFLICT",
            "The normalized record cannot satisfy the supplied schema.",
            502,
        )
    normalized["document_id"] = document_id
    return normalized


def null_fields(value: JsonValue, path: str = "") -> list[str]:
    """Return JSON Pointer paths to null fields without exposing their values."""
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [
            field
            for key, child in value.items()
            for field in null_fields(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        ]
    if isinstance(value, list):
        return [
            field
            for index, child in enumerate(value)
            for field in null_fields(child, f"{path}/{index}")
        ]
    return []


def _properties(schema: Schema) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            result.update(properties)
        branches = schema.get("allOf")
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, (dict, bool)):
                    result.update(_properties(branch))
    return result


def _shape(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    branches = schema.get("allOf")
    if isinstance(branches, list):
        for branch in branches:
            if isinstance(branch, dict):
                result.update(_shape(branch))
    result.update(schema)
    result["properties"] = _properties(schema)
    return result


def _normalize(
    value: JsonValue, schema: Schema, plan: SchemaPlan, *, root: bool = False
) -> JsonValue:
    if value is None or schema is False:
        return None
    if schema is True:
        return value
    original_schema = schema
    schema = _shape(schema)
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            candidates = [branch for branch in branches if isinstance(branch, (dict, bool))]
            candidates.sort(key=lambda branch: not plan.valid(value, branch))
            for branch in candidates:
                if isinstance(branch, (dict, bool)):
                    candidate = _normalize(value, branch, plan, root=root)
                    if candidate is not None and plan.valid(candidate, original_schema):
                        return candidate
            return None
    types = schema.get("type")
    allowed = types if isinstance(types, list) else [types]
    normalized: JsonValue = value
    if isinstance(value, dict) and ("object" in allowed or _properties(schema) or root):
        properties = _properties(schema)
        result: dict[str, JsonValue] = {}
        for name, child in properties.items():
            if isinstance(child, (dict, bool)):
                result[name] = (
                    value.get(name)
                    if root and name == "document_id"
                    else _normalize(value.get(name), child, plan)
                )
        additional = schema.get("additionalProperties", False)
        patterns = schema.get("patternProperties")
        for name in value.keys() - properties.keys():
            matching: list[JsonValue] = [
                child
                for pattern, child in (patterns.items() if isinstance(patterns, dict) else [])
                if isinstance(child, (dict, bool)) and re.search(pattern, name)
            ]
            if matching:
                result[name] = _normalize(value[name], {"allOf": matching}, plan)
            elif isinstance(additional, dict):
                result[name] = _normalize(value[name], additional, plan)
            elif additional is True:
                result[name] = value[name]
        normalized = result
    elif isinstance(value, list) and ("array" in allowed or "items" in schema):
        items = schema.get("items", True)
        prefix = schema.get("prefixItems", items if isinstance(items, list) else [])
        normalized = []
        for index, item in enumerate(value):
            child = prefix[index] if isinstance(prefix, list) and index < len(prefix) else items
            normalized.append(
                _normalize(item, child if isinstance(child, (dict, bool)) else True, plan)
            )
    elif plan.valid(value, original_schema):
        normalized = (
            _text(value, schema) if isinstance(value, str) and "string" in allowed else value
        )
    elif "integer" in allowed or "number" in allowed:
        normalized = _number(value, integer="integer" in allowed and "number" not in allowed)
    elif "boolean" in allowed:
        normalized = _boolean(value)
    elif "string" in allowed and isinstance(value, str):
        normalized = _text(value, schema)
    if root and isinstance(normalized, dict):
        return normalized
    return normalized if plan.valid(normalized, original_schema) else None


def _number(value: JsonValue, *, integer: bool) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if integer:
            return int(value) if value == int(value) else None
        return value
    text = str(value).strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()
    text = re.sub(r"^([+-]?)\s*[$€£¥]\s*", r"\1", text)
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        return None
    try:
        number = Decimal(text.replace(",", ""))
        if not number.is_finite() or (integer and number != number.to_integral_value()):
            return None
        if integer:
            return int(number)
        result = float(number)
        return result if math.isfinite(result) else None
    except (InvalidOperation, OverflowError):
        return None


def _boolean(value: JsonValue) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "checked", "[x]"}:
            return True
        if text in {"false", "no", "unchecked", "[ ]"}:
            return False
    return None


def _text(value: str, schema: dict[str, JsonValue]) -> str | None:
    description = str(schema.get("description", ""))
    if re.search(r"\bas (?:it )?appears\b|\bverbatim\b", description, re.IGNORECASE):
        return value
    target: str | None = None
    if schema.get("format") == "date" or "YYYY-MM-DD" in description:
        target = "%Y-%m-%d"
    elif "MM/DD/YYYY" in description:
        target = "%m/%d/%Y"
    elif "DD/MM/YYYY" in description:
        target = "%d/%m/%Y"
    if target is None:
        return value
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        parsed = None
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
            try:
                parsed = datetime.strptime(text, pattern).date()
                break
            except ValueError:
                continue
        numeric = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if parsed is None and numeric:
            first, second, year = (int(part) for part in numeric.groups())
            if first > 12 >= second:
                month, day = second, first
            elif second > 12 >= first:
                month, day = first, second
            elif 1 <= first <= 12 and 1 <= second <= 12:
                return value if target in {"%m/%d/%Y", "%d/%m/%Y"} else None
            else:
                return None
            try:
                parsed = date(year, month, day)
            except ValueError:
                return None
    return parsed.strftime(target) if parsed is not None else None
