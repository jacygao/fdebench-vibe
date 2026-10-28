"""Provider-level test fixtures. No request leaves the test process."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr
from redis.asyncio import Redis

from triage.api import create_app
from triage.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_api_key=SecretStr("test-only-key"),
        llm_attempts=2,
    )


@pytest.fixture
def signal() -> dict[str, Any]:
    return {
        "ticket_id": "SIG-4829",
        "subject": "Not a big deal",
        "description": "Pressure is falling on Deck 4. Possible hull breach.",
        "reporter": {
            "name": "Mira Chen",
            "email": "mira.chen@cdss.space",
            "department": "Habitat Operations",
        },
        "created_at": "2026-10-28T03:00:00Z",
        "channel": "bridge_terminal",
        "attachments": ["deck4-pressure.csv"],
    }


@pytest.fixture
def decision() -> dict[str, Any]:
    return {
        "ticket_id": "SIG-4829",
        "category": "Hull & Structural Systems",
        "priority": "P1",
        "assigned_team": "Spacecraft Systems Engineering",
        "needs_escalation": True,
        "missing_information": ["affected_crew"],
        "next_best_action": "Notify station emergency command of the suspected breach.",
        "remediation_steps": [
            "Alert qualified emergency responders with the affected deck.",
            "Share the pressure capture and follow approved station emergency procedures.",
        ],
    }


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = FakeAsyncRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def completion(decision: dict[str, Any]) -> httpx.Response:
    """Build a realistic structured completion and usage envelope."""
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decision)}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600},
        },
    )


@asynccontextmanager
async def api_client(
    settings: Settings,
    redis: Redis,
    handler: Callable[..., Any],
) -> AsyncIterator[httpx.AsyncClient]:
    """Run real application lifecycle with only external HTTP and Redis replaced."""
    app = create_app(settings, transport=httpx.MockTransport(handler), redis_client=redis)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
