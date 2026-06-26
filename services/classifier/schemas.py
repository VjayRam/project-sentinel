from pydantic import BaseModel, Field

MAX_BATCH_SIZE = 64


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


class BatchClassifyResponse(BaseModel):
    results: list[ClassifyResult]
    latency_ms: float
    batch_size: int
    model_version: str
    inference_at: str
