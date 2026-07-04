# Label UI — Explanation

The human-in-the-loop step between the stream processor flagging content
(`flagged_content` in MongoDB) and `pipelines/retraining` consuming it. An
operator reviews flagged spans, assigns a manual safe/harm label, decides
whether each one should feed the next fine-tuning run, and can kick off
that run directly from the same page.

It's the smallest service in the repo on purpose — a FastAPI backend plus
one static HTML file with inline JS, no build step, no frontend framework.

---

## Directory structure

```
services/label-ui/
  main.py           — routes: queue, label, stats, trigger-retrain, health
  config.py         — pydantic-settings: mongo_uri, airflow_base_url, airflow creds
  static/index.html — single-page table UI, inline JS, no build step
  Dockerfile        — 2-stage uv build, mirrors services/classifier/'s shape
  pyproject.toml
```

---

## Why plain `def` routes, not `async def`

```python
@app.get("/api/queue")
def get_queue(limit: int = 50, skip: int = 0) -> list[dict]:
    ...
```

FastAPI dispatches sync (`def`) route handlers to its own threadpool
automatically — this is the standard idiom for wrapping blocking I/O
(`pymongo`'s sync client) in FastAPI without manually calling
`run_in_executor`. The classifier's rule ("never block the event loop with
a heavy call inside `async def`") doesn't transfer here as a literal
constraint to route around: that rule exists because ONNX inference under
*concurrent, batched* load would starve the loop. This service is a
single-operator internal tool doing occasional, fast Mongo queries — there's
no batching or throughput concern to design around, so the simplest correct
thing (plain `def`, let FastAPI's threadpool handle it) is also the right
thing, not a shortcut.

---

## Routes

### `GET /api/queue` — the labelling backlog

```python
cursor = (
    _db.flagged_content.find({"training_decision": {"$in": [None, "pending"]}})
    .sort("ts", pymongo.ASCENDING)
    .skip(skip)
    .limit(limit)
)
```

**`$in: [None, "pending"]`, not just `{"training_decision": "pending"}`** —
documents written before this feature existed (by
`services/stream-processor/writer.py`, before `manual_label`/
`training_decision` were added to its schema) have no `training_decision`
field at all. MongoDB's query semantics already treat "field missing" and
"field is null" as matching a `null`/`None` query value, so this one filter
covers both old and new documents without a migration or backfill script.

**Sorted oldest-first** (`pymongo.ASCENDING` on `ts`) — matches the
`training_decision: 1, ts: 1` compound index added to `flagged_content`
(`infra/terraform/local/main.tf`), so this exact query pattern is indexed
rather than doing a collection scan.

### `POST /api/label/{doc_id}` — record a decision

```python
class LabelRequest(BaseModel):
    manual_label: Literal["safe", "harm"]
    training_decision: Literal["accepted", "rejected"]
```

`Literal["safe", "harm"]` and `Literal["accepted", "rejected"]` reject any
other value at the FastAPI/Pydantic validation layer (422) before the
handler body even runs — the same "let the type system enforce the
invariant" instinct as the `model_registry.status` CHECK constraint in
Postgres, just at the API boundary instead of the DB layer, since MongoDB
has no schema enforcement of its own to lean on here.

A plain `update_one` with `$set` — this route is the *only* place these two
fields ever change after a document is first written, so there's no
redelivery/idempotency concern to design around here the way there is in
`writer.py`'s upsert (see that file's explanation.md for why `writer.py`
specifically needs `$setOnInsert` instead).

### `GET /api/stats` — a cheap progress readout

```python
pipeline = [{"$group": {"_id": {"$ifNull": ["$training_decision", "pending"]}, "count": {"$sum": 1}}}]
```

`$ifNull` folds the same "missing field counts as pending" logic from the
queue query into the aggregation, so old and new documents get counted
consistently here too.

### `POST /api/trigger-retrain` — the button that starts everything downstream

```python
resp = httpx.post(
    f"{settings.airflow_base_url}/api/v1/dags/retrain_dag/dagRuns",
    json={},
    auth=(settings.airflow_admin_user, settings.airflow_admin_password),
)
```

Calls Airflow's stable REST API directly with HTTP Basic auth — the same
admin credentials the Airflow UI login uses. This only works because
`infra/terraform/local/airflow.tf` explicitly sets
`config.api.auth_backends = "airflow.api.auth.backend.basic_auth"`; without
it, the REST API's default auth backend rejects Basic auth even when the
webserver's own login page accepts the same credentials fine — the UI
session login and the stable API are two separate auth surfaces. See
[`../../infra/terraform/local/explanation.md`](../../infra/terraform/local/explanation.md)'s
Airflow gotcha #10 for the fix.

`json={}` — both `dag_run_id` and `logical_date` are optional in the
trigger-DAG-run API and auto-generate when omitted (`manual__<timestamp>`).
No need to construct either client-side.

A `502`, not a `500`, on failure — this route is a proxy to a downstream
service (Airflow), so a failure here is "the upstream didn't respond
correctly," which is what 502 Bad Gateway means. Distinguishing this from
Sentinel's own bugs (which would be a 500) matters for whoever's debugging
a failed trigger: check Airflow first, not this service's own logic.

---

## `static/index.html` — no build step, on purpose

One file: inline `<style>`, inline `<script>`, plain `fetch()` calls against
the routes above. No React/Vue, no bundler, no `npm install`. This matches
the project's "don't add infrastructure beyond what this phase needs"
principle — a single-operator review table doesn't need a frontend
framework's component model, state management, or build pipeline. If this
UI ever needs more than a table + a few buttons (multi-user auth, richer
filtering, real-time updates), that's the signal to reconsider, not before.

The table renders `input_text`, `text_type`, the **model's own** label/score
(for context — what did the classifier already think?), and `session_id`,
plus a dropdown for the human's `manual_label` and Accept/Reject buttons for
`training_decision`. Saving is per-row (`fetch` on click), not a bulk-submit
form — simpler to reason about, and a failed save on one row doesn't lose
work on the others.

---

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `MONGO_URI` | `mongodb://sentinel:sentinel@localhost:27017/sentinel` | MongoDB connection URI — `get_default_database()` uses the DB name embedded in the URI's path |
| `AIRFLOW_BASE_URL` | `http://localhost:8090` | Airflow webserver base URL |
| `AIRFLOW_ADMIN_USER` | `admin` | Basic auth username for the REST API trigger |
| `AIRFLOW_ADMIN_PASSWORD` | `sentinel` | Basic auth password |

---

## Tips and tricks

**Label a batch of docs from a script** (useful for seeding test data before
a retraining run, rather than clicking through the UI one row at a time):
```python
import json, urllib.request

docs = json.load(urllib.request.urlopen("http://localhost:8001/api/queue?limit=40"))
for d in docs:
    body = json.dumps({"manual_label": d["label"], "training_decision": "accepted"}).encode()
    req = urllib.request.Request(
        f"http://localhost:8001/api/label/{d['id']}", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)
```

**Verify the redelivery-safety fix directly** (confirms `writer.py`'s
`$setOnInsert` actually protects a labelled doc, without waiting for real
Kafka traffic to redeliver):
```bash
# Label a doc via the UI or the script above, note its span_id/text_type, then:
mongosh "$MONGO_URI" --eval "
  db.flagged_content.updateOne(
    {span_id: '<span_id>', text_type: '<text_type>'},
    {\$set: {input_text: 'redelivered', score: 0.01},
     \$setOnInsert: {manual_label: null, training_decision: 'pending'}},
    {upsert: true}
  )"
# manual_label/training_decision should be unchanged; input_text/score should update.
```

**Trigger a retrain from the CLI instead of the UI** (useful for scripting,
or confirming the API auth is actually working after an Airflow config
change):
```bash
curl -X POST http://localhost:8001/api/trigger-retrain
```

**Check current queue depth without opening the browser:**
```bash
curl -s http://localhost:8001/api/stats | python3 -m json.tool
```
