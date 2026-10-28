"""Shared immutable decisions and short-lived per-signal leases."""

import hashlib
import json
import logging
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from triage.errors import TriageError
from triage.llm import SYSTEM_PROMPT
from triage.models import TriageDecision
from triage.settings import Settings

logger = logging.getLogger(__name__)
RELEASE_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class ResultStore[T: BaseModel]:
    def __init__(
        self,
        redis: Redis,
        *,
        result_type: type[T],
        policy: dict[str, object],
        task: str,
        timeout_seconds: float,
    ) -> None:
        self.redis = redis
        self.result_type = result_type
        self.task = task
        self.release_lease = redis.register_script(RELEASE_LEASE)
        self.lease_ms = int((timeout_seconds + 2) * 1000)
        self.namespace = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()

    def key(self, signal: BaseModel) -> str:
        """Hash complete normalized inputs, not just ticket IDs, to avoid stale decisions."""
        canonical = json.dumps(signal.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        return f"{self.task}:{self.namespace}:{digest}"

    async def get(self, key: str) -> T | None:
        """Load a persisted decision; corrupted data is a loud infrastructure failure."""
        try:
            raw = await self.redis.get(key)
        except RedisError as exc:
            raise self._unavailable() from exc
        if raw is None:
            return None
        try:
            return self.result_type.model_validate_json(raw)
        except ValidationError as exc:
            raise TriageError(
                "DECISION_STORE_INVALID",
                "Stored result is invalid; operator action required.",
                503,
            ) from exc

    async def acquire(self, key: str) -> str:
        """Reserve one in-flight classification across instances or fail fast for retry."""
        token = str(uuid4())
        try:
            acquired = await self.redis.set(f"{key}:lease", token, nx=True, px=self.lease_ms)
        except RedisError as exc:
            raise self._unavailable() from exc
        if not acquired:
            raise TriageError(
                "SIGNAL_IN_PROGRESS" if self.task == "triage" else "DOCUMENT_IN_PROGRESS",
                "This input is already being processed; retry for the shared result.",
                503,
                retry_after=1,
            )
        return token

    async def save(self, key: str, decision: T) -> T:
        """Keep the first committed decision even if a lease expires or a worker is delayed."""
        try:
            await self.redis.set(key, decision.model_dump_json(), nx=True)
        except RedisError as exc:
            raise self._unavailable() from exc
        saved = await self.get(key)
        if saved is None:
            raise self._unavailable()
        return saved

    async def release(self, key: str, token: str) -> None:
        """Release only this worker's lease; a stale worker must not delete its successor's."""
        try:
            await self.release_lease(keys=[f"{key}:lease"], args=[token])
        except RedisError:
            # The lease expires independently; a committed decision remains readable.
            logger.warning(
                "decision_lease_release_failed", extra={"code": "DECISION_STORE_UNAVAILABLE"}
            )

    @staticmethod
    def _unavailable() -> TriageError:
        return TriageError(
            "DECISION_STORE_UNAVAILABLE",
            "Shared decision storage is unavailable; retry later.",
            503,
            retry_after=1,
        )


class DecisionStore(ResultStore[TriageDecision]):
    def __init__(self, redis: Redis, settings: Settings) -> None:
        super().__init__(
            redis,
            result_type=TriageDecision,
            policy={
                "model": settings.llm_model,
                "endpoint": settings.llm_url,
                "prompt": SYSTEM_PROMPT,
                "schema": TriageDecision.model_json_schema(),
                "engine_version": 1,
            },
            task="triage",
            timeout_seconds=settings.request_timeout_seconds,
        )
