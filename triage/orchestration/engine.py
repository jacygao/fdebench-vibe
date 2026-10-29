"""Bounded observe-plan-execute loop with a durable no-replay boundary."""

import asyncio
import logging
import re
from dataclasses import dataclass

from pydantic import JsonValue
from redis.asyncio import Redis

from triage.errors import TriageError
from triage.llm import Usage
from triage.orchestration.dispatch import Destination, Dispatcher, validate_destination
from triage.orchestration.guards import (
    authorize_batch,
    rejected,
    requirement_met,
    validate_policies,
)
from triage.orchestration.journal import Journal
from triage.orchestration.models import (
    ConstraintPolicy,
    ProposedCall,
    WorkflowRequest,
    WorkflowResult,
)
from triage.orchestration.planner import Planner
from triage.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestrationResult:
    record: WorkflowResult
    usage: Usage
    cache_hit: bool
    model_name: str


class OrchestrationEngine:
    def __init__(
        self, planner: Planner, dispatcher: Dispatcher, redis: Redis, settings: Settings
    ) -> None:
        self.planner = planner
        self.dispatcher = dispatcher
        self.redis = redis
        self.settings = settings
        self.capacity = asyncio.Semaphore(settings.max_concurrent_workflows)

    async def orchestrate(
        self, workflow: WorkflowRequest, *, deadline: float | None = None
    ) -> OrchestrationResult:
        """Run once per opaque task ID; return the persisted snapshot on duplicates."""
        usage = Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + self.settings.orchestrate_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline), self.capacity:
                existing = await Journal.existing(self.redis, workflow)
                if existing is not None:
                    return OrchestrationResult(
                        existing.state.result,
                        usage,
                        True,
                        existing.state.model_name or self.settings.orchestrate_model,
                    )
                destinations = dict(
                    zip(
                        [tool.name for tool in workflow.available_tools],
                        await asyncio.gather(
                            *(
                                validate_destination(
                                    tool.endpoint, self.settings, task_id=workflow.task_id
                                )
                                for tool in workflow.available_tools
                            )
                        ),
                        strict=True,
                    )
                )
                if not self.settings.configured:
                    raise TriageError(
                        "MODEL_NOT_CONFIGURED",
                        "Configure TRIAGE_LLM_API_KEY before starting a workflow.",
                        503,
                    )
                journal, claimed = await Journal.claim(
                    self.redis, workflow, model_name=self.settings.orchestrate_model
                )
                if not claimed:
                    return OrchestrationResult(
                        journal.state.result,
                        usage,
                        True,
                        journal.state.model_name or self.settings.orchestrate_model,
                    )
                execution_deadline = deadline - 2
                try:
                    async with asyncio.timeout_at(execution_deadline):
                        await self._run(
                            workflow, journal, destinations, usage, deadline=execution_deadline
                        )
                except (TriageError, TimeoutError) as exc:
                    journal.state.result.reason = (
                        exc.code if isinstance(exc, TriageError) else "WORKFLOW_DEADLINE"
                    )
                    journal.state.result.status = (
                        "partial" if journal.state.result.steps_executed else "failed"
                    )
                    logger.warning(
                        "workflow_stopped",
                        extra={
                            "task_id": workflow.task_id,
                            "code": journal.state.result.reason,
                        },
                    )
                for step in journal.state.result.steps_executed:
                    if step.outcome == "pending":
                        step.outcome = "unknown"
                        step.result_summary = "Execution interrupted; remote outcome is unknown."
                journal.state.result.finished = True
                await journal.persist()
                return OrchestrationResult(
                    journal.state.result, usage, False, self.settings.orchestrate_model
                )
        except TimeoutError as exc:
            raise TriageError(
                "WORKFLOW_DEADLINE",
                "Workflow deadline exceeded; do not replay under a different task ID.",
                504,
            ) from exc

    async def _run(
        self,
        workflow: WorkflowRequest,
        journal: Journal,
        destinations: dict[str, Destination],
        usage: Usage,
        *,
        deadline: float,
    ) -> None:
        tools = {tool.name: tool for tool in workflow.available_tools}
        policies: list[ConstraintPolicy] | None = None
        for _ in range(self.settings.orchestrate_max_rounds):
            if len(journal.state.model_dump_json().encode()) > 2 * 1024 * 1024:
                raise TriageError(
                    "WORKFLOW_TRACE_LIMIT", "Workflow trace exceeds the planning budget.", 502
                )
            plan, consumed = await self.planner.propose(
                workflow, journal.state.result.steps_executed, policies, deadline=deadline
            )
            usage.prompt_tokens += consumed.prompt_tokens
            usage.completion_tokens += consumed.completion_tokens
            usage.total_tokens += consumed.total_tokens
            logger.info(
                "workflow_plan_completed",
                extra={
                    "task_id": workflow.task_id,
                    "model": self.settings.orchestrate_model,
                    "total_tokens": consumed.total_tokens,
                },
            )
            if policies is None:
                validate_policies(plan.policies, len(workflow.constraints), set(tools))
                policies = plan.policies
                journal.state.policies = policies
                await journal.persist()
            elif plan.policies and plan.policies != policies:
                raise rejected("Planner attempted to change immutable constraint policies.")
            if plan.status != "continue":
                if plan.calls:
                    raise rejected("A terminal plan cannot contain tool calls.")
                if plan.status == "completed":
                    if not any(
                        step.success for step in journal.state.result.steps_executed
                    ) or not all(
                        requirement_met(requirement, journal.state.result.steps_executed, {})
                        for policy in policies
                        for requirement in policy.final_requirements
                    ):
                        raise rejected("Completion lacks required executed actions.")
                    if self._unresolved_side_effect(journal):
                        raise rejected("Uncertain side effects prevent a completed status.")
                journal.state.result.status = plan.status
                # Model prose can contain customer data; return it to the caller, not stdout.
                journal.state.result.reason = plan.reason
                return
            if not plan.calls:
                raise rejected("A continuing plan must contain an executable next batch.")
            if (
                len(journal.state.result.steps_executed) + len(plan.calls)
                > self.settings.orchestrate_max_steps
            ):
                raise rejected("Planned batch exceeds the remaining HTTP step budget.")
            authorize_batch(plan.calls, tools, policies, journal.state.result.steps_executed)
            self._prevent_mutation_replay(plan.calls, journal, workflow)
            outcomes = await asyncio.gather(
                *(
                    self.dispatcher.execute(
                        call, tools, destinations, policies, journal, deadline=deadline
                    )
                    for call in plan.calls
                ),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
        raise TriageError("WORKFLOW_ROUND_LIMIT", "Workflow planning budget exhausted.", 502)

    def _prevent_mutation_replay(
        self, calls: list[ProposedCall], journal: Journal, workflow: WorkflowRequest
    ) -> None:
        seen = [(step.tool, step.parameters) for step in journal.state.result.steps_executed]
        safe = self.settings.tool_read_only_names + self.settings.tool_idempotent_names
        for call in calls:
            identity = (call.tool, call.parameters)
            if call.tool not in safe and (
                identity in seen
                or any(
                    step.tool == call.tool and step.outcome == "unknown"
                    for step in journal.state.result.steps_executed
                )
            ):
                raise rejected("Planner attempted to repeat a stateful or uncertain mutation.")
            if call.tool not in safe:
                for name, value in call.parameters.items():
                    if name.endswith("_id") and not self._observed_identifier(
                        value, journal, workflow
                    ):
                        raise rejected("A mutation uses an identifier not supplied or observed.")
            seen.append(identity)

    @staticmethod
    def _observed_identifier(value: JsonValue, journal: Journal, workflow: WorkflowRequest) -> bool:
        def contains(data: JsonValue) -> bool:
            if isinstance(data, dict):
                return any(contains(item) for item in data.values())
            if isinstance(data, list):
                return any(contains(item) for item in data)
            return type(data) is type(value) and data == value

        if any(
            contains(step.result) for step in journal.state.result.steps_executed if step.success
        ):
            return True
        trusted = "\n".join([workflow.goal, *workflow.constraints])
        return isinstance(value, str) and bool(
            re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", trusted)
        )

    def _unresolved_side_effect(self, journal: Journal) -> bool:
        safe = self.settings.tool_read_only_names + self.settings.tool_idempotent_names
        steps = journal.state.result.steps_executed
        return any(
            step.outcome == "unknown"
            and (
                step.tool not in safe
                or not any(
                    later.step > step.step
                    and later.tool == step.tool
                    and later.parameters == step.parameters
                    and later.success
                    for later in steps
                )
            )
            for step in steps
        )
