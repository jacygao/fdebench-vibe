"""Real socket smoke test with the external model and Redis replaced."""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
import uvicorn
from fakeredis import FakeAsyncRedis
from fastapi import Request

from tests.conftest import completion
from tests.test_extraction import image_content
from tests.test_orchestration import call, plan
from triage.api import create_app
from triage.settings import Settings


@pytest.mark.parametrize("endpoint", ["triage", "extract", "orchestrate"])
def test_fifty_cold_identical_requests_over_real_http(
    settings: Settings, signal: dict[str, Any], decision: dict[str, Any], endpoint: str
) -> None:
    provider_calls = 0
    tool_calls = 0
    unicode_text = "\u200b\u202eMission status \U0001f680"

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        await asyncio.sleep(0.1)
        if endpoint == "triage":
            supplied = json.loads(json.loads(request.content)["messages"][1]["content"])
            assert supplied["description"].endswith(unicode_text)
            return completion(
                dict(decision, assigned_team="Threat Response Command")
                if provider_calls == 1
                else decision
            )
        if endpoint == "extract":
            return completion({"amount": "$1,234.50"})
        return completion(
            plan([call("inventory_query")]) if provider_calls == 1 else plan(status="completed")
        )

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        configured = settings.model_copy(
            update={
                "max_concurrent_models": 2,
                "max_concurrent_extractions": 2,
                "max_concurrent_workflows": 2,
                "tool_allowed_origins": [origin],
                "tool_private_origins": [origin],
            }
        )
        app = create_app(
            configured,
            transport=httpx.MockTransport(provider),
            redis_client=FakeAsyncRedis(decode_responses=True),
        )

        @app.post("/probe-tool")
        async def actual_tool() -> dict[str, bool]:
            nonlocal tool_calls
            tool_calls += 1
            return {"checked": True}

        payloads = {
            "triage": dict(signal, description=signal["description"] + unicode_text),
            "extract": {
                "document_id": "cold-probe-document",
                "content": image_content(),
                "content_format": "image_base64",
                "json_schema": {
                    "type": "object",
                    "properties": {"amount": {"type": ["number", "null"]}},
                },
            },
            "orchestrate": {
                "task_id": "cold-probe-workflow",
                "goal": "Read the synthetic inventory once. " + unicode_text,
                "available_tools": [
                    {
                        "name": "inventory_query",
                        "description": "Read a synthetic inventory status.",
                        "endpoint": origin + "/probe-tool",
                        "parameters": [],
                    }
                ],
                "constraints": [],
            },
        }
        required = {
            "triage": set(decision),
            "extract": {"document_id", "amount"},
            "orchestrate": {"task_id", "status", "reason", "steps_executed"},
        }
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started

            async def probe() -> None:
                async with httpx.AsyncClient(
                    base_url=origin, timeout=5, limits=httpx.Limits(max_connections=50)
                ) as client:
                    responses = await asyncio.gather(
                        *(client.post("/" + endpoint, json=payloads[endpoint]) for _ in range(50))
                    )
                    assert [response.status_code for response in responses] == [200] * 50
                    assert all(
                        required[endpoint] <= response.json().keys() for response in responses
                    )
                    if endpoint != "orchestrate":
                        assert all(response.json() == responses[0].json() for response in responses)
                    repeats = [
                        await client.post("/" + endpoint, json=payloads[endpoint]) for _ in range(3)
                    ]
                    assert all(response.status_code == 200 for response in repeats)
                    assert all(response.json() == repeats[0].json() for response in repeats)
                    if endpoint == "orchestrate":
                        assert repeats[0].json()["status"] == "completed"
                        assert repeats[0].json()["finished"]
                        assert len(repeats[0].json()["steps_executed"]) == 1
                    else:
                        assert all(
                            response.headers["x-llm-total-tokens"] == "0" for response in repeats
                        )

            asyncio.run(probe())
            assert provider_calls == (1 if endpoint == "extract" else 2)
            assert tool_calls == (1 if endpoint == "orchestrate" else 0)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive(), "HTTP server did not stop"


@pytest.mark.parametrize("failure", ["schema", "routing", "ticket"])
def test_live_triage_burst_queues_and_repairs_invalid_decisions(
    settings: Settings, signal: dict[str, Any], decision: dict[str, Any], failure: str
) -> None:
    active = 0
    peak = 0
    calls: dict[str, int] = {}
    barrier = threading.Barrier(8)

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        current = json.loads(json.loads(request.content)["messages"][1]["content"])
        ticket_id = current["ticket_id"]
        calls[ticket_id] = calls.get(ticket_id, 0) + 1
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.1)
            result = dict(decision, ticket_id=ticket_id)
            if ticket_id == "SIG-700000" and calls[ticket_id] == 1:
                result.update(
                    {
                        "schema": {"missing_information": ["affected_crew", "affected_crew"]},
                        "routing": {"assigned_team": "Threat Response Command"},
                        "ticket": {"ticket_id": "SIG-999"},
                    }[failure]
                )
            return completion(result)
        finally:
            active -= 1

    app = create_app(
        settings.model_copy(update={"max_concurrent_models": 2}),
        transport=httpx.MockTransport(provider),
        redis_client=FakeAsyncRedis(decode_responses=True),
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            with httpx.Client(
                base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=5
            ) as client:

                def send(index: int) -> httpx.Response:
                    barrier.wait(timeout=5)
                    return client.post(
                        "/triage", json=dict(signal, ticket_id=f"SIG-{700000 + index}")
                    )

                with ThreadPoolExecutor(max_workers=8) as pool:
                    responses = list(pool.map(send, range(8)))
                assert [response.status_code for response in responses] == [200] * 8
                for index, response in enumerate(responses):
                    assert response.json() == dict(decision, ticket_id=f"SIG-{700000 + index}")
                assert peak == 2
                assert sum(calls.values()) == 9
                assert responses[0].headers["x-llm-total-tokens"] == "1200"
                cached = client.post("/triage", json=dict(signal, ticket_id="SIG-700000"))
                assert cached.status_code == 200
                assert cached.headers["x-decision-cache"] == "hit"
                assert sum(calls.values()) == 9
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive(), "HTTP server did not stop"


def test_live_server_contract(
    settings: Settings, signal: dict[str, Any], decision: dict[str, Any]
) -> None:
    redis = FakeAsyncRedis(decode_responses=True)

    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["response_format"]["type"] == "json_object":
            return completion({"amount": "$1,234.50"})
        return completion(decision)

    app = create_app(
        settings,
        transport=httpx.MockTransport(provider),
        redis_client=redis,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "HTTP server failed to start"
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3) as client:
                assert client.get("/health").status_code == 200
                assert client.get("/ready").status_code == 200
                result = client.post("/triage", json=signal)
                assert result.status_code == 200
                assert result.json() == decision
                assert result.headers["x-model-name"] == settings.llm_model
                assert client.post("/triage", content="{").status_code == 415
                cached = client.post("/triage", json=signal)
                assert cached.headers["x-decision-cache"] == "hit"
                document = {
                    "document_id": "Exact document ID 001",
                    "content": image_content(),
                    "content_format": "image_base64",
                    "json_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"amount": {"type": ["number", "null"]}},
                    },
                }
                extracted = client.post("/extract", json=document)
                assert extracted.status_code == 200
                assert extracted.json() == {
                    "document_id": document["document_id"],
                    "amount": 1234.5,
                }
                assert extracted.headers["x-model-name"] == settings.extract_model
                invalid = client.post(
                    "/extract", json=document | {"json_schema": {"type": "object", "$schema": []}}
                )
                assert invalid.status_code == 422
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive(), "HTTP server did not stop"


def test_orchestration_over_real_http_with_pinned_dispatch(settings: Settings) -> None:
    """Run the public endpoint and stateful tools over sockets, not a tool mock transport."""
    seen: list[dict[str, Any]] = []
    plans = [
        plan([call("crm_search", {"filter": "name = Example Corp"})]),
        plan([call("email_send", {"account_id": "returned-opaque-id"})]),
        plan(status="completed"),
    ]

    def provider(_: httpx.Request) -> httpx.Response:
        return completion(plans.pop(0))

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        config = settings.model_copy(
            update={"tool_allowed_origins": [origin], "tool_private_origins": [origin]}
        )
        app = create_app(
            config,
            transport=httpx.MockTransport(provider),
            redis_client=FakeAsyncRedis(decode_responses=True),
        )

        @app.post("/opaque/{tool_id}")
        async def actual_tool(tool_id: str, request: Request) -> dict[str, Any]:
            parameters = await request.json()
            seen.append({"tool": tool_id, "parameters": parameters})
            assert request.scope["query_string"] == b"token=a%2Fb"
            if tool_id == "random-crm-7":
                return {"accounts": [{"account_id": "returned-opaque-id"}]}
            assert parameters == {"account_id": "returned-opaque-id"}
            return {"delivered": True}

        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            workflow = {
                "task_id": " exact opaque ID / 812 ",
                "goal": "Look up Example Corp and send its welcome message.",
                "available_tools": [
                    {
                        "name": name,
                        "description": "Fixture tool",
                        "endpoint": f"{origin}/opaque/{tool_id}?token=a%2Fb",
                        "parameters": [{"name": field, "type": "string", "required": True}],
                    }
                    for name, tool_id, field in [
                        ("crm_search", "random-crm-7", "filter"),
                        ("email_send", "random-email-9", "account_id"),
                    ]
                ],
            }
            with httpx.Client(base_url=origin, timeout=5) as client:
                result = client.post("/orchestrate", json=workflow)
                assert result.status_code == 200
                assert result.json()["status"] == "completed"
                assert result.json()["task_id"] == workflow["task_id"]
                assert len(seen) == len(result.json()["steps_executed"]) == 2
                assert client.post("/orchestrate", json=workflow).json() == result.json()
                assert len(seen) == 2
                assert (
                    client.post(
                        "/orchestrate", content="{", headers={"Content-Type": "application/json"}
                    ).status_code
                    == 422
                )
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive()
