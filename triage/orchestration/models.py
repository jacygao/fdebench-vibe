"""Workflow contracts and the deliberately non-executable planner language."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolParameter(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=4000)
    required: bool = False


class Tool(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(max_length=8000)
    endpoint: str = Field(min_length=1, max_length=4096, repr=False)
    parameters: list[ToolParameter] = Field(max_length=64)

    @model_validator(mode="after")
    def unique_parameters(self) -> Self:
        """Reject ambiguous parameter definitions before planning."""
        names = [parameter.name for parameter in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("Tool parameter names must be unique")
        return self


class WorkflowRequest(StrictModel):
    task_id: str = Field(min_length=1, max_length=255)
    goal: str = Field(min_length=1, max_length=16000, repr=False)
    available_tools: list[Tool] = Field(min_length=1, max_length=40)
    constraints: list[str] = Field(default_factory=list, max_length=40, repr=False)
    # Harness metadata is accepted but never used to construct or select tool URLs.
    mock_service_url: str | None = Field(default=None, max_length=4096, repr=False)

    @model_validator(mode="after")
    def unique_tools(self) -> Self:
        """Require unambiguous tool names and bounded nonblank constraints."""
        names = [tool.name for tool in self.available_tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique")
        if not self.goal.strip() or any(
            not value.strip() or len(value) > 4000 for value in self.constraints
        ):
            raise ValueError("Goal and constraints must be nonblank and bounded")
        return self


class StepResult(StrictModel):
    step: int
    tool: str
    parameters: dict[str, JsonValue] = Field(repr=False)
    result: JsonValue = Field(default=None, repr=False)
    result_summary: str
    success: bool
    status_code: int | None = None
    duration_ms: float = 0
    started_at: float = 0
    finished_at: float | None = None
    attempt: int = 1
    outcome: Literal["pending", "succeeded", "failed", "unknown"] = "pending"
    retry_not_before: float | None = None


class WorkflowResult(StrictModel):
    task_id: str
    status: Literal["completed", "partial", "failed"]
    steps_executed: list[StepResult] = Field(default_factory=list)
    reason: str
    finished: bool = False


class Condition(StrictModel):
    """Compare a JSON Pointer against a literal; never evaluate expressions."""

    pointer: str
    operator: Literal["eq", "ne", "in", "not_in", "lt", "le", "gt", "ge", "exists"]
    value: JsonValue = None


class Requirement(StrictModel):
    tool: str
    min_successes: int = Field(default=1, ge=1, le=64)
    match_parameters: dict[str, str] = Field(default_factory=dict)
    result_checks: list[Condition] = Field(default_factory=list, max_length=20)
    parameter_checks: list[Condition] = Field(default_factory=list, max_length=20)
    for_each_tool: str | None = None


class GuardRule(StrictModel):
    tools: list[str] = Field(min_length=1, max_length=40)
    max_calls: int | None = Field(default=None, ge=0, le=64)
    prerequisites: list[Requirement] = Field(default_factory=list, max_length=40)
    parameter_checks: list[Condition] = Field(default_factory=list, max_length=20)


class ConstraintPolicy(StrictModel):
    constraint_index: int = Field(ge=0)
    rules: list[GuardRule] = Field(default_factory=list, max_length=40)
    final_requirements: list[Requirement] = Field(default_factory=list, max_length=40)


class ProposedCall(StrictModel):
    tool: str
    parameters: dict[str, JsonValue] = Field(repr=False)
    # Dependencies are successful, completed trace step numbers, never planned calls.
    depends_on: list[int] = Field(default_factory=list, max_length=64)


class Plan(StrictModel):
    calls: list[ProposedCall] = Field(default_factory=list, max_length=16)
    status: Literal["continue", "completed", "partial", "failed"]
    reason: str = Field(min_length=1, max_length=2000)
    policies: list[ConstraintPolicy] = Field(default_factory=list, max_length=40)
