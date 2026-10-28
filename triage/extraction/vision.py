"""One vision completion with stable policy and untrusted schema/image data."""

import json
from pathlib import Path

from triage.extraction.images import PreparedImage
from triage.extraction.schemas import SchemaPlan
from triage.llm import JsonCompletion, LLMClient
from triage.settings import Settings

SYSTEM_PROMPT = Path(__file__).with_name("system_prompt.txt").read_text(encoding="utf-8")


async def extract_document(
    llm: LLMClient,
    image: PreparedImage,
    plan: SchemaPlan,
    settings: Settings,
    *,
    deadline: float,
) -> JsonCompletion:
    """Send only the stable system policy and JSON-encoded field/image data."""
    return await llm.complete_json(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps({"json_schema": plan.schema})},
                    {"type": "image_url", "image_url": {"url": image.data_url, "detail": "high"}},
                ],
            },
        ],
        response_format={"type": "json_object"},
        model=settings.extract_model,
        attempt_seconds=settings.extract_model_timeout_seconds,
        max_tokens=8000,
        deadline=deadline,
    )
