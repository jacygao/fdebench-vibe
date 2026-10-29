"""FastAPI routes and application lifecycle."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.middleware.base import RequestResponseEndpoint

from triage.engine import TriageEngine
from triage.errors import TriageError
from triage.extraction.engine import ExtractionEngine, ExtractionResult
from triage.extraction.models import ExtractionRecord, ExtractionRequest
from triage.extraction.normalization import null_fields
from triage.extraction.vision import SYSTEM_PROMPT as EXTRACTION_PROMPT
from triage.llm import LLMClient, ModelResult
from triage.middleware import RequestSizeLimit
from triage.models import Signal, TriageDecision
from triage.observability import configure_logging
from triage.orchestration.dispatch import Dispatcher, PinnedTransport
from triage.orchestration.engine import OrchestrationEngine
from triage.orchestration.models import WorkflowRequest, WorkflowResult
from triage.orchestration.planner import Planner
from triage.settings import Settings
from triage.store import DecisionStore, ResultStore

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    tool_transport: httpx.AsyncBaseTransport | None = None,
    redis_client: Redis | None = None,
) -> FastAPI:
    """Create an isolated service; transport injection supports provider-level tests."""
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        redis = (
            redis_client
            if redis_client is not None
            else Redis.from_url(
                config.redis_url.get_secret_value(),
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
                decode_responses=True,
            )
        )
        app.state.redis = redis
        async with (
            httpx.AsyncClient(
                headers={"Content-Type": "application/json"},
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                follow_redirects=False,
                transport=transport,
            ) as client,
            httpx.AsyncClient(
                transport=tool_transport or PinnedTransport(),
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
            ) as tool_client,
        ):
            llm = LLMClient(config, client)
            app.state.orchestration = OrchestrationEngine(
                Planner(llm, config), Dispatcher(tool_client, config), redis, config
            )
            app.state.engine = TriageEngine(llm, DecisionStore(redis, config), config)
            app.state.extraction = ExtractionEngine(
                llm,
                ResultStore(
                    redis,
                    result_type=ExtractionRecord,
                    policy={
                        "model": config.extract_model,
                        "endpoint": config.llm_url,
                        "prompt": EXTRACTION_PROMPT,
                        "engine_version": 2,
                    },
                    task="extract",
                    timeout_seconds=config.extract_timeout_seconds,
                ),
                config,
            )
            try:
                yield
            finally:
                if redis_client is None:
                    await redis.aclose()

    app = FastAPI(title="FDEBench Operations API", version="0.3.0", lifespan=lifespan)
    app.add_middleware(
        RequestSizeLimit,
        extract_concurrency=config.max_concurrent_extractions,
        workflow_concurrency=config.max_concurrent_workflows,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = str(uuid4())
        start = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.error(
                "request_failed",
                extra={"request_id": request.state.request_id, "code": "INTERNAL_ERROR"},
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Unexpected service failure; contact the operator.",
                        "request_id": request.state.request_id,
                    }
                },
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.setdefault(
            "X-Model-Name",
            {
                "/extract": config.extract_model,
                "/orchestrate": config.orchestrate_model,
            }.get(request.url.path.rstrip("/"), config.llm_model),
        )
        response.headers["Cache-Control"] = "no-store"
        logger.info(
            "request_completed",
            extra={
                "request_id": request.state.request_id,
                "ticket_id": getattr(request.state, "ticket_id", None),
                "document_id": getattr(request.state, "document_id", None),
                "task_id": getattr(request.state, "task_id", None),
                "model": response.headers["X-Model-Name"],
                "status": response.status_code,
                "duration_ms": round((perf_counter() - start) * 1000, 2),
                "total_tokens": getattr(request.state, "total_tokens", None),
                "null_fields": getattr(request.state, "null_fields", []),
            },
        )
        return response

    @app.exception_handler(TriageError)
    async def triage_error(request: Request, exc: TriageError) -> JSONResponse:
        logger.warning(
            "triage_failed", extra={"request_id": request.state.request_id, "code": exc.code}
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": request.state.request_id,
                }
            },
            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_signal(request: Request, exc: RequestValidationError) -> JSONResponse:
        path = request.url.path.rstrip("/")
        code = {
            "/extract": "INVALID_DOCUMENT",
            "/orchestrate": "INVALID_WORKFLOW",
        }.get(path, "INVALID_SIGNAL")
        contract_fields = {
            "/extract": ExtractionRequest.model_fields,
            "/orchestrate": WorkflowRequest.model_fields,
        }.get(path, Signal.model_fields)
        errors = exc.errors()
        logger.warning(
            "request_validation_failed",
            extra={
                "request_id": request.state.request_id,
                "code": code,
                "validation_fields": sorted(
                    {
                        str(error["loc"][1])
                        for error in errors
                        if len(error["loc"]) > 1 and error["loc"][1] in contract_fields
                    }
                ),
                "validation_error_types": sorted({error["type"] for error in errors}),
            },
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": code,
                    "message": "Body does not match the request contract.",
                    "request_id": request.state.request_id,
                    "fields": [
                        {"location": list(error["loc"]), "type": error["type"]} for error in errors
                    ],
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        """Return liveness and configuration state without contacting the provider."""
        return {"status": "ok", "model_configured": config.configured}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Check model configuration and shared storage, but not remote model availability."""
        storage_ready = False
        try:
            storage_ready = bool(await app.state.redis.ping())
        except RedisError:
            logger.warning("readiness_failed", extra={"code": "DECISION_STORE_UNAVAILABLE"})
        is_ready = config.configured and storage_ready
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"ready": is_ready},
        )

    @app.post("/triage", response_model=TriageDecision)
    async def triage(signal: Signal, request: Request, response: Response) -> TriageDecision:
        """Return a validated decision and measured LLM usage."""
        engine: TriageEngine = request.app.state.engine
        request.state.ticket_id = signal.ticket_id
        start = perf_counter()
        result = await engine.triage(signal)
        request.state.total_tokens = result.usage.total_tokens
        _result_headers(response, result)
        logger.info(
            "triage_completed",
            extra={
                "request_id": request.state.request_id,
                "ticket_id": signal.ticket_id,
                "total_tokens": result.usage.total_tokens,
                "duration_ms": round((perf_counter() - start) * 1000, 2),
            },
        )
        return result.decision

    @app.post("/extract", response_model=ExtractionRecord)
    async def extract(
        document: ExtractionRequest, request: Request, response: Response
    ) -> ExtractionRecord:
        """Extract schema-defined fields and echo the exact caller document ID."""
        engine: ExtractionEngine = request.app.state.extraction
        request.state.document_id = document.document_id
        result = await engine.extract(document)
        request.state.total_tokens = result.usage.total_tokens
        request.state.null_fields = null_fields(result.record.root)
        _result_headers(response, result)
        logger.info(
            "extraction_completed",
            extra={
                "request_id": request.state.request_id,
                "document_id": document.document_id,
                "model": config.extract_model,
                "input_tokens": result.usage.prompt_tokens,
                "output_tokens": result.usage.completion_tokens,
                "total_tokens": result.usage.total_tokens,
                "null_fields": request.state.null_fields,
            },
        )
        return result.record

    @app.post("/orchestrate", response_model=WorkflowResult)
    async def orchestrate(
        workflow: WorkflowRequest, request: Request, response: Response
    ) -> WorkflowResult:
        """Execute authorized tools and return the persisted, non-replayable HTTP trace."""
        engine: OrchestrationEngine = request.app.state.orchestration
        request.state.task_id = workflow.task_id
        result = await engine.orchestrate(workflow)
        response.headers["X-Model-Name"] = result.model_name
        request.state.total_tokens = result.usage.total_tokens
        response.headers["X-LLM-Input-Tokens"] = str(result.usage.prompt_tokens)
        response.headers["X-LLM-Output-Tokens"] = str(result.usage.completion_tokens)
        response.headers["X-LLM-Total-Tokens"] = str(result.usage.total_tokens)
        response.headers["X-Decision-Cache"] = "hit" if result.cache_hit else "miss"
        return result.record

    return app


def _result_headers(response: Response, result: ModelResult | ExtractionResult) -> None:
    response.headers["X-LLM-Input-Tokens"] = str(result.usage.prompt_tokens)
    response.headers["X-LLM-Output-Tokens"] = str(result.usage.completion_tokens)
    response.headers["X-LLM-Total-Tokens"] = str(result.usage.total_tokens)
    response.headers["X-Decision-Cache"] = "hit" if result.cache_hit else "miss"
    if result.estimated_cost is not None:
        response.headers["X-LLM-Estimated-Cost-USD"] = format(result.estimated_cost, ".8f")


app = create_app()
