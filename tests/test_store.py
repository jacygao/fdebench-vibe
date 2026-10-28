"""Atomic shared decision and lease semantics."""

from typing import Any

from redis.asyncio import Redis

from triage.models import Signal, TriageDecision
from triage.settings import Settings
from triage.store import DecisionStore


async def test_first_committed_decision_wins(
    settings: Settings, redis: Redis, signal: dict[str, Any], decision: dict[str, Any]
) -> None:
    store = DecisionStore(redis, settings)
    key = store.key(Signal.model_validate(signal))
    first = TriageDecision.model_validate(decision)
    other = first.model_copy(update={"next_best_action": "A competing decision."})
    assert await store.save(key, first) == first
    assert await store.save(key, other) == first


async def test_stale_worker_cannot_delete_successor_lease(
    settings: Settings, redis: Redis, signal: dict[str, Any]
) -> None:
    store = DecisionStore(redis, settings)
    key = store.key(Signal.model_validate(signal))
    original = await store.acquire(key)
    await redis.set(f"{key}:lease", "new-worker", px=5000)
    await store.release(key, original)
    assert await redis.get(f"{key}:lease") == "new-worker"
