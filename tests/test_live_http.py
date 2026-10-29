"""Real socket smoke test with the external model and Redis replaced."""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import uvicorn
from fakeredis import FakeAsyncRedis
from fastapi import Request

from tests.conftest import completion
from tests.test_extraction import image_content
from tests.test_orchestration import call, plan
from triage.api import create_app
from triage.settings import Settings


def test_live_triage_burst_queues_and_repairs_invalid_decisions(
    settings: Settings, signal: dict[str, Any], decision: dict[str, Any]
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
                result["missing_information"] = ["affected_crew", "affected_crew"]
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
