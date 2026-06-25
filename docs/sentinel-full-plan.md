# SENTINEL v2 — Modified Project Plan

## What Changed

Two things you already have eliminate two build phases:

1. **Chat application exists separately.** Sentinel doesn't build or manage it. Sentinel only receives its OTel traces. This simplifies the architecture — your chat app is a black box that emits telemetry.

2. **RoBERTa binary classifier is already trained.** The project focus shifts from "train a model" to "optimize and deploy a model for production inference" — which is a harder, more valuable skill to demonstrate.

The project is now: **instrument → collect → optimize → classify → monitor → detect drift → retrain → redeploy.**

---

## RoBERTa Optimization — The Decision Tree

You said: low memory footprint, low latency, virtually zero accuracy loss. Here's every viable technique ranked by that exact tradeoff.

### Optimization Ladder (apply in order, each stacks on the previous)

```
Step 0: PyTorch baseline
  └─ ~500 MB (roberta-base)
  └─ ~90-120 ms per inference (CPU, single input)
  └─ Accuracy: your baseline

Step 1: ONNX Export + Graph Optimization (O2)
  └─ ~380 MB
  └─ ~50-70 ms (operator fusion, constant folding, dead code elimination)
  └─ Accuracy loss: 0.0% (mathematically identical computation)
  └─ This is a free lunch. No reason not to do it.

Step 2: ONNX + Dynamic INT8 Quantization
  └─ ~125 MB (weights stored as INT8, computed as FP32)
  └─ ~30-45 ms
  └─ Accuracy loss: <0.2% for text classification (empirically verified across BERT-family models)
  └─ This is your sweet spot for "virtually zero accuracy loss."

Step 3: ONNX + Static INT8 Quantization (with calibration)
  └─ ~120 MB
  └─ ~25-35 ms (activations also quantized)
  └─ Accuracy loss: 0.3-0.8% depending on calibration quality
  └─ Requires a calibration dataset (100-500 representative samples)
  └─ Worth doing if Step 2 latency isn't low enough.

Step 4: Knowledge Distillation to DistilRoBERTa + ONNX + Quantization
  └─ ~80 MB
  └─ ~15-25 ms
  └─ Accuracy loss: 1-3% (depends on distillation quality)
  └─ This crosses the "virtually zero" line. Do it only if you need <20ms.

Step 5: ONNX + TensorRT (GPU only)
  └─ ~100 MB
  └─ ~5-10 ms on GPU
  └─ Accuracy loss: <0.1% (FP16) or <0.5% (INT8)
  └─ Only relevant if deploying on GPU.
```

### Recommended path: Step 1 + Step 2

ONNX export with O2 graph optimization + dynamic INT8 quantization gives you:
- **~75% memory reduction** (500 MB → 125 MB)
- **~3x latency reduction** (110 ms → 35 ms)
- **<0.2% accuracy drop** (measure this on your test set and report the exact number)

This is the industry-standard approach. Benchmarks on BERT-family models show ONNX optimized models go from 420 MB / 110 ms to 108 MB / 42 ms with quantization, with accuracy dropping from 91.0% to 90.8%. ProtectAI's toxic-roberta-onnx is a direct example — a RoBERTa toxicity model converted to ONNX format for faster CPU inference, used in production for LLM Guard.

### Implementation

```python
# Step 1: Export to ONNX with graph optimization
# Using HuggingFace Optimum (the clean way)
from optimum.onnxruntime import ORTModelForSequenceClassification
from optimum.onnxruntime.configuration import OptimizationConfig
from optimum.onnxruntime import ORTOptimizer

# Export
model = ORTModelForSequenceClassification.from_pretrained(
    "your-roberta-model-path",
    export=True
)
model.save_pretrained("onnx_model/")

# Optimize (O2: general + transformer-specific fusions)
optimizer = ORTOptimizer.from_pretrained(model)
optimization_config = OptimizationConfig(
    optimization_level=2,
    enable_transformers_specific_optimizations=True,
    optimize_for_gpu=False
)
optimizer.optimize(
    save_dir="onnx_optimized/",
    optimization_config=optimization_config
)

# Step 2: Dynamic INT8 quantization
from onnxruntime.quantization import quantize_dynamic, QuantType

quantize_dynamic(
    model_input="onnx_optimized/model.onnx",
    model_output="onnx_quantized/model.onnx",
    weight_type=QuantType.QInt8,
    extra_options={"MatMulConstBOnly": True}
)

# Step 3: Benchmark — MEASURE, don't guess
import onnxruntime as ort
import time
import numpy as np

session = ort.InferenceSession(
    "onnx_quantized/model.onnx",
    providers=["CPUExecutionProvider"],
    sess_options=_get_session_options()
)

def _get_session_options():
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = 4      # match your CPU cores
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return so

# Warmup + benchmark
for _ in range(10):
    session.run(None, dummy_input)

latencies = []
for _ in range(100):
    start = time.perf_counter()
    session.run(None, real_input)
    latencies.append((time.perf_counter() - start) * 1000)

print(f"p50: {np.percentile(latencies, 50):.1f}ms")
print(f"p95: {np.percentile(latencies, 95):.1f}ms")
print(f"p99: {np.percentile(latencies, 99):.1f}ms")
```

### What to report in your README (and interviews)

Create a benchmark table like this and include the actual numbers:

| Model Variant | Size (MB) | p50 Latency (ms) | p95 Latency (ms) | Accuracy | F1 | AUC-ROC |
|--------------|-----------|-------------------|-------------------|----------|-----|---------|
| PyTorch FP32 (baseline) | ? | ? | ? | ? | ? | ? |
| ONNX O2 optimized | ? | ? | ? | ? | ? | ? |
| ONNX O2 + INT8 dynamic | ? | ? | ? | ? | ? | ? |
| ONNX O2 + INT8 static | ? | ? | ? | ? | ? | ? |

Fill in real numbers from your hardware. This table alone is an interview conversation starter.

### Session options that matter for latency

```python
so = ort.SessionOptions()

# Graph optimization — fuse ops, eliminate dead code
so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

# Threading — tune to your CPU
so.intra_op_num_threads = 4          # parallelism within a single op (matmul)
so.inter_op_num_threads = 1          # parallelism across ops (keep at 1 for sequential models)
so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL  # no inter-op parallelism needed

# Memory — reduce allocation overhead
so.enable_mem_pattern = True         # reuse memory across ops
so.enable_mem_reuse = True
so.enable_cpu_mem_arena = True       # pre-allocate memory pool
```

---

## Modified Architecture — Chat App as External Black Box

```
┌────────────────────────────────────────────────────────────────┐
│  YOUR CHAT APP (runs separately, you control the code)        │
│                                                                │
│  Add: OTel SDK instrumentation (Python/JS/whatever it's in)   │
│  Emits: OTLP traces to OTel Collector endpoint                │
│                                                                │
│  Span attributes you need to emit:                             │
│    llm.request.prompt        — user input text                 │
│    llm.response.content      — model output text               │
│    llm.request.model         — which model was called          │
│    llm.response.latency_ms   — inference time                  │
│    llm.response.tokens       — token count                     │
│    session.id                — conversation session            │
└────────────────────┬───────────────────────────────────────────┘
                     │ OTLP/gRPC (:4317)
                     ▼
┌────────────────────────────────────────────────────────────────┐
│                    SENTINEL CLUSTER (K8s)                       │
│                                                                │
│  ┌───────────────────────────────────┐                         │
│  │  OTel Collector (2 replicas)      │  Receives traces:       │
│  │                                   │──┬─▶ Kafka (traces.raw) │
│  │  2 replicas — no single point     │  ├─▶ Jaeger (traces UI) │
│  │  of failure for telemetry         │  └─▶ Prometheus metrics │
│  └───────────────────────────────────┘                         │
│           │                                                    │
│           ▼                                                    │
│  ┌──────────────────┐                                          │
│  │  Kafka            │                                         │
│  │                   │                                         │
│  │  Topics:          │                                         │
│  │   traces.raw ─────┼──▶ Stream Processor (Python)           │
│  │   classification  │    1. Deserialize spans                 │
│  │   drift.alerts    │    2. Extract prompt + response         │
│  │   retrain.events  │    3. Call classifier (HTTP)            │
│  └──────────────────┘    4. Write to PostgreSQL + MongoDB      │
│           │               5. Publish to classification topic   │
│           │                                                    │
│           ▼ (classification topic, 15-min batch schedule)      │
│  ┌──────────────────────────────────────────────────────┐      │
│  │  Spark (batch, via Airflow schedule)                 │      │
│  │  Reads PostgreSQL classifications table              │      │
│  │  Computes PSI / JSD / confidence decay over windows │      │
│  │  Writes to drift_stats — triggers retrain if breach │      │
│  └──────────────────────────────────────────────────────┘      │
│                                                                │
│           ┌─────────────┐ ┌──────────┐ ┌─────────────┐        │
│           │ PostgreSQL  │ │ MongoDB  │ │ Classifier  │        │
│           │             │ │          │ │ Service     │        │
│           │ classific.  │ │ flagged  │ │             │        │
│           │ drift_stats │ │ content  │ │ RoBERTa     │        │
│           │ model_reg.  │ │ (for     │ │ ONNX+INT8   │        │
│           │ experiments │ │ retrain) │ │ FastAPI     │        │
│           └─────────────┘ └──────────┘ │ ~35ms p50   │        │
│                    │                    │ model from  │        │
│                    ▼                    │ MinIO init  │        │
│           ┌──────────────┐              └─────────────┘        │
│           │   Airflow    │──▶ Retrain Pipeline                 │
│           │              │    drift-triggered fine-tuning       │
│           │ drift_check  │    A/B eval via MLflow               │
│           │ retrain_dag  │    rolling restart (no /reload)      │
│           │ data_etl     │    kubectl rollout restart           │
│           └──────────────┘                                     │
│                                                                │
│  ┌──────────────────────────────────────────────────────┐      │
│  │  Prometheus + Grafana + Jaeger                       │      │
│  │  4 dashboards | alerting rules | distributed traces  │      │
│  └──────────────────────────────────────────────────────┘      │
│                                                                │
│  All provisioned via Terraform                                 │
│  MinIO for model artifacts | MLflow for experiment tracking    │
└────────────────────────────────────────────────────────────────┘
```

---

## What You Need to Add to Your Chat App (Minimal Instrumentation)

This is the only code change to your existing app. Everything else is Sentinel-side.

### If your chat app is Python (FastAPI/Flask):

```python
# otel_setup.py — add this file to your chat app
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

def setup_otel(app, service_name="chat-app"):
    provider = TracerProvider()
    
    # Export to Sentinel's OTel Collector
    exporter = OTLPSpanExporter(
        endpoint="http://otel-collector.sentinel-monitoring:4317",
        insecure=True
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    
    # Auto-instrument FastAPI (captures HTTP spans automatically)
    FastAPIInstrumentor.instrument_app(app)
    
    return trace.get_tracer(service_name)

# In your LLM call handler:
tracer = setup_otel(app)

@app.post("/chat")
async def chat(request: ChatRequest):
    with tracer.start_as_current_span("llm_call") as span:
        span.set_attribute("llm.request.prompt", request.message)
        span.set_attribute("session.id", request.session_id)
        
        response = await call_llm(request.message)  # your existing logic
        
        span.set_attribute("llm.response.content", response.text)
        span.set_attribute("llm.response.latency_ms", response.latency)
        span.set_attribute("llm.response.tokens", response.token_count)
        span.set_attribute("llm.request.model", response.model_name)
        
        return response
```

### If your chat app is Node.js/TypeScript:

```typescript
// otel-setup.ts
import { NodeSDK } from '@opentelemetry/sdk-node';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-grpc';
import { getNodeAutoInstrumentations } from '@opentelemetry/auto-instrumentations-node';

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: 'http://otel-collector.sentinel-monitoring:4317',
  }),
  instrumentations: [getNodeAutoInstrumentations()],
  serviceName: 'chat-app',
});

sdk.start();

// In your LLM handler, add span attributes:
import { trace } from '@opentelemetry/api';
const tracer = trace.getTracer('chat-app');

const span = tracer.startSpan('llm_call');
span.setAttribute('llm.request.prompt', userMessage);
span.setAttribute('llm.response.content', llmResponse);
span.setAttribute('llm.response.latency_ms', latency);
span.end();
```

That's it. Your chat app now emits traces. Sentinel handles everything else.

---

## OTel Collector Config (Sentinel-Side)

```yaml
# otel-collector-config.yml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 5s
    send_batch_size: 256

connectors:
  spanmetrics:
    histogram:
      explicit:
        boundaries: [10, 25, 50, 100, 250, 500, 1000, 2500]
    dimensions:
      - name: llm.request.model
      - name: session.id
    namespace: llm

exporters:
  kafka:
    protocol_version: "2.0.0"
    brokers:
      - kafka.sentinel-data:9092
    topic: traces.raw
    encoding: otlp_json
  
  jaeger:
    endpoint: jaeger.sentinel-monitoring:14250
    tls:
      insecure: true
  
  prometheus:
    endpoint: 0.0.0.0:8889
    namespace: sentinel

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [kafka, jaeger]
    
    metrics:
      receivers: [spanmetrics]
      exporters: [prometheus]
```

---

## Classifier Service — Optimized Deployment

```python
# services/classifier/main.py
import onnxruntime as ort
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoTokenizer
from prometheus_client import Histogram, Counter, Gauge
import numpy as np
import time

app = FastAPI()

# --- Prometheus metrics ---
INFERENCE_LATENCY = Histogram(
    "sentinel_classification_latency_seconds",
    "Toxicity classification inference latency",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5]
)
CLASSIFICATION_TOTAL = Counter(
    "sentinel_classifications_total",
    "Total classifications performed",
    ["result"]  # harmful / safe
)
MODEL_CONFIDENCE = Histogram(
    "sentinel_classification_confidence",
    "Model confidence score distribution",
    buckets=[0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99]
)
MODEL_VERSION = Gauge(
    "sentinel_model_version_info",
    "Currently loaded model version",
    ["version"]
)

# --- Model loading ---
TOKENIZER = AutoTokenizer.from_pretrained("/models/tokenizer")

def create_session(model_path: str) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.enable_mem_pattern = True
    so.enable_cpu_mem_arena = True
    return ort.InferenceSession(
        model_path,
        sess_options=so,
        providers=["CPUExecutionProvider"]
    )

SESSION = create_session("/models/onnx_quantized/model.onnx")
CURRENT_VERSION = "v1"
MODEL_VERSION.labels(version=CURRENT_VERSION).set(1)

# --- Inference ---
class ClassifyRequest(BaseModel):
    text: str
    trace_id: str | None = None

class ClassifyResponse(BaseModel):
    label: str           # "harmful" or "safe"
    confidence: float
    latency_ms: float
    model_version: str

@app.post("/classify", response_model=ClassifyResponse)
async def classify(request: ClassifyRequest):
    # Tokenize
    inputs = TOKENIZER(
        request.text,
        return_tensors="np",
        truncation=True,
        max_length=512,
        padding="max_length"     # static shape = more consistent latency
    )
    
    ort_inputs = {
        "input_ids": inputs["input_ids"].astype(np.int64),
        "attention_mask": inputs["attention_mask"].astype(np.int64),
    }
    
    # Inference with timing
    start = time.perf_counter()
    outputs = SESSION.run(None, ort_inputs)
    latency = (time.perf_counter() - start) * 1000
    
    # Softmax
    logits = outputs[0][0]
    probs = np.exp(logits) / np.sum(np.exp(logits))
    
    predicted_class = int(np.argmax(probs))
    confidence = float(probs[predicted_class])
    label = "harmful" if predicted_class == 1 else "safe"
    
    # Record metrics
    INFERENCE_LATENCY.observe(latency / 1000)
    CLASSIFICATION_TOTAL.labels(result=label).inc()
    MODEL_CONFIDENCE.observe(confidence)
    
    return ClassifyResponse(
        label=label,
        confidence=confidence,
        latency_ms=round(latency, 2),
        model_version=CURRENT_VERSION
    )

# --- Health check ---
# Model swaps are done via rolling restart (kubectl rollout restart deployment/classifier).
# Each new pod queries model_registry on startup and loads the active model from MinIO.
# There is no /reload endpoint — in-process reload would only hit one pod out of N replicas,
# causing a silent model version split that's undetectable from the Service level.
@app.get("/health")
def health():
    return {"status": "ok", "model_version": CURRENT_VERSION}
```

---

## Modified Build Phases

### Phase 1: Infra + Optimized Classifier Deployment (Week 1-3)

**Week 1: Model optimization + Terraform foundation**
1. Export your RoBERTa to ONNX using HuggingFace Optimum
2. Apply O2 graph optimization
3. Apply dynamic INT8 quantization
4. Benchmark all three variants (PyTorch vs ONNX vs ONNX+INT8), produce the comparison table
5. Write Terraform modules for Minikube, PostgreSQL, MongoDB, MinIO
6. Deploy kube-prometheus-stack via Terraform

**Week 2: Classifier service + OTel pipeline**
1. Build the classifier FastAPI service with ONNX Runtime (code above)
2. Deploy on K8s with 2 replicas, HPA, Prometheus metrics endpoint
3. Add OTel instrumentation to your chat app (minimal — see code above)
4. Deploy OTel Collector as DaemonSet with multi-backend export config
5. Deploy Jaeger
6. Verify: Chat app message → trace in Jaeger → OTel metrics in Prometheus

**Week 3: Dashboards + alerting**
1. Build 4 Grafana dashboards
2. Write Prometheus alerting rules
3. Verify: Everything visible, alerts fire on threshold breach

### Phase 2: Streaming + Orchestration (Week 4-6)

**Week 4: Kafka + Stream Processor**
1. Deploy Kafka (Strimzi) via Terraform
2. Configure OTel Collector to export traces to Kafka (traces.raw topic)
3. Write the Python stream-processor service (services/stream-processor/):
   - Kafka consumer on traces.raw
   - Extract prompt + response from span attributes
   - POST to classifier service
   - Write classification result to PostgreSQL classifications table
   - Write flagged (harmful) content to MongoDB for retraining corpus
   - Publish result to classification Kafka topic
4. Deploy stream-processor as a K8s Deployment (2 replicas)

**Week 5: Spark drift detection + Airflow**
1. Deploy spark-on-k8s-operator via Terraform
2. Write Spark batch job (ml/drift/):
   - Reads PostgreSQL classifications table
   - Computes PSI, JSD, confidence decay over configurable time windows
   - Writes results to drift_stats table
   - Publishes to drift.alerts topic if threshold breached
3. Deploy Airflow via Terraform
4. Write DAGs: drift_monitor_dag (runs Spark batch on schedule), retrain_dag, data_pipeline_dag
5. Implement model swap via rolling restart:
   - Airflow calls optimize.py → writes staging row to model_registry
   - Airflow evaluates on hold-out set, promotes to active via UPDATE
   - Airflow runs: kubectl rollout restart deployment/classifier
   - New pods start up, query model_registry, pull active model from MinIO
6. Set up MLflow for experiment tracking

**Week 6: Drift simulation + integration**
1. Build data simulator (services/data-simulator/) with controllable toxicity distribution
2. Test: normal → gradual drift → sudden drift → retrain → new model live
3. End-to-end trace in Jaeger from chat message through classification
4. Load test: verify pipeline handles backpressure

### Phase 3: Cloud + Documentation (Week 7-8)
Same as before — Terraform cloud env, demo video, README.

---

## Key Resume Bullets This Produces

After completing this project, your resume Projects section gains:

**Sentinel — LLM Content Safety Monitoring Platform** | [GitHub link]
*Python | FastAPI | ONNX Runtime | Terraform | Kubernetes | Kafka | Spark | Airflow | Prometheus | Grafana | OpenTelemetry*

- Optimized RoBERTa toxicity classifier for production inference using ONNX Runtime with INT8 dynamic quantization — reducing model size by 75% (500→125 MB) and p95 latency by 3x (110→38 ms) with <0.2% accuracy degradation.
- Built post-delivery LLM content monitoring pipeline: Python Kafka consumer classifies ~X req/sec via ONNX classifier; Spark batch job computes PSI/JSD drift metrics over sliding windows and triggers automated retraining when distribution shift is detected.
- Provisioned end-to-end infrastructure on Kubernetes using Terraform (11 services across 4 namespaces), with Prometheus custom metrics, 4 Grafana dashboards, and OTel distributed tracing via Jaeger.
- Orchestrated automated model retraining with Airflow — drift-triggered fine-tuning pipeline with A/B evaluation via MLflow and zero-downtime blue/green K8s deployment.

Your skills section gains:

**Infrastructure & Observability:** Terraform, Kubernetes (Helm, HPA, blue/green), Prometheus, Grafana, OpenTelemetry, Jaeger, Apache Kafka, Apache Spark, Apache Airflow

**Model Optimization:** ONNX Runtime, INT8 quantization, HuggingFace Optimum
