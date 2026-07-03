"""Integration tests for db.py — require a live PostgreSQL connection.

Skipped automatically when DATABASE_URL is not set.
Run with:
    DATABASE_URL=postgresql://sentinel:...@localhost:5432/sentinel \
        pipelines/drift/.venv/bin/pytest pipelines/drift/tests/test_db.py -v
"""

import os
from datetime import datetime, timezone

import pytest

DB_URL = os.environ.get("DATABASE_URL")
skip_no_db = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")


@skip_no_db
def test_get_active_model_version_returns_string():
    from db import get_active_model_version

    version = get_active_model_version(DB_URL)
    assert isinstance(version, str), f"Expected str, got {type(version)}"
    assert len(version) > 0, "model_version should not be empty"


@skip_no_db
def test_read_reference_scores_returns_floats():
    from db import get_active_model_version, read_reference_scores

    version = get_active_model_version(DB_URL)
    scores = read_reference_scores(DB_URL, version, size=50)
    assert isinstance(scores, list)
    assert len(scores) > 0, "Expected at least one reference row"
    assert all(0.0 <= s <= 1.0 for s in scores), "Scores must be in [0, 1]"


@skip_no_db
def test_read_current_scores_returns_floats_and_window():
    from db import get_active_model_version, read_current_scores

    version = get_active_model_version(DB_URL)
    scores, window_start, window_end = read_current_scores(DB_URL, version, hours=24 * 365)
    assert isinstance(scores, list)
    assert len(scores) > 0, "Expected rows within the last year"
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert window_start <= window_end


@skip_no_db
def test_write_drift_stats_inserts_row():
    import psycopg
    from db import get_active_model_version, write_drift_stats

    version = get_active_model_version(DB_URL)
    now = datetime.now(timezone.utc)

    write_drift_stats(
        DB_URL,
        model_version=version,
        window_start=now,
        window_end=now,
        n_samples=42,
        psi=0.9999,
        jsd=0.1234,
        drift_flagged=True,
    )

    with psycopg.connect(DB_URL) as conn:
        row = conn.execute(
            """
            SELECT model_version, n_samples, psi, jsd, drift_flagged
            FROM drift_stats
            WHERE psi = 0.9999 AND n_samples = 42
            ORDER BY computed_at DESC
            LIMIT 1
            """,
        ).fetchone()

    assert row is not None, "write_drift_stats did not insert a row"
    assert row[0] == version
    assert row[1] == 42
    assert abs(row[2] - 0.9999) < 1e-6
    assert row[4] is True

    # Clean up the sentinel row.
    with psycopg.connect(DB_URL) as conn:
        conn.execute("DELETE FROM drift_stats WHERE psi = 0.9999 AND n_samples = 42")
