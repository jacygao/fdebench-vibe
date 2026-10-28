"""Permanent task claims and write-ahead traces for non-replayable workflows."""

import asyncio
import hashlib
import json
import logging
from time import time
from typing import Self

from pydantic import Field, JsonValue, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from triage.errors import TriageError
from triage.orchestration.models import (
    ConstraintPolicy,
    StepResult,
    StrictModel,
    WorkflowRequest,
    WorkflowResult,
)

logger = logging.getLogger(__name__)


class JournalState(StrictModel):
    fingerprint: str
    result: WorkflowResult
    version: int = Field(default=1)
    policies: list[ConstraintPolicy] = Field(default_factory=list)
    model_name: str | None = None


class Journal:
    def __init__(self, redis: Redis, key: str, state: JournalState) -> None:
        self.redis = redis
        self.key = key
        self.state = state
        self.lock = asyncio.Lock()
        self.failed = False

    @staticmethod
    def _identity(workflow: WorkflowRequest) -> tuple[str, str]:
        key = "orchestrate:task:" + hashlib.sha256(workflow.task_id.encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(workflow.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        return key, fingerprint

    @classmethod
    async def existing(cls, redis: Redis, workflow: WorkflowRequest) -> Self | None:
        """Return retained evidence even if the tools or their DNS are now unavailable."""
        key, fingerprint = cls._identity(workflow)
        try:
            raw = await redis.get(key)
            if raw is None:
                return None
            state = JournalState.model_validate_json(raw)
        except (RedisError, ValidationError) as exc:
            raise cls.unavailable() from exc
        if state.fingerprint != fingerprint:
            raise TriageError(
                "TASK_ID_CONFLICT", "This task ID already belongs to a different request.", 409
            )
        return cls(redis, key, state)

    @classmethod
    async def claim(
        cls, redis: Redis, workflow: WorkflowRequest, *, model_name: str | None = None
    ) -> tuple[Self, bool]:
        """Claim a task forever; model changes or worker crashes never authorize replay."""
        key, fingerprint = cls._identity(workflow)
        state = JournalState(
            fingerprint=fingerprint,
            model_name=model_name,
            result=WorkflowResult(
                task_id=workflow.task_id, status="failed", reason="WORKFLOW_IN_PROGRESS"
            ),
        )
        try:
            claimed = await redis.set(key, state.model_dump_json(), nx=True)
        except RedisError as exc:
            raise cls.unavailable() from exc
        if claimed:
            return cls(redis, key, state), True
        existing = await cls.existing(redis, workflow)
        if existing is None:
            raise cls.unavailable()
        return existing, False

    async def persist(self) -> None:
        """Persist a snapshot before further external actions; never silently lose the journal."""
        try:
            if not await self.redis.set(self.key, self.state.model_dump_json(), xx=True):
                self.failed = True
                raise self.unavailable()
        except RedisError as exc:
            self.failed = True
            raise self.unavailable() from exc

    async def begin(self, tool: str, parameters: dict[str, JsonValue], attempt: int) -> StepResult:
        """Record intent before a POST; caller holds the lock across guards and reservation."""
        if self.failed:
            raise self.unavailable()
        step = StepResult(
            step=len(self.state.result.steps_executed) + 1,
            tool=tool,
            parameters=parameters,
            success=False,
            result_summary="Request prepared; execution outcome not yet known.",
            attempt=attempt,
            started_at=time(),
        )
        self.state.result.steps_executed.append(step)
        self.state.result.status = "partial"
        await self.persist()
        logger.info(
            "workflow_step_started",
            extra={
                "task_id": self.state.result.task_id,
                "tool": step.tool,
                "step": step.step,
                "attempt": step.attempt,
                "outcome": step.outcome,
                "parameter_names": sorted(step.parameters),
            },
        )
        return step

    async def finish_step(self, step: StepResult) -> None:
        """Persist the observed response and emit value-free operational metadata."""
        async with self.lock:
            await self.persist()
            logger.info(
                "workflow_step_completed",
                extra={
                    "task_id": self.state.result.task_id,
                    "tool": step.tool,
                    "step": step.step,
                    "attempt": step.attempt,
                    "status": step.status_code,
                    "outcome": step.outcome,
                    "duration_ms": step.duration_ms,
                    "parameter_names": sorted(step.parameters),
                },
            )

    @staticmethod
    def unavailable() -> TriageError:
        """Fail closed when the durable side-effect journal is unavailable."""
        return TriageError(
            "WORKFLOW_JOURNAL_UNAVAILABLE",
            "Workflow journal unavailable; do not replay this task or use a new task ID.",
            503,
        )
