# SENTINEL — Phase 0: Windows + NVIDIA GPU Setup

## The Setup Stack

```
Windows 11
├── NVIDIA GPU Driver (Windows-side, the ONLY driver you install)
├── WSL2 (Ubuntu 24.04)
│   ├── CUDA Toolkit (Linux-side, NO driver — WSL shares Windows driver)
│   ├── Python 3.11 (pyenv)
│   ├── All project dependencies
│   ├── Terraform
│   ├── kubectl, Helm
│   ├── Java 17
│   └── DB CLIs
├── Docker Desktop (WSL2 backend, GPU passthrough enabled)
│   └── k3d (k3s-in-Docker)
│       └── Sentinel K8s cluster
└── VS Code (with Remote-WSL extension)
```

Everything runs inside WSL2. You never install Python, Terraform, or project tools on the Windows side. Docker Desktop bridges Windows ↔ WSL2 and passes your NVIDIA GPU through to containers.

---

## Step 1: Windows Prerequisites

### 1a. Windows Update

Open **Settings → Windows Update → Check for updates**. Install everything. You need Windows 11 22H2+ or Windows 10 21H2+ (Build 19041+).

```powershell
# Verify in PowerShell (run as Administrator)
winver
# Should show Version 22H2 or later
```

### 1b. Enable Windows Features

Open PowerShell as **Administrator**:

```powershell
# Enable WSL and Virtual Machine Platform
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart

# Restart your machine
Restart-Computer
```

### 1c. Install NVIDIA GPU Driver (Windows-Side)

This is the ONLY NVIDIA driver you install. Do NOT install a Linux NVIDIA driver inside WSL2 — it will break GPU passthrough.

1. Go to https://www.nvidia.com/download/index.aspx
2. Select your GPU model, Windows 11, download the **Game Ready** or **Studio** driver
3. Install it
4. Reboot

Verify in PowerShell:

```powershell
nvidia-smi
```

You should see your GPU listed with a CUDA version (12.x). If `nvidia-smi` works on Windows, the GPU will automatically be available inside WSL2.

---

## Step 2: WSL2 + Ubuntu

### 2a. Install WSL2 and Ubuntu

Open PowerShell as **Administrator**:

```powershell
# Install WSL2 with Ubuntu (one command does everything)
wsl --install -d Ubuntu-24.04

# This installs:
#   - WSL2 kernel
#   - Ubuntu 24.04 distribution
#   - Sets WSL2 as default version

# Restart when prompted
Restart-Computer
```

After reboot, Ubuntu will launch automatically and ask you to create a username and password. Remember these — you'll use them for `sudo`.

```powershell
# Verify WSL2 is running (in PowerShell)
wsl --list --verbose

# Should show:
#   NAME            STATE     VERSION
#   Ubuntu-24.04    Running   2       <-- must be VERSION 2
```

### 2b. Configure WSL2 Memory

By default WSL2 takes up to 50% of your RAM. With 32GB, set it explicitly.

Create/edit `C:\Users\<YourUsername>\.wslconfig` in Notepad:

```ini
[wsl2]
memory=20GB
swap=4GB
processors=8
localhostForwarding=true

[experimental]
autoMemoryReclaim=gradual
```

This gives WSL2 20GB (plenty for the full stack) while leaving 12GB for Windows, your browser, and VS Code.

```powershell
# Restart WSL to apply
wsl --shutdown
wsl
```

### 2c. Verify GPU Inside WSL2

Open Ubuntu terminal (search "Ubuntu" in Start menu):

```bash
nvidia-smi
```

You should see the same GPU output as on the Windows side. The driver version might show slightly differently — that's normal. The key thing is your GPU appears.

**Critical:** If `nvidia-smi` doesn't work inside WSL2, do NOT install an NVIDIA driver inside Linux. Instead:
1. Make sure your Windows NVIDIA driver is up to date
2. Run `wsl --update` in PowerShell
3. Restart WSL: `wsl --shutdown` then reopen Ubuntu

### 2d. Install CUDA Toolkit Inside WSL2 (NO driver)

Inside Ubuntu terminal:

```bash
# Remove the old GPG key if present
sudo apt-key del 7fa2af80 2>/dev/null

# Add NVIDIA CUDA repository for WSL-Ubuntu
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
rm cuda-keyring_1.1-1_all.deb

# Install CUDA toolkit ONLY (not the driver!)
sudo apt-get update
sudo apt-get install -y cuda-toolkit

# Add to PATH
echo 'export PATH=/usr/local/cuda/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc

# Verify
nvcc --version      # Should show CUDA compilation tools
nvidia-smi           # Should still work (using Windows driver)
```

---

## Step 3: Docker Desktop with GPU Support

### 3a. Install Docker Desktop

1. Download Docker Desktop for Windows from https://www.docker.com/products/docker-desktop/
2. Run the installer
3. **During installation, make sure "Use WSL 2 instead of Hyper-V" is checked**
4. Restart when prompted

### 3b. Configure Docker Desktop

Open Docker Desktop → **Settings**:

**General:**
- ✅ Use the WSL 2 based engine (must be checked)
- ✅ Start Docker Desktop when you log in (optional)

**Resources → WSL Integration:**
- ✅ Enable integration with my default WSL distro
- ✅ Ubuntu-24.04 (toggle ON)
- Click **Apply & restart**

**Resources → Advanced:**
- Memory: Leave as default (Docker shares WSL2's memory allocation)
- CPUs: Leave as default

### 3c. Verify Docker in WSL2

Open Ubuntu terminal:

```bash
# Docker should work directly inside WSL2
docker --version
docker run hello-world
```

If you get `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`:

```bash
# Fix 1: Add your user to the docker group
sudo usermod -aG docker $USER
newgrp docker

# Fix 2: If Fix 1 doesn't work, check Docker Desktop:
#   Docker Desktop → Settings → Resources → WSL Integration
#   → Toggle ON for Ubuntu-24.04 → Apply & restart
#   → Close and reopen your Ubuntu terminal

# Verify the fix worked
docker run hello-world
```

Once `hello-world` works, verify GPU passthrough:

```bash
# Verify GPU passthrough to Docker containers
docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi
```

The last command should show your GPU inside the container. If you see your GPU name and CUDA version, GPU passthrough is working.

### 3d. Install NVIDIA Container Toolkit (inside WSL2)

This ensures `--gpus` flag works properly:

```bash
# Add the GPG key
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

# Add the repository (generic stable/deb path — NOT distribution-specific)
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# Install
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# Configure Docker to use the toolkit
sudo nvidia-ctk runtime configure --runtime=docker

# Restart Docker Desktop from Windows (right-click tray icon → Restart)
# Then verify
docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi
```

**If you previously ran the old command** and your `apt-get update` is broken with `Type '<!doctype' is not known`, run this first:

```bash
sudo rm /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
# Then run the correct commands above
```

---

## Step 4: System Tools (Inside WSL2)

Everything from here runs inside your Ubuntu terminal.

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Core CLI tools
sudo apt install -y \
  git curl wget jq make gcc build-essential \
  unzip zip software-properties-common \
  apt-transport-https ca-certificates gnupg lsb-release
  
# Install yq
sudo wget -qO /usr/local/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64
sudo chmod +x /usr/local/bin/yq

# Verify
git --version
jq --version
```

---

## Step 5: Python Environment (Inside WSL2)

```bash
# Install pyenv dependencies
sudo apt install -y \
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
  libffi-dev liblzma-dev

# Install pyenv
curl https://pyenv.run | bash

# Add to shell profile
cat >> ~/.bashrc << 'SHELLRC'
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
eval "$(pyenv virtualenv-init -)"
SHELLRC

source ~/.bashrc

# Install Python 3.11 and create project virtualenv
pyenv install 3.11.9
pyenv global 3.11.9
pyenv virtualenv 3.11.9 sentinel

python --version     # 3.11.9
```

### Create project and install dependencies

```bash
mkdir -p ~/projects/sentinel && cd ~/projects/sentinel
pyenv local sentinel

# Core ML & serving — GPU-enabled PyTorch
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

# ONNX Runtime with GPU support (also includes CPU provider)
pip install onnxruntime-gpu

# The rest
pip install transformers optimum onnx
pip install fastapi uvicorn[standard] pydantic
pip install pandas numpy scikit-learn datasets
pip install mlflow
pip install prometheus-client
pip install opentelemetry-api opentelemetry-sdk \
    opentelemetry-exporter-otlp-proto-grpc \
    opentelemetry-instrumentation-fastapi
pip install psycopg2-binary pymongo
pip install pyspark==3.5.3
pip install pytest httpx ruff

pip freeze > requirements.txt
```

Note the two GPU-specific differences from the macOS guide:
- `torch` uses `cu124` index (CUDA 12.4) instead of `cpu`
- `onnxruntime-gpu` instead of `onnxruntime` — this gives you both CPU and CUDA execution providers

### Verify GPU is accessible from Python

```bash
python -c "
import torch
print(f'PyTorch {torch.__version__}')
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    print(f'CUDA version: {torch.version.cuda}')
"
```

You should see your GPU name. If `CUDA available: False`, check that `nvidia-smi` works in WSL2 and that you installed the `cu124` PyTorch.

```bash
# Verify ONNX Runtime GPU provider
python -c "
import onnxruntime as ort
providers = ort.get_available_providers()
print(f'ONNX Runtime {ort.__version__}')
print(f'Available providers: {providers}')
assert 'CUDAExecutionProvider' in providers, 'GPU provider missing!'
print('GPU provider available')
"
```

---

## Step 6: Kubernetes — k3d on WSL2

k3d runs k3s inside Docker containers. Since Docker Desktop with WSL2 backend is already set up, this works seamlessly.

```bash
# Install k3d
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash

# Install kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
rm kubectl

# Install Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Create the Sentinel cluster
k3d cluster create sentinel \
  --servers 1 \
  --agents 2 \
  --port "8080:80@loadbalancer" \
  --port "4317:4317@loadbalancer" \
  --k3s-arg "--disable=traefik@server:0" \
  --registry-create sentinel-registry:0.0.0.0:5111

# Verify
kubectl cluster-info
kubectl get nodes             # should show 3 nodes, all Ready
helm version

# Create namespaces
kubectl create namespace sentinel-app
kubectl create namespace sentinel-pipeline
kubectl create namespace sentinel-data
kubectl create namespace sentinel-monitoring
```

### GPU access in K8s pods (optional, for TensorRT path)

If you later want to run the classifier on GPU inside K8s:

```bash
# Deploy NVIDIA device plugin to k3s
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.0/deployments/static/nvidia-device-plugin.yml

# Verify GPU is visible to K8s
kubectl get nodes -o json | jq '.items[].status.capacity["nvidia.com/gpu"]'
# Should show "1" (or your GPU count)
```

This is optional for Phase 1 (ONNX INT8 on CPU is fast enough). It matters if you pursue the TensorRT optimization path later.

---

## Step 7: Terraform

```bash
# Install Terraform
wget -O - https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install -y terraform

terraform --version   # >= 1.9

# Initialize project Terraform
cd ~/projects/sentinel
mkdir -p terraform/environments/local
cat > terraform/environments/local/main.tf << 'EOF'
terraform {
  required_version = ">= 1.9"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.16"
    }
  }
}

provider "kubernetes" {
  config_path = "~/.kube/config"
}

provider "helm" {
  kubernetes {
    config_path = "~/.kube/config"
  }
}
EOF

cd terraform/environments/local && terraform init
```

---

## Step 8: Helm Repos

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

helm repo list   # should show all 8
```

---

## Step 9: Java + DB CLIs

```bash
# Java 17 (for Kafka and Spark)
sudo apt install -y openjdk-17-jdk-headless
echo 'export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64' >> ~/.bashrc
source ~/.bashrc
java -version

# PostgreSQL client
sudo apt install -y postgresql-client

# MongoDB shell
wget -qO - https://www.mongodb.org/static/pgp/server-8.0.asc | sudo gpg --dearmor -o /usr/share/keyrings/mongodb-server-8.0.gpg
echo "deb [signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list
sudo apt update && sudo apt install -y mongosh

# Redis CLI
sudo apt install -y redis-tools

# Verify
psql --version
mongosh --version
redis-cli --version
```

---

## Step 10: ONNX Pipeline Verification

This tests the full export → quantize → benchmark pipeline AND verifies GPU inference works.

```bash
cd ~/projects/sentinel
mkdir -p ml/optimization

cat > ml/optimization/verify_pipeline.py << 'PYEOF'
"""
Verify ONNX pipeline on dummy model.
Tests both CPU and GPU inference paths.
"""
import os
import time
import numpy as np
from transformers import AutoTokenizer
from optimum.onnxruntime import ORTModelForSequenceClassification
from onnxruntime.quantization import quantize_dynamic, QuantType
import onnxruntime as ort

MODEL_NAME = "distilbert-base-uncased-finetuned-sst-2-english"
OUT_DIR = "/tmp/sentinel_verify"

print("=" * 60)
print("SENTINEL — ONNX Pipeline Verification")
print("=" * 60)

# Step 1: Export
print("\n1. Exporting to ONNX...")
model = ORTModelForSequenceClassification.from_pretrained(MODEL_NAME, export=True)
model.save_pretrained(f"{OUT_DIR}/onnx")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
tokenizer.save_pretrained(f"{OUT_DIR}/onnx")

# Step 2: Quantize
print("2. Applying INT8 dynamic quantization...")
quantize_dynamic(
    model_input=f"{OUT_DIR}/onnx/model.onnx",
    model_output=f"{OUT_DIR}/quantized/model.onnx",
    weight_type=QuantType.QInt8,
)

onnx_size = os.path.getsize(f"{OUT_DIR}/onnx/model.onnx") / 1e6
quant_size = os.path.getsize(f"{OUT_DIR}/quantized/model.onnx") / 1e6
print(f"   ONNX size:      {onnx_size:.1f} MB")
print(f"   Quantized size: {quant_size:.1f} MB")
print(f"   Reduction:      {(1 - quant_size/onnx_size)*100:.0f}%")

# Step 3: Benchmark
text = "This is a test sentence for benchmarking inference latency."
inputs = tokenizer(text, return_tensors="np", padding="max_length",
                   max_length=128, truncation=True)
ort_inputs = {k: v.astype(np.int64) for k, v in inputs.items()
              if k in ["input_ids", "attention_mask"]}

def make_session(model_path, provider):
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 1
    return ort.InferenceSession(model_path, sess_options=so, providers=[provider])

def benchmark(session, n=100):
    for _ in range(10):  # warmup
        session.run(None, ort_inputs)
    lats = []
    for _ in range(n):
        s = time.perf_counter()
        session.run(None, ort_inputs)
        lats.append((time.perf_counter() - s) * 1000)
    return np.percentile(lats, 50), np.percentile(lats, 95)

print("\n3. Benchmarking...")
results = []

# CPU — ONNX
sess = make_session(f"{OUT_DIR}/onnx/model.onnx", "CPUExecutionProvider")
p50, p95 = benchmark(sess)
results.append(("ONNX (CPU)", onnx_size, p50, p95))
print(f"   ONNX (CPU):      p50={p50:.1f}ms  p95={p95:.1f}ms")

# CPU — Quantized
sess = make_session(f"{OUT_DIR}/quantized/model.onnx", "CPUExecutionProvider")
p50, p95 = benchmark(sess)
results.append(("ONNX+INT8 (CPU)", quant_size, p50, p95))
print(f"   ONNX+INT8 (CPU): p50={p50:.1f}ms  p95={p95:.1f}ms")

# GPU — ONNX (if available)
available = ort.get_available_providers()
if "CUDAExecutionProvider" in available:
    sess = make_session(f"{OUT_DIR}/onnx/model.onnx", "CUDAExecutionProvider")
    p50, p95 = benchmark(sess)
    results.append(("ONNX (GPU)", onnx_size, p50, p95))
    print(f"   ONNX (GPU):      p50={p50:.1f}ms  p95={p95:.1f}ms")
else:
    print("   GPU: CUDAExecutionProvider not available, skipping")

# Step 4: Accuracy check
print("\n4. Verifying outputs match...")
sess_orig = make_session(f"{OUT_DIR}/onnx/model.onnx", "CPUExecutionProvider")
sess_q = make_session(f"{OUT_DIR}/quantized/model.onnx", "CPUExecutionProvider")
out_orig = sess_orig.run(None, ort_inputs)[0]
out_q = sess_q.run(None, ort_inputs)[0]
max_diff = np.max(np.abs(out_orig - out_q))
same_pred = np.argmax(out_orig) == np.argmax(out_q)
print(f"   Max logit difference: {max_diff:.6f}")
print(f"   Same prediction: {same_pred}")

# Summary table
print("\n" + "=" * 60)
print(f"{'Variant':<20} {'Size MB':>8} {'p50 ms':>8} {'p95 ms':>8}")
print("-" * 60)
for name, size, p50, p95 in results:
    print(f"{name:<20} {size:>7.1f} {p50:>7.1f} {p95:>7.1f}")
print("=" * 60)

print("\n✓ Pipeline verified. Ready to apply to your RoBERTa model.")
PYEOF

python ml/optimization/verify_pipeline.py
```

---

## Step 11: Project Scaffold + Verification Script

```bash
cd ~/projects/sentinel

# Create directory structure
mkdir -p \
  docs \
  terraform/{modules/{kubernetes,kafka,databases,storage,monitoring,airflow,spark,ml-serving},environments/{local,aws,gcp}} \
  services/{classifier,stream-processor,data-simulator} \
  ml/{optimization,training,drift} \
  airflow/dags \
  k8s/{base,overlays/{local,cloud}} \
  monitoring/{prometheus,grafana/dashboards,otel} \
  db/{postgres/migrations,mongo/init-scripts} \
  tests/{unit,integration,load} \
  scripts

# .gitignore
cat > .gitignore << 'EOF'
__pycache__/
*.pyc
.env
*.egg-info/
dist/
build/
.pytest_cache/
.terraform/
*.tfstate
*.tfstate.backup
.terraform.lock.hcl
*.onnx
models/
data/
*.parquet
*.csv
.DS_Store
EOF

# Full verification script
cat > scripts/verify-setup.sh << 'BASH'
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
BASH

chmod +x scripts/verify-setup.sh
```

### Run it

```bash
bash scripts/verify-setup.sh
```

---

## Step 12: VS Code Integration (optional but recommended)

On the **Windows side**, install VS Code and the Remote-WSL extension:

1. Install VS Code from https://code.visualstudio.com/
2. Install extension: **Remote - WSL** (by Microsoft)
3. Open Ubuntu terminal, navigate to your project, and run:

```bash
cd ~/projects/sentinel
code .
```

This opens VS Code on Windows connected to your WSL2 filesystem. Your terminal in VS Code runs inside WSL2. All file operations happen on the Linux filesystem (fast). Do NOT put your project on the Windows filesystem (`/mnt/c/...`) — WSL2 cross-filesystem I/O is 5-10x slower.

---

## Step 13: Git Init + First Commit

```bash
cd ~/projects/sentinel
git init
git add -A
git commit -m "phase 0: project scaffold and environment setup (windows + nvidia gpu)"
```

---

## What the NVIDIA GPU Changes for Sentinel

Your GPU opens a fifth optimization step that wasn't practical before:

| Step | Runtime | Latency | Available to you |
|------|---------|---------|:---:|
| ONNX + INT8 (CPU) | CPU | ~35ms | ✓ |
| ONNX FP32 (GPU) | CUDA | ~10-15ms | ✓ now |
| ONNX + TensorRT FP16 (GPU) | TensorRT | ~5-8ms | ✓ now |
| ONNX + TensorRT INT8 (GPU) | TensorRT | ~3-5ms | ✓ now |

For the classifier service, the recommended path is still ONNX + INT8 on CPU (simplest, no GPU dependency in K8s, fast enough at ~35ms). But you can now benchmark the GPU path and include it in your comparison table — that table becomes more impressive with 4-5 rows instead of 3.

The GPU also speeds up model retraining during the Airflow retrain_dag (fine-tuning DistilBERT/RoBERTa on GPU is minutes instead of an hour on CPU).

---

## Time Estimate

| Step | Time |
|------|------|
| Windows features + reboot | 10 min |
| NVIDIA driver (if not already installed) | 10 min |
| WSL2 + Ubuntu install + reboot | 15 min |
| WSL2 memory config | 5 min |
| Docker Desktop install + config + reboot | 15 min |
| NVIDIA Container Toolkit | 10 min |
| System tools (apt install) | 5 min |
| Python (pyenv + packages) | 20 min |
| k3d + kubectl + Helm + cluster creation | 15 min |
| Terraform | 5 min |
| Helm repos | 5 min |
| Java + DB CLIs | 10 min |
| ONNX verification script | 5 min |
| Project scaffold + verify script | 5 min |
| **Total** | **~2.5 hours** |

Add 30-60 min if you hit GPU driver issues (the most common failure point). The verification script at the end tells you exactly what's broken.
