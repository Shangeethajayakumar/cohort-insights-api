"""Explicit request and response contracts for the API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DocumentStatus = Literal["queued", "processing", "enriching", "completed", "failed"]
StageStatus = Literal["pending", "running", "retry_wait", "completed", "failed"]


class DocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    client_doc_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")

    @field_validator("title", "content")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Must contain non-whitespace characters")
        return value


class DocumentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=100_000)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Must contain non-whitespace characters")
        return value


class StageView(BaseModel):
    status: StageStatus
    attempts: int
    error: str | None
    result_version: int | None


class DocumentView(BaseModel):
    document_id: str
    user_id: str
    title: str
    content: str
    client_doc_ref: str | None
    content_version: int
    content_hash: str
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime
    stages: dict[str, StageView]
    results_current: bool
    is_stale: bool
    result_version: int | None
    summary: str | None
    tags: list[str] | None


class DocumentPage(BaseModel):
    page: int
    page_size: int
    items: list[DocumentView]


class CacheEntry(BaseModel):
    content_hash: str
    pipeline_version: str
    summary: str
    tags: list[str]
