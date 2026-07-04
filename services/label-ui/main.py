"""Manual labelling UI for MongoDB flagged_content — the human-in-the-loop
step between the stream processor flagging content and the retraining
pipeline consuming it.

Routes are plain `def`, not `async def` — FastAPI dispatches sync routes to
its threadpool automatically, which is all this needs. Unlike the classifier
(see services/classifier/CLAUDE.md rules on session.run() blocking the event
loop under concurrent batched load), this is a single-operator internal tool
with occasional, fast Mongo queries — no throughput/batching concern that
would justify async drivers here.
"""

import logging
from datetime import datetime, timezone
from typing import Literal

import httpx
import pymongo
from bson import ObjectId
from bson.errors import InvalidId
from config import settings
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="sentinel-label-ui")

_mongo = pymongo.MongoClient(settings.mongo_uri)
_db = _mongo.get_default_database()


def _serialize(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


@app.get("/")
def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/queue")
def get_queue(limit: int = 50, skip: int = 0) -> list[dict]:
    """Docs still awaiting a manual labelling decision, oldest first.

    Missing training_decision (older docs, written before this field
    existed) counts as pending too — $in matches both null and the
    literal string, so nothing predating this feature is silently hidden.
    """
    cursor = (
        _db.flagged_content.find({"training_decision": {"$in": [None, "pending"]}})
        .sort("ts", pymongo.ASCENDING)
        .skip(skip)
        .limit(limit)
    )
    return [_serialize(d) for d in cursor]


@app.get("/api/stats")
def get_stats() -> dict:
    pipeline = [
        {
            "$group": {
                "_id": {"$ifNull": ["$training_decision", "pending"]},
                "count": {"$sum": 1},
            }
        }
    ]
    counts = {row["_id"]: row["count"] for row in _db.flagged_content.aggregate(pipeline)}
    return {
        "pending": counts.get("pending", 0),
        "accepted": counts.get("accepted", 0),
        "rejected": counts.get("rejected", 0),
    }


class LabelRequest(BaseModel):
    manual_label: Literal["safe", "harm"]
    training_decision: Literal["accepted", "rejected"]


@app.post("/api/label/{doc_id}")
def label_doc(doc_id: str, body: LabelRequest) -> dict:
    try:
        oid = ObjectId(doc_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="invalid doc id")

    result = _db.flagged_content.update_one(
        {"_id": oid},
        {
            "$set": {
                "manual_label": body.manual_label,
                "training_decision": body.training_decision,
                "labelled_at": datetime.now(timezone.utc),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="doc not found")
    return {"id": doc_id, **body.model_dump()}


@app.post("/api/trigger-retrain")
def trigger_retrain() -> JSONResponse:
    """Triggers orchestration/retrain_dag.py via Airflow's REST API.

    Basic auth against the same admin user the Airflow UI itself uses
    (AIRFLOW__API__AUTH_BACKENDS=basic_auth is set explicitly in
    infra/terraform/local/airflow.tf — not a chart default).
    """
    try:
        resp = httpx.post(
            f"{settings.airflow_base_url}/api/v1/dags/retrain_dag/dagRuns",
            json={},  # dag_run_id and logical_date both auto-generate
            auth=(settings.airflow_admin_user, settings.airflow_admin_password),
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("Failed to trigger retrain_dag")
        raise HTTPException(status_code=502, detail=f"Airflow trigger failed: {exc}")

    body = resp.json()
    logger.info("Triggered retrain_dag | dag_run_id=%s", body.get("dag_run_id"))
    return JSONResponse(
        {"dag_run_id": body.get("dag_run_id"), "state": body.get("state")}, status_code=201
    )
