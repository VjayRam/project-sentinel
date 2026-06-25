#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

echo "==========================================="
echo " SENTINEL — Phase 0 Setup Verification"
echo " Windows + NVIDIA GPU Edition"
echo "==========================================="
echo ""

# --- System ---
echo "--- System ---"
uname -r | grep -q "microsoft" && pass "Running inside WSL2" || warn "Not WSL2 — some steps may differ"
command -v git    >/dev/null && pass "git $(git --version | cut -d' ' -f3)"    || fail "git not found"
command -v curl   >/dev/null && pass "curl installed"                            || fail "curl not found"
command -v jq     >/dev/null && pass "jq installed"                              || fail "jq not found"
command -v make   >/dev/null && pass "make installed"                             || fail "make not found"

# --- GPU ---
echo ""
echo "--- NVIDIA GPU ---"
command -v nvidia-smi >/dev/null && pass "nvidia-smi accessible" || fail "nvidia-smi not found — check Windows NVIDIA driver"
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
[ -n "$GPU_NAME" ] && pass "GPU: $GPU_NAME" || warn "Could not read GPU name"
command -v nvcc >/dev/null && pass "CUDA toolkit: $(nvcc --version | grep release | awk '{print $6}')" || warn "CUDA toolkit not installed (optional for ONNX CPU path)"

# --- Python ---
echo ""
echo "--- Python ---"
command -v python >/dev/null && pass "python $(python --version 2>&1 | cut -d' ' -f2)" || fail "python not found"
python -c "import torch; print(f'  CUDA available: {torch.cuda.is_available()}')" 2>/dev/null
python -c "import torch"          2>/dev/null && pass "PyTorch installed"               || fail "PyTorch missing"
python -c "import torch; assert torch.cuda.is_available()" 2>/dev/null && pass "PyTorch CUDA works" || warn "PyTorch CUDA not available — GPU training won't work"
python -c "import onnxruntime"    2>/dev/null && pass "ONNX Runtime installed"          || fail "ONNX Runtime missing"
python -c "import onnxruntime as ort; assert 'CUDAExecutionProvider' in ort.get_available_providers()" 2>/dev/null && pass "ONNX Runtime GPU provider available" || warn "ONNX Runtime GPU provider missing — will use CPU (still fast)"
python -c "import transformers"   2>/dev/null && pass "Transformers installed"           || fail "Transformers missing"
python -c "import optimum"        2>/dev/null && pass "Optimum installed"                || fail "Optimum missing"
python -c "import fastapi"        2>/dev/null && pass "FastAPI installed"                || fail "FastAPI missing"
python -c "import pyspark"        2>/dev/null && pass "PySpark installed"                || fail "PySpark missing"
python -c "import mlflow"         2>/dev/null && pass "MLflow installed"                 || fail "MLflow missing"
python -c "from opentelemetry import trace" 2>/dev/null && pass "OpenTelemetry installed" || fail "OTel missing"
python -c "import prometheus_client"        2>/dev/null && pass "Prometheus client installed" || fail "prometheus_client missing"
python -c "import psycopg2"       2>/dev/null && pass "psycopg2 installed"              || fail "psycopg2 missing"
python -c "import pymongo"        2>/dev/null && pass "pymongo installed"               || fail "pymongo missing"

# --- Container Runtime ---
echo ""
echo "--- Containers ---"
command -v docker >/dev/null && pass "Docker $(docker --version | cut -d' ' -f3 | tr -d ',')" || fail "Docker not found"
docker info 2>/dev/null | grep -q "Operating System" && pass "Docker daemon running" || fail "Docker daemon not running — start Docker Desktop, then check: sudo usermod -aG docker \$USER && newgrp docker"
docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi >/dev/null 2>&1 && pass "Docker GPU passthrough works" || warn "Docker GPU passthrough failed — check NVIDIA Container Toolkit"

# --- Kubernetes ---
echo ""
echo "--- Kubernetes ---"
command -v kubectl  >/dev/null && pass "kubectl installed" || fail "kubectl not found"
command -v helm     >/dev/null && pass "helm $(helm version --short 2>/dev/null)" || fail "helm not found"
command -v k3d      >/dev/null && pass "k3d installed"     || fail "k3d not found"
kubectl cluster-info >/dev/null 2>&1 && pass "K8s cluster reachable" || fail "K8s cluster not reachable"
NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
[ "$NODE_COUNT" -ge 1 ] && pass "K8s nodes: $NODE_COUNT" || fail "No K8s nodes found"

# --- Terraform ---
echo ""
echo "--- Terraform ---"
command -v terraform >/dev/null && pass "terraform $(terraform --version -json 2>/dev/null | jq -r '.terraform_version' 2>/dev/null || echo 'installed')" || fail "terraform not found"

# --- Java ---
echo ""
echo "--- Java ---"
command -v java >/dev/null && pass "java $(java -version 2>&1 | head -1 | cut -d'"' -f2)" || fail "java not found"

# --- DB CLIs ---
echo ""
echo "--- Database CLIs ---"
command -v psql    >/dev/null && pass "psql installed"    || fail "psql not found"
command -v mongosh >/dev/null && pass "mongosh installed" || fail "mongosh not found"
command -v redis-cli >/dev/null && pass "redis-cli installed" || warn "redis-cli not found (optional)"

# --- Helm Repos ---
echo ""
echo "--- Helm Repos ---"
helm repo list 2>/dev/null | grep -q prometheus-community && pass "prometheus-community" || fail "Helm repo prometheus-community missing"
helm repo list 2>/dev/null | grep -q strimzi              && pass "strimzi"              || fail "Helm repo strimzi missing"
helm repo list 2>/dev/null | grep -q bitnami              && pass "bitnami"              || fail "Helm repo bitnami missing"
helm repo list 2>/dev/null | grep -q apache-airflow       && pass "apache-airflow"       || fail "Helm repo apache-airflow missing"
helm repo list 2>/dev/null | grep -q spark-operator       && pass "spark-operator"       || fail "Helm repo spark-operator missing"

# --- K8s Namespaces ---
echo ""
echo "--- K8s Namespaces ---"
kubectl get namespace sentinel-app         >/dev/null 2>&1 && pass "sentinel-app"        || fail "namespace sentinel-app missing"
kubectl get namespace sentinel-pipeline    >/dev/null 2>&1 && pass "sentinel-pipeline"   || fail "namespace sentinel-pipeline missing"
kubectl get namespace sentinel-data        >/dev/null 2>&1 && pass "sentinel-data"       || fail "namespace sentinel-data missing"
kubectl get namespace sentinel-monitoring  >/dev/null 2>&1 && pass "sentinel-monitoring" || fail "namespace sentinel-monitoring missing"

echo ""
echo "==========================================="
echo " All checks passed. Ready for Phase 1."
echo "==========================================="
