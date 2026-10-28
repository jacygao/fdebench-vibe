"""Strict, typed JSON parsing shared by extraction and workflow tools."""

import json
import math
from typing import NoReturn

from pydantic import JsonValue, RootModel


class JsonDocument(RootModel[JsonValue]):
    """Typed boundary around the standard library's untyped JSON decoder."""


def parse_json(content: str) -> JsonValue:
    """Refuse duplicate keys, non-finite numbers and overflowing float exponents."""
    return JsonDocument.model_validate(
        json.loads(
            content,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
    ).root


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError("Non-finite JSON numbers are invalid.")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON numbers are invalid.")
    return number


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are invalid.")
        result[key] = value
    return result
