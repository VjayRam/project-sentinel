from typing import Annotated

from config import settings
from pydantic import BaseModel, Field

# Single source of truth for the request-size cap: config.py's settings
# (env var MAX_BATCH_SIZE) — DynamicBatcher (batcher.py) reads the same
# settings.max_batch_size, so raising/lowering the env var now moves both
# the batcher's actual batching limit and this request-validation cap
# together instead of them silently drifting apart.
MAX_BATCH_SIZE = settings.max_batch_size


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


class ModerationResponse(BaseModel):
    id: str  # "modr-<hex>" — unique request ID
    model: str  # model version string
    results: list[ModerationResult]
