#!/usr/bin/env bash
# dev-start.sh — Start the full Sentinel dev stack locally.
#
# What this does:
#   1. Ensures the k3d cluster is running (creates it if needed)
#   2. Builds classifier + stream-processor Docker images and imports into k3d
#   3. Runs terraform apply (all infra + app deployments, incl. Airflow)
#   4. Waits for data-layer pods (+ Airflow scheduler/webserver) to pass readiness probes
#   5. Verifies Kafka topic exists and Airflow's DAGs load with no import errors
#   6. Opens port-forwards for every service
#   7. Runs schema migration and model auto-bootstrap
#   8. Restarts app deployments so they pick up the bootstrapped model
#   9. Waits for classifier + stream-processor pods to become ready
#
# Ctrl-C stops everything cleanly.
#
# Usage:
#   ./scripts/dev-start.sh
#   MODEL_PATH=/path/to/int8 ./scripts/dev-start.sh   # explicit model override

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

wait_for_port() {
    local label=$1 port=$2 log_name=$3
    local i=0
    while ! timeout 1 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/$port" 2>/dev/null; do
        i=$((i + 1))
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

mkdir -p "$PF_DIR"

cleanup() {
    echo ""
    info "Shutting down..."
    for pid_file in "$PF_DIR"/*.pid; do
        [[ -f "$pid_file" ]] && kill "$(cat "$pid_file")" 2>/dev/null || true
    done
    rm -f "$PF_DIR"/*.pid
    info "Done."
}
trap cleanup EXIT INT TERM

# ── prerequisites ─────────────────────────────────────────────────────────────
info "Checking prerequisites..."
for cmd in k3d kubectl terraform docker uv; do
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

# ── build images and import into k3d ─────────────────────────────────────────
# Images are built locally and imported directly — no registry needed.
# imagePullPolicy=Never in the Deployment tells K8s to use only the local store.
info "Building classifier image..."
docker build -t sentinel-classifier:local "$REPO_ROOT/services/classifier/" --quiet
k3d image import sentinel-classifier:local -c "$CLUSTER" 2>/dev/null
info "Classifier image imported"

info "Building stream-processor image..."
docker build -t sentinel-stream-processor:local "$REPO_ROOT/services/stream-processor/" --quiet
k3d image import sentinel-stream-processor:local -c "$CLUSTER" 2>/dev/null
info "Stream-processor image imported"

info "Building drift image..."
docker build -t sentinel-drift:local "$REPO_ROOT/pipelines/drift/" --quiet
k3d image import sentinel-drift:local -c "$CLUSTER" 2>/dev/null
info "Drift image imported"

# ── terraform ─────────────────────────────────────────────────────────────────
info "Applying Terraform..."
cd "$TF_DIR"
terraform apply -auto-approve
cd "$REPO_ROOT"

# ── wait for data-layer pods ──────────────────────────────────────────────────
# App pods (classifier, stream-processor) are NOT waited on here — they need
# the model bootstrap to complete first (see below).
info "Waiting for data-layer pods to pass readiness probes..."
kubectl wait --for=condition=ready pod -l app=postgresql    -n sentinel-data       --timeout=120s
kubectl wait --for=condition=ready pod -l app=mongodb       -n sentinel-data       --timeout=120s
kubectl wait --for=condition=ready pod -l app=minio         -n sentinel-data       --timeout=120s
kubectl wait --for=condition=ready pod -l app=mongo-express -n sentinel-data       --timeout=120s
kubectl wait --for=condition=ready pod -l app=kafka         -n sentinel-data       --timeout=180s
kubectl wait --for=condition=ready pod -l app=prometheus    -n sentinel-monitoring --timeout=120s
kubectl wait --for=condition=ready pod -l app=grafana       -n sentinel-monitoring --timeout=120s
kubectl wait --for=condition=ready pod -l app=jaeger        -n sentinel-monitoring --timeout=60s
kubectl wait --for=condition=ready pod -l app=otel-collector -n sentinel-monitoring --timeout=60s
kubectl wait --for=condition=ready pod -l release=airflow,component=scheduler -n sentinel-pipeline --timeout=180s
kubectl wait --for=condition=ready pod -l release=airflow,component=webserver -n sentinel-pipeline --timeout=180s
info "Data-layer pods ready"

# ── ensure Kafka topic exists ─────────────────────────────────────────────────
# Kafka uses ephemeral storage in local dev — topics are lost on cluster restart.
# The Terraform job runs once and is not re-triggered on subsequent dev-start runs.
info "Ensuring Kafka topic 'traces.raw' exists..."
kubectl exec -n sentinel-data kafka-0 -- \
    /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 \
    --create --if-not-exists \
    --topic traces.raw \
    --partitions 3 \
    --replication-factor 1 >/dev/null 2>&1 && info "Kafka topic ready" \
    || warn "Could not ensure Kafka topic — check kafka-0 pod"

# ── verify Airflow can actually load and run a DAG ────────────────────────────
# healthcheck_dag.py (mounted from orchestration/ via ConfigMap) is a minimal
# smoke test: if it fails to parse or import here, retrain_dag.py (Phase 7.3)
# won't fare any better. Checks import errors rather than triggering a new
# run every dev-start.sh invocation, which would clutter run history for no
# benefit — the DAG's own logic is trivial enough that "it parses" is already
# a meaningful signal.
info "Verifying Airflow DAGs load correctly..."
_airflow_dag_ok=false
for _ in $(seq 1 20); do
    _import_errors=$(kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
        airflow dags list-import-errors 2>/dev/null || true)
    if echo "$_import_errors" | grep -q "No data found"; then
        _airflow_dag_ok=true
        break
    fi
    sleep 3
done
if [[ "$_airflow_dag_ok" == true ]]; then
    info "Airflow DAGs loaded with no import errors"
else
    warn "Airflow DAG import errors detected (or scheduler not ready in time):"
    warn "$_import_errors"
fi

# ── sync PostgreSQL password ───────────────────────────────────────────────────
info "Syncing PostgreSQL password from secret..."
PG_PASSWORD=$(kubectl get secret postgresql-credentials -n sentinel-data \
    -o jsonpath='{.data.password}' | base64 -d)
# -v/:'var' substitution only happens when psql reads SQL as a script
# (stdin/-f) — NOT via -c, which bypasses that parser and sends the string
# through unexpanded (verified live: -c left the literal text ":'pw'" in
# the query, which Postgres then rejected as a syntax error). Piped via
# stdin instead (kubectl exec -i to keep stdin open across the exec).
if echo "ALTER USER sentinel PASSWORD :'pw';" | kubectl exec -i -n sentinel-data postgresql-0 -- \
    psql -U sentinel -d sentinel -v pw="$PG_PASSWORD"; then
    info "PostgreSQL password synced"
else
    warn "ALTER USER failed — local socket auth may not be trusted in this setup."
    warn "To fix: delete the PVC so PostgreSQL re-initialises with the current secret:"
    warn "  kubectl delete statefulset postgresql -n sentinel-data"
    warn "  kubectl delete pvc data-postgresql-0 -n sentinel-data"
    warn "  cd infra/terraform/local && terraform apply -auto-approve"
    die "Cannot guarantee DB connectivity — fix the PostgreSQL password first."
fi

export DATABASE_URL="postgresql://sentinel:${PG_PASSWORD}@localhost:5432/sentinel"

# ── kill stale port-forwards from a previous run ──────────────────────────────
for pid_file in "$PF_DIR"/*.pid; do
    [[ -f "$pid_file" ]] && kill "$(cat "$pid_file")" 2>/dev/null || true
done
sleep 1

# ── open port-forwards ────────────────────────────────────────────────────────
# --address=0.0.0.0 binds to all interfaces so Windows browsers reach WSL2.
info "Opening port-forwards..."

kubectl port-forward --address=0.0.0.0 -n sentinel-data svc/postgresql 5432:5432 \
    &>"$PF_DIR/postgres.log" & echo $! >"$PF_DIR/postgres.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-data svc/mongodb 27017:27017 \
    &>"$PF_DIR/mongo.log" & echo $! >"$PF_DIR/mongo.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-data svc/minio 9000:9000 9001:9001 \
    &>"$PF_DIR/minio.log" & echo $! >"$PF_DIR/minio.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-data svc/mongo-express 8081:8081 \
    &>"$PF_DIR/mongo-express.log" & echo $! >"$PF_DIR/mongo-express.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-monitoring svc/prometheus 9090:9090 \
    &>"$PF_DIR/prometheus.log" & echo $! >"$PF_DIR/prometheus.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-monitoring svc/grafana 3000:3000 \
    &>"$PF_DIR/grafana.log" & echo $! >"$PF_DIR/grafana.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-monitoring svc/jaeger 16686:16686 \
    &>"$PF_DIR/jaeger.log" & echo $! >"$PF_DIR/jaeger.pid"

kubectl port-forward --address=0.0.0.0 -n sentinel-monitoring svc/otel-collector 4317:4317 4318:4318 \
    &>"$PF_DIR/otel-collector.log" & echo $! >"$PF_DIR/otel-collector.pid"

# Local port 8090, not 8080 — k3d's own serverlb container publishes host
# port 8080 -> its internal ingress (0.0.0.0:8080->80/tcp) by default,
# unrelated to anything in this repo. Binding 8080 here silently loses the
# race against it (or fails outright), so the webserver's remote port
# (8080, inside the pod/Service) stays the same; only the local side moves.
kubectl port-forward --address=0.0.0.0 -n sentinel-pipeline svc/airflow-webserver 8090:8080 \
    &>"$PF_DIR/airflow.log" & echo $! >"$PF_DIR/airflow.pid"

# Classifier port-forward — allows local curl/tests against the in-cluster pod.
kubectl port-forward --address=0.0.0.0 -n sentinel-app svc/classifier 8000:8000 \
    &>"$PF_DIR/classifier.log" & echo $! >"$PF_DIR/classifier.pid"

wait_for_port "PostgreSQL"     5432  postgres || true
wait_for_port "MongoDB"        27017 mongo || true
wait_for_port "MinIO"          9000  minio || true
wait_for_port "mongo-express"  8081  mongo-express || true
wait_for_port "Prometheus"     9090  prometheus || true
wait_for_port "Grafana"        3000  grafana || true
wait_for_port "Jaeger"         16686 jaeger || true
wait_for_port "OTel Collector" 4317  otel-collector || true
wait_for_port "Airflow"        8090  airflow || true

# ── schema ─────────────────────────────────────────────────────────────────────
# Schema (model_registry, classifications, drift_stats, and all indexes) is
# fully owned by Terraform's postgres_init ConfigMap (01_schema.sql) — it runs
# automatically on the postgres pod's first startup. No separate migration step
# needed here; keeping schema definitions in one place avoids them drifting
# apart (see: the drift_stats table used to only exist via this script).
info "Schema managed by Terraform — nothing to migrate"

# ── model auto-bootstrap ──────────────────────────────────────────────────────
# The classifier pod needs a model in MinIO to start. Run the optimizer locally
# if no valid model exists, then the pod picks it up after the rollout restart.
export MINIO_ENDPOINT="http://localhost:9000"
export MINIO_ACCESS_KEY="sentinel"
export MINIO_SECRET_KEY="sentinel-minio"

if [[ -z "${MODEL_PATH:-}" ]] && command -v psql >/dev/null 2>&1; then
    _registry_path=$(psql "$DATABASE_URL" -t -A -c "
        SELECT model_path FROM model_registry
        WHERE status IN ('active','staging')
        ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                 COALESCE(promoted_at, created_at) DESC
        LIMIT 1" 2>/dev/null || true)

    _needs_optimizer=false

    if [[ -z "$_registry_path" ]]; then
        info "model_registry is empty — no model available."
        _needs_optimizer=true
    elif [[ "$_registry_path" == /* ]]; then
        # Active entry is a local host path — pods can't use it.
        # Check if a MinIO-path entry already exists for the same run.
        _minio_path=$(psql "$DATABASE_URL" -t -A -c "
            SELECT model_path FROM model_registry
            WHERE model_path LIKE 'models/%'
            ORDER BY created_at DESC LIMIT 1" 2>/dev/null || true)

        if [[ -n "$_minio_path" ]]; then
            info "Active registry entry is a host path — switching to MinIO path: $_minio_path"
            # -v/:'var' substitution only happens when psql reads SQL as a
            # script (stdin/-f) — NOT via -c, which bypasses that parser and
            # sends the string through unexpanded (verified live: -c left
            # the literal text ":'path'" in the query, which Postgres then
            # rejected as a syntax error). Piped via stdin instead.
            echo "
                UPDATE model_registry SET status = 'retired'
                WHERE model_path NOT LIKE 'models/%' AND status IN ('active','staging');
                UPDATE model_registry SET status = 'active', promoted_at = NOW()
                WHERE model_path = :'path';" \
                | psql "$DATABASE_URL" -v path="$_minio_path" >/dev/null 2>&1 || true
        elif ! find "$_registry_path" -maxdepth 1 -name "*.onnx" 2>/dev/null | grep -q .; then
            warn "Registry has a stale local path (artifacts deleted): $_registry_path"
            psql "$DATABASE_URL" -c \
                "UPDATE model_registry SET status = 'retired'
                 WHERE model_path NOT LIKE 'models/%'
                   AND status IN ('active','staging');" \
                >/dev/null 2>&1 || true
            _needs_optimizer=true
        else
            warn "Active model is a local host path — pods cannot access it."
            warn "Run the optimizer to upload the model to MinIO: uv run --package sentinel-optimizer python -m pipelines.optimizer ..."
            _needs_optimizer=true
        fi
    fi

    if [[ "$_needs_optimizer" == true ]]; then
        if find "$REPO_ROOT/logs/optimizer" -maxdepth 2 -name "report.json" \
           2>/dev/null | grep -q .; then
            info "Found completed optimizer run in logs/optimizer/ — skipping bootstrap."
            _needs_optimizer=false
        fi
    fi

    if [[ "$_needs_optimizer" == true ]]; then
        info "Running optimizer pipeline to bootstrap the first model..."
        info "(downloads ~500 MB on first run — this takes a few minutes)"
        cd "$REPO_ROOT"
        uv run --package sentinel-optimizer python -m pipelines.optimizer \
            --model-id VijayRam1812/content-classifier-roberta \
            --output-dir artifacts \
            --log-dir logs \
        || die "Optimizer failed — fix the error above, then re-run dev-start.sh."
        info "Optimizer complete — model registered and uploaded to MinIO."
    fi
fi

# ── restart app deployments ───────────────────────────────────────────────────
# Model is now in MinIO. Restart the pods so they download it on startup.
# Also ensures the latest image build is picked up on subsequent runs.
info "Restarting app deployments..."
kubectl rollout restart deployment/classifier     -n sentinel-app
kubectl rollout restart deployment/stream-processor -n sentinel-app

info "Waiting for classifier to become ready..."
kubectl rollout status deployment/classifier -n sentinel-app --timeout=120s

info "Waiting for stream-processor to become ready..."
kubectl rollout status deployment/stream-processor -n sentinel-app --timeout=120s

# Classifier port-forward may have connected before the pod was ready.
# Re-establish it now that the pod is confirmed healthy.
kill "$(cat "$PF_DIR/classifier.pid")" 2>/dev/null || true
kubectl port-forward --address=0.0.0.0 -n sentinel-app svc/classifier 8000:8000 \
    &>"$PF_DIR/classifier.log" & echo $! >"$PF_DIR/classifier.pid"
wait_for_port "Classifier" 8000 classifier || true

echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  Sentinel dev stack is running  —  Ctrl-C to stop everything         ${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Classifier API   →  http://localhost:8000         (K8s pod)"
echo "  Classifier docs  →  http://localhost:8000/docs"
echo "  Prometheus scrape →  http://localhost:8000/metrics"
echo ""
echo "  Jaeger UI        →  http://localhost:16686"
echo "  Grafana          →  http://localhost:3000  (admin / admin)"
echo "  Prometheus       →  http://localhost:9090"
echo "  MinIO console    →  http://localhost:9001  (sentinel / sentinel-minio)"
echo "  MinIO S3 API     →  http://localhost:9000"
echo "  mongo-express    →  http://localhost:8081"
echo ""
echo "  OTel Collector   →  grpc://localhost:4317  http://localhost:4318"
echo ""
echo "  Airflow UI       →  http://localhost:8090  (admin / sentinel)"
echo ""
echo "  PostgreSQL       →  localhost:5432  (sentinel / sentinel)"
echo "  MongoDB          →  localhost:27017 (sentinel / sentinel)"
echo ""
echo "  Simulate traces: python scripts/simulate-traces.py"
echo "  Stream logs:     kubectl logs -f -n sentinel-app deploy/stream-processor"
echo "  Classifier logs: kubectl logs -f -n sentinel-app deploy/classifier"
echo "  Rebuild images:  re-run ./scripts/dev-start.sh"
echo ""
echo "  See docs/local-dev.md for curl examples, schemas, and full reference."
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Keep the script alive until Ctrl-C — cleanup trap handles teardown.
while true; do sleep 60; done
