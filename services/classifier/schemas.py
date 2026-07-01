from typing import Annotated

from pydantic import BaseModel, Field

MAX_BATCH_SIZE = 64


# ── Internal classify API (kept for direct testing / backwards compat) ─────────


class ClassifyRequest(BaseModel):
    text: str


class ClassifyResult(BaseModel):
    label: str
    score: float


class ClassifyResponse(ClassifyResult):
    latency_ms: float
    model_version: str
    inference_at: str


class BatchClassifyRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH_SIZE)
    persist: bool = True  # False skips classifier's own PG write; stream processor handles it


class BatchClassifyResponse(BaseModel):
    results: list[ClassifyResult]
    latency_ms: float
    batch_size: int
    model_version: str
    inference_at: str


# ── OpenAI Moderation API-compatible types ─────────────────────────────────────
# POST /v1/moderations — primary public endpoint


class ModerationCategories(BaseModel):
    harm: bool


class ModerationCategoryScores(BaseModel):
    harm: float


class ModerationResult(BaseModel):
    flagged: bool
    categories: ModerationCategories
    category_scores: ModerationCategoryScores


class ModerationRequest(BaseModel):
    input: str | Annotated[list[str], Field(min_length=1, max_length=MAX_BATCH_SIZE)]
    # Sentinel extension: set False to skip classifier-side PG write.
    # Not in the OpenAI spec — used internally by the stream processor.
    persist: bool = True


class ModerationResponse(BaseModel):
    id: str  # "modr-<hex>" — unique request ID
    model: str  # model version string
    results: list[ModerationResult]
