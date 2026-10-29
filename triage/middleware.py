"""Bound request memory and extraction admission before JSON decoding."""

import asyncio
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 65536
MAX_EXTRACT_BODY_BYTES = 16 * 1024 * 1024
MAX_ORCHESTRATE_BODY_BYTES = 256 * 1024
logger = logging.getLogger(__name__)


class RequestSizeLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        extract_concurrency: int = 4,
        workflow_concurrency: int = 8,
        triage_timeout: float = 20,
        extract_timeout: float = 45,
        workflow_timeout: float = 55,
    ) -> None:
        self.app = app
        self.extraction_capacity = asyncio.Semaphore(extract_concurrency)
        self.workflow_capacity = asyncio.Semaphore(workflow_concurrency)
        self.timeouts = {
            "/triage": triage_timeout,
            "/extract": extract_timeout,
            "/orchestrate": workflow_timeout,
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject oversized requests even when Content-Length is absent or misleading."""
        if scope["type"] != "http" or scope["path"] not in {
            "/triage",
            "/extract",
            "/orchestrate",
        }:
            await self.app(scope, receive, send)
            return
        deadline = asyncio.get_running_loop().time() + self.timeouts[scope["path"]]
        scope.setdefault("state", {})["deadline"] = deadline
        if scope["path"] in {"/extract", "/orchestrate"}:
            capacity = (
                self.extraction_capacity if scope["path"] == "/extract" else self.workflow_capacity
            )
            try:
                async with asyncio.timeout_at(deadline):
                    await capacity.acquire()
            except TimeoutError:
                await self._deadline_response(scope, receive, send)
                return
            try:
                await self._receive_request(scope, receive, send)
            finally:
                capacity.release()
        else:
            await self._receive_request(scope, receive, send)

    async def _receive_request(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = dict(scope.get("headers", []))
        limit = {
            "/triage": MAX_BODY_BYTES,
            "/extract": MAX_EXTRACT_BODY_BYTES,
            "/orchestrate": MAX_ORCHESTRATE_BODY_BYTES,
        }[scope["path"]]
        content_type = (
            headers.get(b"content-type", b"").decode("latin-1").split(";")[0].strip().lower()
        )
        if scope["method"] == "POST" and not (
            content_type == "application/json"
            or (content_type.startswith("application/") and content_type.endswith("+json"))
        ):
            await JSONResponse(
                status_code=415,
                content={
                    "error": {"code": "INVALID_CONTENT_TYPE", "message": "Use application/json."}
                },
            )(scope, receive, send)
            return
        body = bytearray()
        oversized = False
        try:
            async with asyncio.timeout_at(scope["state"]["deadline"]):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > limit:
                        oversized = True
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await self._deadline_response(scope, receive, send)
            return
        if oversized:
            await JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": f"Request must not exceed {limit} bytes.",
                    }
                },
            )(scope, receive, send)
            return
        consumed = False

        async def limited_receive() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _deadline_response(scope: Scope, receive: Receive, send: Send) -> None:
        code = {
            "/triage": "TRIAGE_DEADLINE_EXCEEDED",
            "/extract": "EXTRACTION_DEADLINE_EXCEEDED",
            "/orchestrate": "WORKFLOW_DEADLINE",
        }[scope["path"]]
        logger.warning(
            "request_deadline_exceeded",
            extra={"code": code, "request_id": scope["state"].get("request_id")},
        )
        await JSONResponse(
            status_code=504,
            content={
                "error": {
                    "code": code,
                    "message": "Request deadline exceeded while waiting for admission or input.",
                }
            },
        )(scope, receive, send)
