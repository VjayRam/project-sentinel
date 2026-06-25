import os
import time

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app
from pydantic import BaseModel
from transformers import AutoTokenizer

app = FastAPI(title="Sentinel Classifier")

# Prometheus metrics
INFERENCE_LATENCY = Histogram(
    "sentinel_classification_latency_seconds",
    "Toxicity classification inference latency",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)
CLASSIFICATION_TOTAL = Counter(
    "sentinel_classifications_total",
    "Total classifications performed",
    ["result"],
)
MODEL_CONFIDENCE = Histogram(
    "sentinel_classification_confidence",
    "Model confidence score distribution",
    buckets=[0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99],
)
MODEL_VERSION = Gauge(
    "sentinel_model_version_info",
    "Currently loaded model version",
    ["version"],
)

app.mount("/metrics", make_asgi_app())

TOKENIZER_PATH = os.getenv("TOKENIZER_PATH", "/models/tokenizer")
MODEL_PATH = os.getenv("MODEL_PATH", "/models/onnx_quantized/model.onnx")

TOKENIZER = AutoTokenizer.from_pretrained(TOKENIZER_PATH)


def create_session(model_path: str) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.enable_mem_pattern = True
    so.enable_cpu_mem_arena = True
    return ort.InferenceSession(model_path, sess_options=so, providers=["CPUExecutionProvider"])


SESSION = create_session(MODEL_PATH)
CURRENT_VERSION = os.getenv("MODEL_VERSION", "v1")
MODEL_VERSION.labels(version=CURRENT_VERSION).set(1)


class ClassifyRequest(BaseModel):
    text: str
    trace_id: str | None = None


class ClassifyResponse(BaseModel):
    label: str
    confidence: float
    latency_ms: float
    model_version: str


@app.post("/classify", response_model=ClassifyResponse)
async def classify(request: ClassifyRequest):
    inputs = TOKENIZER(
        request.text,
        return_tensors="np",
        truncation=True,
        max_length=512,
        padding="max_length",
    )
    ort_inputs = {
        "input_ids": inputs["input_ids"].astype(np.int64),
        "attention_mask": inputs["attention_mask"].astype(np.int64),
    }

    start = time.perf_counter()
    outputs = SESSION.run(None, ort_inputs)
    latency = (time.perf_counter() - start) * 1000

    logits = outputs[0][0]
    probs = np.exp(logits) / np.sum(np.exp(logits))
    predicted_class = int(np.argmax(probs))
    confidence = float(probs[predicted_class])
    label = "harmful" if predicted_class == 1 else "safe"

    INFERENCE_LATENCY.observe(latency / 1000)
    CLASSIFICATION_TOTAL.labels(result=label).inc()
    MODEL_CONFIDENCE.observe(confidence)

    return ClassifyResponse(
        label=label,
        confidence=confidence,
        latency_ms=round(latency, 2),
        model_version=CURRENT_VERSION,
    )


@app.post("/reload")
async def reload_model(version: str, model_path: str):
    global SESSION, CURRENT_VERSION
    SESSION = create_session(model_path)
    CURRENT_VERSION = version
    MODEL_VERSION.labels(version=version).set(1)
    return {"status": "reloaded", "version": version}


@app.get("/health")
async def health():
    return {"status": "ok", "model_version": CURRENT_VERSION}
