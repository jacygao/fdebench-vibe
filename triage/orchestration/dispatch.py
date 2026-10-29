"""Exact-URL, bounded HTTP dispatch with DNS pinning and conservative retries."""

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from time import perf_counter, time
from urllib.parse import urlsplit

import httpx

from triage.errors import TriageError
from triage.json_values import parse_json
from triage.llm import retry_after_seconds
from triage.orchestration.guards import authorize_batch
from triage.orchestration.journal import Journal
from triage.orchestration.models import ConstraintPolicy, ProposedCall, Tool
from triage.settings import Settings

logger = logging.getLogger(__name__)

MAX_TOOL_RESPONSE_BYTES = 256 * 1024


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.transports: dict[tuple[str, str, int | None], httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Connect to a validated IP while preserving original Host, path, query and TLS SNI."""
        address = request.extensions["tool_address"]
        if not isinstance(address, str):
            raise ValueError("A validated tool IP address is required")
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = request.url.host
        origin = (request.url.scheme, request.url.host, request.url.port)
        if origin not in self.transports:
            # Separate pools prevent two TLS hostnames sharing an IP from sharing a TLS session.
            self.transports[origin] = httpx.AsyncHTTPTransport(
                trust_env=False,
                limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
            )
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=request.headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self.transports[origin].handle_async_request(pinned)

    async def aclose(self) -> None:
        """Close all origin-isolated connection pools."""
        await asyncio.gather(*(transport.aclose() for transport in self.transports.values()))


@dataclass(frozen=True)
class Destination:
    endpoint: str
    address: str


async def validate_destination(
    endpoint: str, settings: Settings, *, task_id: str | None = None
) -> Destination:
    """Validate an exact operator-authorized origin and pin a safe DNS answer."""
    safe_origin: str | None = None
    try:
        parsed = urlsplit(endpoint)
        url = httpx.URL(endpoint)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(ord(char) <= 32 or ord(char) >= 127 for char in endpoint)
            or str(url) != endpoint
        ):
            raise ValueError("Noncanonical or invalid tool URL")
        safe_origin = origin
        if origin not in settings.tool_allowed_origins:
            raise ValueError("Unapproved tool origin")
        private_allowed = origin in settings.tool_private_origins
        if parsed.scheme != "https" and not private_allowed:
            raise ValueError("Plain HTTP requires an explicitly trusted private origin")
        try:
            addresses = [ipaddress.ip_address(parsed.hostname)]
        except ValueError:
            answers = await asyncio.get_running_loop().getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
        if not addresses or any(
            not address.is_global and not private_allowed for address in addresses
        ):
            raise ValueError("Tool DNS resolved outside the authorized public network")
        return Destination(endpoint, str(addresses[0]))
    except (ValueError, httpx.InvalidURL, OSError) as exc:
        logger.warning(
            "tool_destination_rejected",
            extra={
                "task_id": task_id,
                "tool_origin": safe_origin,
                "code": "INVALID_TOOL_URL",
            },
        )
        raise TriageError(
            "INVALID_TOOL_URL",
            "Tool URLs must exactly match an operator-approved origin and safe destination.",
            422,
        ) from exc


class Dispatcher:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def execute(
        self,
        call: ProposedCall,
        tools: dict[str, Tool],
        destinations: dict[str, Destination],
        policies: list[ConstraintPolicy],
        journal: Journal,
        *,
        deadline: float,
    ) -> None:
        """Record every real attempt; never replay an uncertain or terminal mutation."""
        destination = destinations[call.tool]
        safe_retry = call.tool in (
            self.settings.tool_read_only_names + self.settings.tool_idempotent_names
        )
        for attempt in range(1, self.settings.tool_attempts + 1):
            cooldown = (
                max(
                    (
                        step.retry_not_before or 0
                        for step in journal.state.result.steps_executed
                        if destinations[step.tool].endpoint == destination.endpoint
                    ),
                    default=0,
                )
                - time()
            )
            if cooldown > 0:
                if cooldown + 0.05 >= deadline - asyncio.get_running_loop().time():
                    raise TriageError(
                        "TOOL_RETRY_AFTER_EXCEEDS_BUDGET",
                        "Tool cooldown exceeds the remaining workflow budget.",
                        502,
                    )
                await asyncio.sleep(cooldown)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0.05:
                return
            # Recheck limits atomically with the reservation of an actual attempt.
            async with journal.lock:
                authorize_batch([call], tools, policies, journal.state.result.steps_executed)
                if len(journal.state.result.steps_executed) >= self.settings.orchestrate_max_steps:
                    raise TriageError("WORKFLOW_STEP_LIMIT", "Workflow step budget exhausted.", 502)
                step = await journal.begin(call.tool, call.parameters, attempt)
            started = perf_counter()
            retry_delay = 0.2 * 2 ** (attempt - 1)
            retry = False
            try:
                timeout = min(self.settings.tool_timeout_seconds, remaining)
                async with asyncio.timeout(timeout):
                    async with self.client.stream(
                        "POST",
                        destination.endpoint,
                        json=call.parameters,
                        timeout=timeout,
                        follow_redirects=False,
                        extensions={"tool_address": destination.address},
                    ) as response:
                        step.status_code = response.status_code
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_TOOL_RESPONSE_BYTES:
                                raise TriageError(
                                    "TOOL_RESPONSE_TOO_LARGE",
                                    "Tool response exceeds the safe response limit.",
                                    502,
                                )
                            body.extend(chunk)
                        try:
                            step.result = parse_json(body.decode("utf-8"))
                        except (ValueError, RecursionError):
                            step.result_summary = "Tool returned invalid JSON."
                        else:
                            business_failed = isinstance(step.result, dict) and (
                                step.result.get("success") is False
                                or step.result.get("error") not in (None, False, "")
                            )
                            step.success = 200 <= response.status_code < 300 and not business_failed
                            step.result_summary = (
                                "Tool returned a successful JSON response."
                                if step.success
                                else "Tool returned an HTTP or business failure."
                            )
                        step.outcome = (
                            "succeeded"
                            if step.success
                            else "unknown"
                            if (200 <= response.status_code < 300 and step.result is None)
                            or (response.status_code >= 500 and not safe_retry)
                            else "failed"
                        )
                        retry = response.status_code == 429 or (
                            response.status_code >= 500 and safe_retry
                        )
                        retry_after = retry_after_seconds(response.headers.get("Retry-After"))
                        if retry_after is not None:
                            retry_delay = max(retry_delay, retry_after)
                        if response.status_code == 429:
                            step.retry_not_before = time() + retry_delay
            except (TimeoutError, httpx.RequestError):
                step.outcome = "unknown"
                step.result_summary = "Transport failed; the remote side effect may have occurred."
                retry = safe_retry
            except TriageError as exc:
                step.outcome = "unknown"
                step.result_summary = exc.code
            step.duration_ms = round((perf_counter() - started) * 1000, 2)
            step.finished_at = time()
            await journal.finish_step(step)
            if (
                step.success
                or not retry
                or attempt == self.settings.tool_attempts
                or retry_delay + 0.05 >= deadline - asyncio.get_running_loop().time()
            ):
                return
            # A 429 cooldown is checked at the start of every attempt, including replans.
            if step.status_code != 429:
                await asyncio.sleep(retry_delay)
