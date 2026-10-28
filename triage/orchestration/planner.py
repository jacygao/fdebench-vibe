"""The model proposes typed batches; it cannot dispatch tools or execute code."""

import json
from pathlib import Path

from pydantic import ValidationError

from triage.errors import TriageError
from triage.json_values import parse_json
from triage.llm import LLMClient, Usage
from triage.orchestration.models import ConstraintPolicy, Plan, StepResult, WorkflowRequest
from triage.settings import Settings

SYSTEM_PROMPT = Path(__file__).with_name("system_prompt.txt").read_text(encoding="utf-8")


class Planner:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    async def propose(
        self,
        workflow: WorkflowRequest,
        steps: list[StepResult],
        policies: list[ConstraintPolicy] | None,
        *,
        deadline: float,
    ) -> tuple[Plan, Usage]:
        """Return one next independent batch based only on the real execution history."""
        payload = {
            "workflow": workflow.model_dump(
                mode="json",
                exclude={
                    "mock_service_url": True,
                    "available_tools": {"__all__": {"endpoint"}},
                },
            ),
            "steps_executed": [step.model_dump(mode="json") for step in steps],
            "immutable_policies": (
                [policy.model_dump(mode="json") for policy in policies]
                if policies is not None
                else None
            ),
        }
        result = await self.llm.complete_json(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
                {
                    "role": "user",
                    "content": "Return JSON matching this plan schema: "
                    + json.dumps(Plan.model_json_schema()),
                },
            ],
            response_format={"type": "json_object"},
            model=self.settings.orchestrate_model,
            attempt_seconds=self.settings.orchestrate_model_timeout_seconds,
            max_tokens=5000,
            deadline=deadline,
        )
        try:
            return Plan.model_validate(parse_json(result.content)), result.usage
        except (ValidationError, ValueError, RecursionError) as exc:
            raise TriageError(
                "WORKFLOW_INVALID_PLAN", "Planner returned an invalid workflow plan.", 502
            ) from exc
