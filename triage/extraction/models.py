"""Typed extraction input and dynamic JSON output."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, StringConstraints


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document_id: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content: str = Field(min_length=1, repr=False)
    content_format: Literal["image_base64"]
    json_schema: str | dict[str, JsonValue] = Field(repr=False)


class ExtractionRecord(RootModel[dict[str, JsonValue]]):
    model_config = ConfigDict(strict=True)
