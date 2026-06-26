#!/usr/bin/env bash
# dev-start.sh — Start the full Sentinel dev stack locally.
#
# What this does:
#   1. Ensures the k3d cluster is running (creates it if needed)
#   2. Runs terraform apply (namespaces + PostgreSQL + MongoDB + MinIO)
#   3. Waits for all pods to pass readiness probes
#   4. Opens port-forwards for every data-layer service
#   5. Starts the classifier with uvicorn on localhost:8000
#
# Ctrl-C stops everything cleanly (port-forwards + uvicorn).
#
# Usage:
#   ./scripts/dev-start.sh
#   MODEL_PATH=/path/to/int8 ./scripts/dev-start.sh   # explicit model override

set -euo pipefail

# ── formatting ────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# Wait until a TCP port accepts connections, up to ~15 seconds.
# Prints a clear success or failure line — errors go to the log file named
# after the service so you know exactly where to look.
wait_for_port() {
    local label=$1 port=$2 log_name=$3
    local i=0
    while ! timeout 1 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/$port" 2>/dev/null; do
        i=$((i + 1))   # arithmetic assignment never exits under set -e (unlike ((i++)) which returns 0 when i=0)
        if [[ $i -ge 15 ]]; then
            warn "${label} did not open on port ${port} — check $PF_DIR/${log_name}.log"
            return 1
        fi
        sleep 1
    done
    info "${label} ready on localhost:${port}"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLUSTER="sentinel"
TF_DIR="$REPO_ROOT/infra/terraform/local"
PF_DIR="/tmp/sentinel-pf"
UVICORN_PID=""

mkdir -p "$PF_DIR"

# ── cleanup on exit / Ctrl-C ──────────────────────────────────────────────────
cleanup() {
    echo ""
    info "Shutting down..."
    [[ -n "$UVICORN_PID" ]] && kill "$UVICORN_PID" 2>/dev/null || true
    for pid_file in "$PF_DIR"/*.pid; do
        [[ -f "$pid_file" ]] && kill "$(cat "$pid_file")" 2>/dev/null || true
    done
    rm -f "$PF_DIR"/*.pid
    info "Done."
}
trap cleanup EXIT INT TERM

# ── prerequisites ─────────────────────────────────────────────────────────────
info "Checking prerequisites..."
for cmd in k3d kubectl terraform uv; do
    command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' not found — install it first"
done

# ── k3d cluster ───────────────────────────────────────────────────────────────
if k3d cluster list 2>/dev/null | awk 'NR>1{print $1}' | grep -q "^${CLUSTER}$"; then
    SERVERS=$(k3d cluster list | awk -v c="$CLUSTER" '$1==c{print $2}')
    RUNNING=$(echo "$SERVERS" | cut -d/ -f1)
    TOTAL=$(echo "$SERVERS" | cut -d/ -f2)
    if [[ "$RUNNING" -lt "$TOTAL" ]]; then
        info "Starting k3d cluster '$CLUSTER'..."
        k3d cluster start "$CLUSTER"
    else
        info "k3d cluster '$CLUSTER' is already running"
    fi
else
    info "Creating k3d cluster '$CLUSTER'..."
    k3d cluster create "$CLUSTER"
fi

kubectl config use-context "k3d-$CLUSTER" >/dev/null

# ── terraform ─────────────────────────────────────────────────────────────────
info "Applying Terraform (PostgreSQL + MongoDB + MinIO)..."
cd "$TF_DIR"
terraform apply -auto-approve
cd "$REPO_ROOT"

# ── wait for pods ─────────────────────────────────────────────────────────────
info "Waiting for data-layer pods to pass readiness probes..."
kubectl wait --for=condition=ready pod -l app=postgresql -n sentinel-data       --timeout=120s
kubectl wait --for=condition=ready pod -l app=mongodb    -n sentinel-data       --timeout=120s
kubectl wait --for=condition=ready pod -l app=minio      -n sentinel-data       --timeout=120s
kubectl wait --for=condition=ready pod -l app=prometheus -n sentinel-monitoring --timeout=120s
kubectl wait --for=condition=ready pod -l app=grafana    -n sentinel-monitoring --timeout=120s
info "All pods ready"

# ── sync PostgreSQL password ───────────────────────────────────────────────────
# PostgreSQL initialises its password once when the data directory is first
# created. If the PVC survived a previous Terraform apply that used a different
# password, the stored password won't match the current secret. We fix this by
# reading the authoritative value from the k8s secret and applying it via
# local Unix socket auth (pg_hba.conf trusts local socket connections, so no
# password is required to run this ALTER USER command).
info "Syncing PostgreSQL password from secret..."
PG_PASSWORD=$(kubectl get secret postgresql-credentials -n sentinel-data \
    -o jsonpath='{.data.password}' | base64 -d)
if kubectl exec -n sentinel-data postgresql-0 -- \
    psql -U sentinel -d sentinel -c "ALTER USER sentinel PASSWORD '${PG_PASSWORD}';"; then
    info "PostgreSQL password synced"
else
    warn "ALTER USER failed — local socket auth may not be trusted in this setup."
    warn "To fix: delete the PVC so PostgreSQL re-initialises with the current secret:"
    warn "  kubectl delete statefulset postgresql -n sentinel-data"
    warn "  kubectl delete pvc data-postgresql-0 -n sentinel-data"
    warn "  cd infra/terraform/local && terraform apply -auto-approve"
    die "Cannot guarantee DB connectivity — fix the PostgreSQL password first."
fi

# ── kill stale port-forwards from a previous run ──────────────────────────────
for pid_file in "$PF_DIR"/*.pid; do
    [[ -f "$pid_file" ]] && kill "$(cat "$pid_file")" 2>/dev/null || true
done
sleep 1

# ── open port-forwards ────────────────────────────────────────────────────────
# --address=0.0.0.0 binds the tunnel to all interfaces, not just 127.0.0.1.
# This is required on WSL2 so that the Windows browser can reach the services
# via localhost — without it, the relay from Windows to WSL2 doesn't connect.
info "Opening port-forwards..."

kubectl port-forward --address=0.0.0.0 -n sentinel-data svc/postgresql 5432:5432 \
    &>"$PF_DIR/postgres.log" & echo $! >"$PF_DIR/postgres.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-data svc/mongodb 27017:27017 \
    &>"$PF_DIR/mongo.log" & echo $! >"$PF_DIR/mongo.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-data svc/minio 9000:9000 9001:9001 \
    &>"$PF_DIR/minio.log" & echo $! >"$PF_DIR/minio.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-monitoring svc/prometheus 9090:9090 \
    &>"$PF_DIR/prometheus.log" & echo $! >"$PF_DIR/prometheus.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-monitoring svc/grafana 3000:3000 \
    &>"$PF_DIR/grafana.log" & echo $! >"$PF_DIR/grafana.pid"

# Verify each tunnel actually opened — prints a clear success or failure line.
# Errors are in $PF_DIR/*.log so the user knows exactly where to look.
wait_for_port "PostgreSQL"  5432  postgres
wait_for_port "MongoDB"     27017 mongo
wait_for_port "MinIO"       9000  minio
wait_for_port "Prometheus"  9090  prometheus
wait_for_port "Grafana"     3000  grafana

# ── classifier ─────────────────────────────────────────────────────────────────

export DATABASE_URL="postgresql://sentinel:${PG_PASSWORD}@localhost:5432/sentinel"
export MINIO_ENDPOINT="http://localhost:9000"
export MINIO_ACCESS_KEY="sentinel"
export MINIO_SECRET_KEY="sentinel-minio"

# Model loading priority (automatic — no action required in most cases):
#   1. model_registry table (active or most recent staging) → download from MinIO
#   2. MODEL_PATH env var → load that directory directly
#   3. logs/optimizer/*/report.json auto-discovery → use local int8 path
# Run the optimizer first if the registry is empty and MODEL_PATH is not set.
if [[ -z "${MODEL_PATH:-}" ]]; then
    info "MODEL_PATH not set — classifier will load from model_registry (MinIO) or logs/optimizer/"
fi

info "Starting classifier at http://localhost:8000 ..."
cd "$REPO_ROOT/services/classifier"
uv run uvicorn main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level info &
UVICORN_PID=$!

sleep 2   # wait for uvicorn to bind before printing the summary

echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  Sentinel dev stack is running  —  Ctrl-C to stop everything         ${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Classifier API   →  http://localhost:8000"
echo "  Classifier docs  →  http://localhost:8000/docs"
echo "  Prometheus scrape →  http://localhost:8000/metrics"
echo ""
echo "  Grafana          →  http://localhost:3000  (admin / admin)"
echo "  Prometheus       →  http://localhost:9090"
echo "  MinIO console    →  http://localhost:9001  (sentinel / sentinel-minio)"
echo "  MinIO S3 API     →  http://localhost:9000"
echo ""
echo "  PostgreSQL       →  localhost:5432  (sentinel / sentinel)"
echo "  MongoDB          →  localhost:27017 (sentinel / sentinel)"
echo ""
echo "  See docs/local-dev.md for curl examples, schemas, and full reference."
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

wait "$UVICORN_PID"
