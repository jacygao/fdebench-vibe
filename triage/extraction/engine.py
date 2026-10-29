"""Bounded extraction orchestration and shared repeatability."""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter

from triage.errors import TriageError
from triage.extraction.images import prepare_image
from triage.extraction.models import ExtractionRecord, ExtractionRequest
from triage.extraction.normalization import normalize_record
from triage.extraction.schemas import SchemaPlan, compile_schema
from triage.extraction.vision import extract_document
from triage.llm import LLMClient, Usage
from triage.settings import Settings
from triage.store import ResultStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractionResult:
    record: ExtractionRecord
    usage: Usage
    estimated_cost: Decimal | None
    cache_hit: bool = False


class ExtractionEngine:
    def __init__(
        self, llm: LLMClient, store: ResultStore[ExtractionRecord], settings: Settings
    ) -> None:
        self.llm = llm
        self.store = store
        self.settings = settings
        self.capacity = asyncio.Semaphore(settings.max_concurrent_extractions)

    async def extract(
        self, document: ExtractionRequest, *, deadline: float | None = None
    ) -> ExtractionResult:
        """Validate before inference and keep total processing below the caller's minute."""
        try:
            if deadline is None:
                deadline = asyncio.get_running_loop().time() + self.settings.extract_timeout_seconds
            async with asyncio.timeout_at(deadline):
                return await self._extract(document, deadline=deadline)
        except TimeoutError as exc:
            raise TriageError(
                "EXTRACTION_DEADLINE_EXCEEDED",
                "Extraction exceeded its time budget; retry later.",
                504,
            ) from exc

    async def _extract(self, document: ExtractionRequest, *, deadline: float) -> ExtractionResult:
        plan = await asyncio.to_thread(compile_schema, document.json_schema)
        if not self.settings.configured:
            raise TriageError(
                "MODEL_NOT_CONFIGURED", "Configure TRIAGE_LLM_API_KEY before extraction.", 503
            )
        key = self.store.key(document.model_copy(update={"json_schema": plan.schema}))
        async with self.store.reserve(key, deadline=deadline) as saved:
            if saved is not None:
                return self._cached(saved, plan, document.document_id)
            async with self.capacity:
                image = await asyncio.to_thread(prepare_image, document.content)
                started = perf_counter()
                result = await extract_document(
                    self.llm, image, plan, self.settings, deadline=deadline
                )
                logger.info(
                    "extraction_model_completed",
                    extra={
                        "document_id": document.document_id,
                        "model": self.settings.extract_model,
                        "duration_ms": round((perf_counter() - started) * 1000, 2),
                        "input_tokens": result.usage.prompt_tokens,
                        "output_tokens": result.usage.completion_tokens,
                        "total_tokens": result.usage.total_tokens,
                    },
                )
                fields = await asyncio.to_thread(
                    normalize_record, result.content, plan, document.document_id
                )
                record = ExtractionRecord(fields)
                saved = await self.store.save(key, record)
                validated = self._cached(saved, plan, document.document_id)
                return ExtractionResult(validated.record, result.usage, result.estimated_cost)

    def _cached(
        self, saved: ExtractionRecord, plan: SchemaPlan, document_id: str
    ) -> ExtractionResult:
        if saved.root.get("document_id") != document_id:
            raise TriageError("DECISION_STORE_INVALID", "Stored document ID does not match.", 503)
        normalized = normalize_record(saved.model_dump_json(), plan, document_id)
        if normalized != saved.root:
            raise TriageError("DECISION_STORE_INVALID", "Stored document fields are invalid.", 503)
        return ExtractionResult(
            saved,
            Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            Decimal(0)
            if self.settings.input_price_per_million is not None
            and self.settings.output_price_per_million is not None
            else None,
            cache_hit=True,
        )
