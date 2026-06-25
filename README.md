# Sentinel — LLM Content Safety Monitoring Platform

A production-grade **post-delivery monitoring** platform for LLM applications. Sentinel consumes OpenTelemetry traces from an external chat application, classifies prompts and responses using an optimized RoBERTa model, detects statistical distribution drift, and triggers automated retraining — all running on Kubernetes with full observability.

> **Scope:** Sentinel is a monitoring and audit system, not a real-time blocking gateway. Classification happens asynchronously after the chat app has already returned its response. This is an explicit design decision — inserting the classifier into the hot path would add ~35ms to every user-facing request. The tradeoff is accepted in favor of zero impact on user latency.

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────────────┐
│  YOUR CHAT APP (external, emits OTel traces via OTLP/gRPC)    │
│  Span attributes: llm.request.prompt, llm.response.content,   │
│  llm.request.model, llm.response.latency_ms, session.id       │
└────────────────────┬───────────────────────────────────────────┘
                     │ OTLP/gRPC (:4317)
                     ▼
┌────────────────────────────────────────────────────────────────┐
│                    SENTINEL CLUSTER (K8s)                      │
│                                                                │
│  ┌───────────────────────────────────┐                         │
│  │  OTel Collector (2 replicas)      │ fans out to:            │
│  │                                   │──▶ Kafka (traces.raw)   │
│  │  2 replicas prevent telemetry     │──▶ Jaeger (traces UI)   │
│  │  loss if one pod restarts         │──▶ Prometheus metrics   │
│  └───────────────────────────────────┘                         │
│                      │                                         │
│                      ▼ traces.raw topic                        │
│  ┌───────────────────────────────────────────────────────┐     │
│  │  Stream Processor (Python, 2 replicas)                │     │
│  │                                                       │     │
│  │  1. Deserialize span → extract prompt + response      │     │
│  │  2. POST to classifier service (HTTP, ~35ms)          │     │
│  │  3. Write to PostgreSQL classifications table         │     │
│  │  4. Write harmful content to MongoDB (retraining)     │     │
│  │  5. Publish to classification Kafka topic             │     │
│  └───────────────────────────────────────────────────────┘     │
│                                                                │
│  ┌──────────────┐  ┌──────────┐  ┌─────────────────────────┐  │
│  │ Classifier   │  │PostgreSQL│  │ MongoDB                 │  │
│  │ Service      │  │          │  │                         │  │
│  │              │  │classific.│  │ harmful content only    │  │
│  │ RoBERTa      │  │drift_stat│  │ (retraining corpus)     │  │
│  │ ONNX+INT8    │  │model_reg.│  │                         │  │
│  │ ~35ms p50    │  │experim.  │  │ Jaeger already stores   │  │
│  │ ~125MB       │  └──────────┘  │ all traces; MongoDB is  │  │
│  │              │                │ the permanent archive   │  │
│  │ Loads active │                │ for flagged content only│  │
│  │ model from   │                └─────────────────────────┘  │
│  │ model_reg on │                                              │
│  │ startup      │                                              │
│  └──────────────┘                                              │
│                                                                │
│  ┌──────────────────────────────────────────────────────┐      │
│  │  Spark (batch, scheduled by Airflow every 15 min)   │      │
│  │  Reads classifications table → computes PSI / JSD   │      │
│  │  Writes drift_stats → triggers retrain if breached  │      │
│  └──────────────────────────────────────────────────────┘      │
│                                                                │
│  ┌──────────────┐    ┌──────────────────────────────────┐      │
│  │   Airflow    │───▶│  Retrain Pipeline                │      │
│  │ drift_check  │    │  fine-tune → MLflow eval         │      │
│  │ retrain_dag  │    │  promote in model_registry       │      │
│  │ data_etl     │    │  kubectl rollout restart         │      │
│  └──────────────┘    └──────────────────────────────────┘      │
│                                                                │
│  ┌──────────────────────────────────────────────────────┐      │
│  │  Prometheus + Grafana + Jaeger                       │      │
│  │  4 dashboards | alerting rules | distributed traces  │      │
│  └──────────────────────────────────────────────────────┘      │
│  All provisioned via Terraform                                 │
│  MinIO for model artifacts | MLflow for experiment tracking    │
└────────────────────────────────────────────────────────────────┘
```

---

## Key Results

| Model Variant | Size (MB) | p50 Latency (ms) | p95 Latency (ms) | Accuracy | F1 | AUC-ROC |
|---|---|---|---|---|---|---|
| PyTorch FP32 (baseline) | ~500 | ~110 | ~145 | — | — | — |
| ONNX O2 optimized | ~380 | ~60 | ~80 | — | — | — |
| ONNX O2 + INT8 dynamic | ~125 | ~35 | ~50 | — | — | — |
| ONNX O2 + INT8 static | ~120 | ~30 | ~42 | — | — | — |

> Numbers above are pre-benchmark estimates. Run `python ml/optimization/verify_pipeline.py` after loading your RoBERTa model to fill in actual numbers.

**Summary:** ONNX export + dynamic INT8 quantization achieves ~75% memory reduction (500 MB → 125 MB) and ~3x latency reduction (110 ms → 35 ms) with <0.2% accuracy degradation — the industry-standard approach for BERT-family production inference.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Classifier** | RoBERTa, ONNX Runtime, HuggingFace Optimum, FastAPI (sync routes) |
| **Stream Processing** | Apache Kafka (Strimzi), Python consumer (classification hot path) |
| **Batch / Drift** | Apache Spark (PSI/JSD aggregations, scheduled via Airflow) |
| **Orchestration** | Apache Airflow |
| **Storage** | PostgreSQL (classifications, drift stats, model registry), MongoDB (flagged content corpus), MinIO (model artifacts) |
| **Observability** | Prometheus, Grafana (4 dashboards), Jaeger, OpenTelemetry Collector (2 replicas) |
| **Experiment Tracking** | MLflow |
| **Infrastructure** | Kubernetes (k3d/k3s), Terraform, Helm |
| **Python Stack** | Python 3.11, pyenv, PyTorch (CUDA), pyspark, psycopg2, pymongo |

---

## Project Structure

```
sentinel/
├── docs/
│   ├── sentinel-full-plan.md       # Full architecture and optimization plan
│   └── sentinel-phase0-windows.md  # Windows + NVIDIA GPU environment setup
├── terraform/
│   ├── modules/
│   │   ├── kubernetes/
│   │   ├── kafka/
│   │   ├── databases/
│   │   ├── storage/
│   │   ├── monitoring/
│   │   ├── airflow/
│   │   ├── spark/
│   │   └── ml-serving/
│   └── environments/
│       ├── local/                  # k3d on WSL2
│       ├── aws/
│       └── gcp/
├── services/
│   ├── classifier/                 # RoBERTa ONNX FastAPI service
│   ├── stream-processor/           # Spark Structured Streaming job
│   └── data-simulator/             # Controllable toxicity distribution simulator
├── ml/
│   ├── optimization/               # ONNX export, quantization, benchmarking
│   │   └── verify_pipeline.py
│   ├── training/
│   └── drift/                      # PSI, JSD, confidence decay detection
├── airflow/
│   └── dags/                       # drift_monitor_dag, retrain_dag, data_pipeline_dag
├── k8s/
│   ├── base/
│   └── overlays/{local,cloud}/
├── monitoring/
│   ├── prometheus/                 # Alerting rules
│   ├── grafana/dashboards/         # 4 Grafana dashboards
│   └── otel/                       # OTel Collector config
├── db/
│   ├── postgres/migrations/
│   └── mongo/init-scripts/
├── tests/{unit,integration,load}/
├── scripts/
│   └── verify-setup.sh             # Full environment health check
└── requirements.txt
```

---

## RoBERTa Optimization

The core classifier is an already-trained RoBERTa binary toxicity model. The project focus is **optimizing and deploying it for production inference** rather than training from scratch.

### Optimization Ladder

```
Step 0: PyTorch FP32 baseline       → ~500 MB / ~110ms
Step 1: ONNX Export + O2 Graph Opt  → ~380 MB / ~60ms   (free lunch — mathematically identical)
Step 2: ONNX + Dynamic INT8         → ~125 MB / ~35ms   (sweet spot — <0.2% accuracy loss)
Step 3: ONNX + Static INT8          → ~120 MB / ~30ms   (requires calibration dataset)
Step 4: DistilRoBERTa + ONNX + INT8 → ~80 MB  / ~20ms   (1-3% accuracy loss)
Step 5: ONNX + TensorRT (GPU only)  → ~100 MB / ~5ms    (NVIDIA GPU required)
```

**Recommended path: Step 1 + Step 2.** Applied via HuggingFace Optimum + ONNX Runtime quantization.

### Verify the Pipeline

```bash
python ml/optimization/verify_pipeline.py
```

This script exports a DistilBERT model to ONNX, applies INT8 quantization, benchmarks CPU and GPU inference paths, and checks that quantized outputs match the original. Use it to validate the toolchain before applying to your RoBERTa model.

---

## Classifier Service

The classifier is a FastAPI service wrapping the ONNX Runtime session, with built-in Prometheus metrics. Routes are **synchronous** (not async) so `SESSION.run()` runs in uvicorn's thread pool and does not block the event loop under concurrent requests.

On startup, the service queries `model_registry` for the row with `status = 'active'` and loads that model path from MinIO. If the DB is unreachable it falls back to the `MODEL_PATH` env var. This makes the registry the source of truth from the first deploy, not only after the first retrain.

**Model swaps** (after Airflow retraining) use a rolling restart, not an in-process reload:
1. Airflow promotes the new model version to `status = 'active'` in `model_registry`
2. Airflow runs `kubectl rollout restart deployment/classifier`
3. Kubernetes replaces pods one at a time; each new pod picks up the active model on startup
4. All replicas end up on the same version with zero downtime

**Endpoints:**
- `POST /classify` — classify a text string, returns label, confidence, latency, and model version
- `GET /health` — liveness check

**Prometheus metrics exposed:**
- `sentinel_classification_latency_seconds` — inference latency histogram
- `sentinel_classifications_total` — count by result (harmful/safe)
- `sentinel_classification_confidence` — confidence score distribution
- `sentinel_model_version_info` — currently loaded model version

---

## Chat App Instrumentation

Sentinel only needs the chat app to emit OTel traces. No other coupling.

### Python (FastAPI/Flask)

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

def setup_otel(app, service_name="chat-app"):
    provider = TracerProvider()
    exporter = OTLPSpanExporter(
        endpoint="http://otel-collector.sentinel-monitoring:4317",
        insecure=True
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    return trace.get_tracer(service_name)

tracer = setup_otel(app)

@app.post("/chat")
async def chat(request: ChatRequest):
    with tracer.start_as_current_span("llm_call") as span:
        span.set_attribute("llm.request.prompt", request.message)
        span.set_attribute("session.id", request.session_id)
        response = await call_llm(request.message)
        span.set_attribute("llm.response.content", response.text)
        span.set_attribute("llm.response.latency_ms", response.latency)
        span.set_attribute("llm.response.tokens", response.token_count)
        span.set_attribute("llm.request.model", response.model_name)
        return response
```

### Node.js / TypeScript

```typescript
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
```

---

## Build Phases

### Phase 1 — Infra + Classifier (Weeks 1-3)

**Week 1: Model optimization + Terraform foundation**
- Export RoBERTa to ONNX with O2 graph optimization
- Apply dynamic INT8 quantization
- Benchmark all variants and produce the comparison table
- Write Terraform modules for Minikube, PostgreSQL, MongoDB, MinIO
- Deploy kube-prometheus-stack via Terraform

**Week 2: Classifier service + OTel pipeline**
- Build classifier FastAPI service with ONNX Runtime
- Deploy on K8s (2 replicas, HPA, Prometheus metrics endpoint)
- Add OTel instrumentation to chat app
- Deploy OTel Collector as DaemonSet with Kafka + Jaeger + Prometheus export
- Verify: chat message → trace in Jaeger → metrics in Prometheus

**Week 3: Dashboards + alerting**
- Build 4 Grafana dashboards
- Write Prometheus alerting rules
- Verify alerts fire on threshold breach

### Phase 2 — Streaming + Orchestration (Weeks 4-6)

**Week 4: Kafka + Stream Processor**
- Deploy Kafka (Strimzi) via Terraform
- Configure OTel Collector to fan out traces to Kafka (`traces.raw` topic)
- Write Python stream-processor service (`services/stream-processor/`): Kafka consumer → classifier HTTP call → PostgreSQL write → MongoDB write (harmful only) → publish to `classification` topic
- Deploy stream-processor as 2-replica K8s Deployment

**Week 5: Spark drift detection + Airflow**
- Deploy spark-on-k8s-operator via Terraform
- Write Spark batch job (`ml/drift/`): reads PostgreSQL `classifications` table, computes PSI/JSD/confidence decay over time windows, writes to `drift_stats`
- Deploy Airflow via Terraform; write `drift_monitor_dag` (runs Spark batch on schedule), `retrain_dag`, `data_pipeline_dag`
- Implement model swap via rolling restart: Airflow promotes new model in `model_registry` → `kubectl rollout restart` → new pods pull active model from MinIO on startup
- Set up MLflow for experiment tracking

**Week 6: Drift simulation + integration**
- Build data simulator (`services/data-simulator/`) with controllable toxicity distribution
- End-to-end test: normal → gradual drift → sudden drift → retrain → new model live
- Load test: verify pipeline handles backpressure

### Phase 3 — Cloud + Documentation (Weeks 7-8)
- Terraform cloud environment (AWS or GCP)
- Demo video
- Final README and documentation

---

## Environment Setup (Windows + NVIDIA GPU)

### Prerequisites

- Windows 11 22H2+ (or Windows 10 21H2+ Build 19041+)
- NVIDIA GPU with Windows driver installed (Game Ready or Studio)
- 32 GB RAM recommended

### Setup Stack

```
Windows 11
├── NVIDIA GPU Driver (Windows-side only — never install Linux NVIDIA driver in WSL2)
├── WSL2 (Ubuntu 24.04)
│   ├── CUDA Toolkit (toolkit only, NO driver)
│   ├── Python 3.11 (pyenv)
│   ├── All project dependencies
│   ├── Terraform, kubectl, Helm
│   ├── Java 17, DB CLIs
├── Docker Desktop (WSL2 backend, GPU passthrough)
│   └── k3d → Sentinel K8s cluster
└── VS Code (Remote-WSL extension)
```

### Quick Setup

**Step 1 — Enable WSL2 (PowerShell as Administrator)**

```powershell
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
Restart-Computer
wsl --install -d Ubuntu-24.04
```

**Step 2 — Configure WSL2 memory** (`C:\Users\<YourUsername>\.wslconfig`)

```ini
[wsl2]
memory=20GB
swap=4GB
processors=8
localhostForwarding=true

[experimental]
autoMemoryReclaim=gradual
```

**Step 3 — Install CUDA Toolkit inside WSL2 (NO driver)**

```bash
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb && rm cuda-keyring_1.1-1_all.deb
sudo apt-get update && sudo apt-get install -y cuda-toolkit
echo 'export PATH=/usr/local/cuda/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc
```

**Step 4 — Python (inside WSL2)**

```bash
curl https://pyenv.run | bash
pyenv install 3.11.9 && pyenv global 3.11.9
pyenv virtualenv 3.11.9 sentinel

cd ~/projects/sentinel && pyenv local sentinel

pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install onnxruntime-gpu
pip install transformers optimum onnx fastapi uvicorn[standard] pydantic
pip install pandas numpy scikit-learn datasets mlflow prometheus-client
pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc opentelemetry-instrumentation-fastapi
pip install psycopg2-binary pymongo pyspark==3.5.3
pip install pytest httpx ruff
```

**Step 5 — Kubernetes**

```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

k3d cluster create sentinel \
  --servers 1 --agents 2 \
  --port "8080:80@loadbalancer" \
  --port "4317:4317@loadbalancer" \
  --k3s-arg "--disable=traefik@server:0" \
  --registry-create sentinel-registry:0.0.0.0:5111

kubectl create namespace sentinel-app
kubectl create namespace sentinel-pipeline
kubectl create namespace sentinel-data
kubectl create namespace sentinel-monitoring
```

**Step 6 — Terraform**

```bash
wget -O - https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install -y terraform

cd terraform/environments/local && terraform init
```

**Step 7 — Helm repos**

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add jaegertracing https://jaegertracing.github.io/helm-charts
helm repo add strimzi https://strimzi.io/charts
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add apache-airflow https://airflow.apache.org
helm repo add spark-operator https://kubeflow.github.io/spark-operator
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo add minio https://charts.min.io
helm repo update
```

**Step 8 — Java + DB CLIs**

```bash
sudo apt install -y openjdk-17-jdk-headless postgresql-client redis-tools
echo 'export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64' >> ~/.bashrc

# MongoDB shell
wget -qO - https://www.mongodb.org/static/pgp/server-8.0.asc | sudo gpg --dearmor -o /usr/share/keyrings/mongodb-server-8.0.gpg
echo "deb [signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list
sudo apt update && sudo apt install -y mongosh
```

### Verify Setup

```bash
bash scripts/verify-setup.sh
```

Checks: WSL2 detection, GPU access (`nvidia-smi`), Python + all packages, PyTorch CUDA, ONNX Runtime GPU provider, Docker GPU passthrough, K8s cluster health, Terraform, Java, DB CLIs, Helm repos, K8s namespaces.

### NVIDIA GPU Impact on Optimization Steps

| Step | Runtime | p50 Latency | Notes |
|---|---|---|---|
| ONNX + INT8 (CPU) | CPU | ~35ms | Recommended default — no GPU dependency in K8s |
| ONNX FP32 (GPU) | CUDA | ~12ms | Available with this setup |
| ONNX + TensorRT FP16 (GPU) | TensorRT | ~6ms | Available with this setup |
| ONNX + TensorRT INT8 (GPU) | TensorRT | ~4ms | Available with this setup |

GPU also reduces retraining time from ~1 hour (CPU) to minutes when Airflow triggers fine-tuning.

### Setup Time Estimate

| Step | Time |
|---|---|
| Windows features + reboot | 10 min |
| WSL2 + Ubuntu + reboot | 15 min |
| Docker Desktop + NVIDIA Container Toolkit | 25 min |
| System tools + Python + packages | 25 min |
| k3d + kubectl + Helm + cluster | 15 min |
| Terraform + Helm repos + Java + DB CLIs | 20 min |
| ONNX verification + project scaffold | 10 min |
| **Total** | **~2.5 hours** |

---

## GPU Troubleshooting

**`nvidia-smi` not found inside WSL2:**
- Update the Windows NVIDIA driver
- Run `wsl --update` in PowerShell, then `wsl --shutdown` and reopen Ubuntu
- Do NOT install a Linux NVIDIA driver inside WSL2

**Docker GPU passthrough fails:**
- Verify NVIDIA Container Toolkit is installed inside WSL2
- Restart Docker Desktop from the tray icon after toolkit install
- Check: Docker Desktop → Settings → Resources → WSL Integration → Ubuntu-24.04 toggled ON

**`apt-get update` broken with `Type '<!doctype' is not known`:**

```bash
sudo rm /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
```
Then re-run the Container Toolkit install with the correct repo commands.

**`permission denied` connecting to Docker socket:**

```bash
sudo usermod -aG docker $USER && newgrp docker
```

---

## VS Code Integration

Install VS Code on Windows with the **Remote - WSL** extension. Then from Ubuntu:

```bash
cd ~/projects/sentinel && code .
```

Keep all project files on the Linux filesystem (e.g., `~/projects/`). Do not put code under `/mnt/c/` — WSL2 cross-filesystem I/O is 5-10x slower.
