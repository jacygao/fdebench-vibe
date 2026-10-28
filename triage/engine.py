"""Domain invariants around the semantic triage decision."""

import asyncio
from dataclasses import replace
from decimal import Decimal

from triage.errors import TriageError
from triage.llm import LLMClient, ModelResult, Usage
from triage.models import Category, Priority, Signal, Team, TriageDecision
from triage.settings import Settings
from triage.store import DecisionStore

OWNERS = {
    Category.CREW_ACCESS: Team.IDENTITY,
    Category.STRUCTURAL: Team.SYSTEMS,
    Category.COMMUNICATIONS: Team.COMMUNICATIONS,
    Category.SOFTWARE: Team.SOFTWARE,
    Category.THREAT: Team.THREAT,
    Category.DATA: Team.DATA,
    Category.NOISE: Team.NONE,
}


class TriageEngine:
    def __init__(self, llm: LLMClient, store: DecisionStore, settings: Settings) -> None:
        self.llm = llm
        self.store = store
        self.settings = settings
        self.capacity = asyncio.Semaphore(settings.max_concurrent_models)

    async def triage(self, signal: Signal) -> ModelResult:
        """Return a stable shared decision under bounded latency and concurrency."""
        if not self.settings.configured:
            raise TriageError(
                "MODEL_NOT_CONFIGURED", "Configure TRIAGE_LLM_API_KEY before triaging signals.", 503
            )
        try:
            deadline = asyncio.get_running_loop().time() + self.settings.request_timeout_seconds
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                return await self._triage(signal, deadline=deadline)
        except TimeoutError as exc:
            raise TriageError(
                "TRIAGE_DEADLINE_EXCEEDED", "Triage exceeded its time budget; retry later.", 504
            ) from exc

    async def _triage(self, signal: Signal, *, deadline: float) -> ModelResult:
        key = self.store.key(signal)
        saved = await self.store.get(key)
        if saved is not None:
            return self._cached(signal, saved)
        try:
            async with asyncio.timeout(0.01):
                await self.capacity.acquire()
        except TimeoutError as exc:
            raise TriageError(
                "MODEL_CAPACITY_EXCEEDED",
                "All model slots are busy; retry later.",
                503,
                retry_after=1,
            ) from exc
        try:
            token = await self.store.acquire(key)
            try:
                # Another worker may have committed between our read and lease acquisition.
                saved = await self.store.get(key)
                if saved is not None:
                    return self._cached(signal, saved)
                result = await self.llm.classify(signal, deadline=deadline)
                result = self._validate(signal, result)
                saved = await self.store.save(key, result.decision)
                return replace(result, decision=saved)
            finally:
                await self.store.release(key, token)
        finally:
            self.capacity.release()

    def _cached(self, signal: Signal, decision: TriageDecision) -> ModelResult:
        return self._validate(
            signal,
            ModelResult(
                decision,
                Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                Decimal(0)
                if self.settings.input_price_per_million is not None
                and self.settings.output_price_per_million is not None
                else None,
                cache_hit=True,
            ),
        )

    @staticmethod
    def _validate(signal: Signal, result: ModelResult) -> ModelResult:
        decision = result.decision
        if decision.ticket_id != signal.ticket_id:
            raise TriageError(
                "MODEL_TICKET_MISMATCH",
                "The model returned a decision for a different signal.",
                502,
            )
        owner = OWNERS.get(decision.category)
        if owner is not None and decision.assigned_team != owner:
            raise TriageError(
                "MODEL_ROUTING_CONFLICT",
                "The model returned conflicting category and team ownership.",
                502,
            )
        if decision.priority == Priority.P1 and not decision.needs_escalation:
            decision = decision.model_copy(update={"needs_escalation": True})
        return replace(result, decision=decision)
