"""Deterministic parameter, dependency and constraint checks."""

import json

from pydantic import JsonValue

from triage.errors import TriageError
from triage.orchestration.models import (
    Condition,
    ConstraintPolicy,
    ProposedCall,
    Requirement,
    StepResult,
    Tool,
)


def rejected(message: str) -> TriageError:
    """Create a safe planner rejection without copying customer values."""
    return TriageError("WORKFLOW_PLAN_REJECTED", message, 502)


def pointer_value(value: JsonValue, pointer: str) -> tuple[bool, JsonValue]:
    """Resolve a bounded JSON Pointer, distinguishing missing values from null."""
    if pointer == "":
        return True, value
    if not pointer.startswith("/") or len(pointer) > 2000:
        return False, None
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isascii() and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return False, None
    return True, value


def matches(value: JsonValue, condition: Condition) -> bool:
    """Evaluate only typed comparisons; absent data never passes a negative check."""
    found, actual = pointer_value(value, condition.pointer)
    expected = condition.value
    if condition.operator == "exists":
        return found and actual is not None
    if not found:
        return False
    if condition.operator == "eq":
        return type(actual) is type(expected) and actual == expected
    if condition.operator == "ne":
        return not (type(actual) is type(expected) and actual == expected)
    if condition.operator in {"in", "not_in"}:
        if not isinstance(expected, list):
            return False
        member = any(type(actual) is type(item) and actual == item for item in expected)
        return member if condition.operator == "in" else not member
    if (
        isinstance(actual, bool)
        or isinstance(expected, bool)
        or not isinstance(actual, (int, float))
        or not isinstance(expected, (int, float))
    ):
        return False
    return {
        "lt": actual < expected,
        "le": actual <= expected,
        "gt": actual > expected,
        "ge": actual >= expected,
    }.get(condition.operator, False)


def requirement_met(
    requirement: Requirement,
    steps: list[StepResult],
    parameters: dict[str, JsonValue],
) -> bool:
    """Check actual successful calls with matching entity parameters and result facts."""
    if requirement.for_each_tool is not None:
        per_action = requirement.model_copy(update={"for_each_tool": None})
        return all(
            requirement_met(
                per_action,
                [
                    subsequent
                    for subsequent in steps
                    if step.finished_at is not None and subsequent.started_at >= step.finished_at
                ],
                step.parameters,
            )
            for step in steps
            if step.tool == requirement.for_each_tool and step.success
        )

    def parameter_value(values: dict[str, JsonValue], key: str) -> tuple[bool, JsonValue]:
        return pointer_value(values, key if key.startswith("/") else "/" + key)

    candidates = [
        step
        for step in steps
        if step.tool == requirement.tool
        and all(matches(step.parameters, check) for check in requirement.parameter_checks)
        and all(
            parameter_value(step.parameters, source)[0]
            and parameter_value(parameters, target)[0]
            and parameter_value(step.parameters, source)[1]
            == parameter_value(parameters, target)[1]
            for source, target in requirement.match_parameters.items()
        )
    ]
    # A newer failed/inactive observation must not resurrect stale success.
    if not candidates or not candidates[-1].success:
        return False
    latest = {json.dumps(step.parameters, sort_keys=True): step for step in candidates}
    successes = [
        step
        for step in latest.values()
        if step.success and all(matches(step.result, check) for check in requirement.result_checks)
    ]
    return len(successes) >= requirement.min_successes and all(
        matches(candidates[-1].result, check) for check in requirement.result_checks
    )


def validate_policies(policies: list[ConstraintPolicy], count: int, tools: set[str]) -> None:
    """Require a nonempty executable interpretation for every supplied constraint."""
    if sorted(policy.constraint_index for policy in policies) != list(range(count)):
        raise rejected("Every constraint needs exactly one immutable policy.")
    for policy in policies:
        if not policy.rules and not policy.final_requirements:
            raise rejected("A constraint policy must contain an executable guard.")
        requirements = list(policy.final_requirements)
        for rule in policy.rules:
            if not set(rule.tools) <= tools:
                raise rejected("Constraint policy references an unavailable tool.")
            if rule.max_calls is None and not rule.prerequisites and not rule.parameter_checks:
                raise rejected("A constraint guard cannot be empty.")
            requirements.extend(rule.prerequisites)
            if any(requirement.for_each_tool is not None for requirement in rule.prerequisites):
                raise rejected("Per-action requirements are only supported at workflow completion.")
        if any(requirement.tool not in tools for requirement in requirements):
            raise rejected("Constraint prerequisite references an unavailable tool.")
        if any(
            requirement.match_parameters and requirement.for_each_tool is None
            for requirement in policy.final_requirements
        ):
            raise rejected("Final requirements cannot reference a pending call's parameters.")
        if any(
            requirement.for_each_tool is not None
            and (
                requirement.for_each_tool not in tools
                or requirement.for_each_tool == requirement.tool
            )
            for requirement in requirements
        ):
            raise rejected("Per-action requirement references an invalid source tool.")


def validate_parameters(tool: Tool, parameters: dict[str, JsonValue]) -> None:
    """Enforce advertised parameter names and JSON types without coercion."""
    definitions = {parameter.name: parameter for parameter in tool.parameters}
    if set(parameters) - definitions.keys():
        raise rejected("Call contains parameters not advertised by the tool.")
    for name, parameter in definitions.items():
        if name not in parameters:
            if parameter.required:
                raise rejected("Call omits a required tool parameter.")
            continue
        value = parameters[name]
        types: dict[str, type | tuple[type, ...]] = {
            "string": str,
            "str": str,
            "int": int,
            "integer": int,
            "number": (int, float),
            "float": (int, float),
            "boolean": bool,
            "bool": bool,
            "object": dict,
            "array": list,
            "list": list,
        }
        expected = types.get(parameter.type.lower())
        if expected is None:
            raise rejected("Tool advertises an unsupported parameter type.")
        if not isinstance(value, expected) or (
            isinstance(value, bool) and parameter.type.lower() not in {"boolean", "bool"}
        ):
            raise rejected("Call parameter does not match the advertised JSON type.")


def authorize_batch(
    calls: list[ProposedCall],
    tools: dict[str, Tool],
    policies: list[ConstraintPolicy],
    steps: list[StepResult],
) -> None:
    """Validate the entire independent batch before dispatching any of its calls."""
    reserved: list[str] = []
    for call in calls:
        if call.tool not in tools:
            raise rejected("Planner selected an unavailable tool.")
        validate_parameters(tools[call.tool], call.parameters)
        if any(
            not any(step.step == dependency and step.success for step in steps)
            for dependency in call.depends_on
        ):
            raise rejected("Call depends on a step that has not succeeded.")
        for policy in policies:
            for rule in policy.rules:
                if call.tool not in rule.tools:
                    continue
                # Count attempts, including uncertain writes and other reserved batch calls.
                count = sum(step.tool in rule.tools for step in steps) + sum(
                    name in rule.tools for name in reserved
                )
                if rule.max_calls is not None and count >= rule.max_calls:
                    raise rejected("Call would exceed a constraint action limit.")
                if not all(
                    requirement_met(requirement, steps, call.parameters)
                    for requirement in rule.prerequisites
                ) or not all(matches(call.parameters, check) for check in rule.parameter_checks):
                    raise rejected("Call does not satisfy its constraint prerequisites.")
        reserved.append(call.tool)
