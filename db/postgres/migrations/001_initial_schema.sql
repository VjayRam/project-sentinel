-- Sentinel — initial schema
-- Applied by a K8s Job after PostgreSQL is ready (see terraform/modules/databases/).

-- ── classifications ───────────────────────────────────────────────────────────
-- One row per inference. Written by the Spark streaming job.
CREATE TABLE IF NOT EXISTS classifications (
    id            BIGSERIAL PRIMARY KEY,
    trace_id      TEXT        NOT NULL,
    session_id    TEXT,
    ts            TIMESTAMPTZ NOT NULL,
    label         TEXT        NOT NULL,   -- 'harmful' | 'safe'
    confidence    FLOAT       NOT NULL,
    model_version TEXT        NOT NULL,
    prompt_len    INT,
    response_len  INT
);

CREATE INDEX IF NOT EXISTS idx_classifications_ts           ON classifications (ts);
CREATE INDEX IF NOT EXISTS idx_classifications_label_ts     ON classifications (label, ts);
CREATE INDEX IF NOT EXISTS idx_classifications_model_ts     ON classifications (model_version, ts);

-- ── drift_stats ───────────────────────────────────────────────────────────────
-- One row per drift metric evaluation window. Written by the Spark streaming job.
CREATE TABLE IF NOT EXISTS drift_stats (
    id         BIGSERIAL   PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL,
    metric     TEXT        NOT NULL,   -- 'psi' | 'jsd' | 'confidence_decay'
    value      FLOAT       NOT NULL,
    threshold  FLOAT       NOT NULL,
    breached   BOOLEAN     NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drift_stats_ts         ON drift_stats (ts);
CREATE INDEX IF NOT EXISTS idx_drift_stats_metric_ts  ON drift_stats (metric, ts);

-- ── model_registry ────────────────────────────────────────────────────────────
-- One row per model version. Written by the Airflow retrain_dag after evaluation.
CREATE TABLE IF NOT EXISTS model_registry (
    version           TEXT        PRIMARY KEY,
    onnx_path         TEXT        NOT NULL,

    -- evaluation metrics (from optimize.py benchmark against test_dataset.csv)
    accuracy          FLOAT,
    f1                FLOAT,
    auc_roc           FLOAT,

    -- resource profile (from optimize.py benchmark)
    size_mb           FLOAT,       -- model file size on disk
    p50_latency_ms    FLOAT,       -- median inference latency on benchmark dataset
    p95_latency_ms    FLOAT,       -- 95th percentile inference latency
    p99_latency_ms    FLOAT,       -- 99th percentile inference latency

    -- deployment lifecycle
    trained_at        TIMESTAMPTZ,
    deployed_at       TIMESTAMPTZ,
    retired_at        TIMESTAMPTZ,
    status            TEXT        NOT NULL  -- 'staging' | 'active' | 'retired'
        CHECK (status IN ('staging', 'active', 'retired'))
);
