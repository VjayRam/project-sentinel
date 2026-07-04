# MLflow image — Explanation

This directory holds exactly one file: a `Dockerfile`. There's no application
code here — MLflow's tracking server is a pip-installable CLI (`mlflow
server ...`), not something this repo builds. The Dockerfile exists to
extend the official image with the two extra clients it needs to talk to
this project's backing stores.

---

## Why a custom image at all

```dockerfile
FROM ghcr.io/mlflow/mlflow:v3.13.0
RUN pip install --no-cache-dir psycopg2-binary boto3
```

The official `ghcr.io/mlflow/mlflow` image only supports SQLite/MySQL
backend stores and local-disk artifact storage out of the box — it doesn't
bundle a PostgreSQL driver or an S3 client. This project's MLflow deployment
(`infra/terraform/local/mlflow.tf`) needs both:

- `--backend-store-uri postgresql://...` (experiments/runs/params/metrics)
  → needs `psycopg2-binary`
- `--default-artifact-root s3://mlflow/` (against MinIO) → needs `boto3`

Without them, `mlflow server` fails immediately at startup trying to
construct the SQLAlchemy engine or the S3 client. This is a
well-documented, common gap — most MLflow-on-Postgres-and-S3 deployment
guides extend the base image the same way.

**Version pinned to an exact tag** (`v3.13.0`, not `latest`), matching this
repo's general "never `:latest`" rule. Confirmed to exist in the `ghcr.io/
mlflow/mlflow` registry before pinning to it, rather than guessed.

---

## Built and imported like every other local image

```bash
docker build -t sentinel-mlflow:local infra/mlflow/
k3d image import sentinel-mlflow:local -c sentinel
```

Same pattern as classifier/stream-processor/drift/label-ui/retraining —
`dev-start.sh` does both steps automatically. `imagePullPolicy: Never` in
`mlflow.tf`'s Deployment spec tells Kubernetes to use only the local image
store, never attempt a registry pull.

---

## Tips and tricks

**Checking what's actually installed in the image:**
```bash
docker run --rm sentinel-mlflow:local pip list | grep -iE "mlflow|psycopg2|boto3"
```

**`mlflow server --help` is the fastest way to check a flag's exact
semantics** rather than guessing from memory — this is how the
`--allowed-hosts` and `--workers` behavior (see
[`../terraform/local/explanation.md`](../terraform/local/explanation.md)'s
MLflow section for what those two flags fixed) was actually confirmed,
not assumed:
```bash
docker run --rm sentinel-mlflow:local mlflow server --help
```

**Bumping the MLflow version later**: change the `FROM` tag, confirm the
new tag exists in the registry first (`ghcr.io/mlflow/mlflow`'s package
page, or just let `docker build` fail fast if it doesn't), rebuild, and
re-run `dev-start.sh` — no other file needs to change unless the new
version's CLI flags differ (check `--help` again).
