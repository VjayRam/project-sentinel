# Stream Processor — Explanation

The stream processor is the bridge between the observability pipeline and the
classification system. It consumes OTLP trace messages from Kafka, extracts the
LLM span attributes (prompt text, response text), classifies them via the
classifier HTTP API, and persists results to PostgreSQL and MongoDB.

It is a long-running Python process, not a web server — no HTTP endpoints, no
frameworks. Just a Kafka poll loop.

---

## Directory structure

```
services/stream-processor/
  main.py       — Kafka consumer loop, signal handling, orchestration
  processor.py  — OTLP JSON parser, span extraction
  writer.py     — PostgreSQL and MongoDB persistence
  pyproject.toml
```

---

## How to run

`dev-start.sh` starts the stream processor automatically after the classifier.
To run it manually (with port-forwards already open):

```bash
cd services/stream-processor
KAFKA_BOOTSTRAP_SERVERS=localhost:9094 \
DATABASE_URL=postgresql://sentinel:sentinel@localhost:5432/sentinel \
MONGO_URI=mongodb://sentinel:sentinel@localhost:27017/sentinel \
CLASSIFIER_URL=http://localhost:8000 \
uv run python main.py
```

Watch its output:
```bash
tail -f /tmp/sentinel-pf/stream-processor.log
```

Send test data:
```bash
python scripts/simulate-traces.py --count 20 --harm-pct 0.3
```

---

## Data flow

```
Kafka topic: traces.raw (3 partitions, otlp_json encoding)
  │
  ▼ consumer.poll()
main.py — poll loop
  │
  ├─ processor.extract_spans()
  │    Parses OTLP JSON → list of span dicts
  │    One span → up to two entries (prompt + response)
  │
  ├─ chunk texts to CLASSIFY_CHUNK_SIZE (default 64)
  │
  ├─ POST /v1/moderations  (X-Sentinel-Skip-Persist: true), one call per chunk
  │    Classifier returns OpenAI-shaped moderation results
  │    _moderation_results_to_label_score() maps them to {label, score}
  │    Header (not a body field) suppresses the classifier's own PG write —
  │    stream processor owns PG writes for this traffic
  │
  ├─ writer.write_classifications()   → PostgreSQL (all results)
  │    ON CONFLICT DO NOTHING on (span_id, text_type)
  │
  ├─ writer.write_flagged_content()   → MongoDB (harm + 10% safe)
  │    bulk_write: UpdateOne upsert keyed on (span_id, text_type) when
  │    span_id is present, InsertOne otherwise — idempotent on redelivery
  │
  └─ consumer.commit()                ← ONLY if all writes succeed
```

---

## main.py

### At-least-once delivery guarantee

This is the core design principle. Kafka offset commits are manual and happen
only after `_pg_write()` (which internally writes both PostgreSQL and
MongoDB) succeeds:

```python
try:
    pg_conn = _pg_write(pg_conn, spans, all_results, model_version, per_span_latency_ms, mongo_db)
except Exception:
    logger.exception("DB write failed — not committing, Kafka will redeliver")
    continue   # skip consumer.commit()

consumer.commit()  # only reached if no exception was raised
```

If the process crashes, the PostgreSQL connection drops, MongoDB times out, or
the classifier returns a 5xx — the offset is not committed. On restart (or after
the session timeout expires), Kafka redelivers the same messages from the last
committed offset.

**"At-least-once" means the same message can be processed more than once.**
Both persistence layers are now idempotent on redelivery: the `ON CONFLICT
DO NOTHING` on the partial unique index `(span_id, text_type) WHERE span_id
IS NOT NULL` in the `classifications` table prevents duplicate PostgreSQL
rows, and `writer.write_flagged_content()`'s `bulk_write` with `UpdateOne`
upserts (keyed on the same `(span_id, text_type)` pair) makes MongoDB
redelivery a no-op instead of a duplicate insert too — see `writer.py`'s
section below for why this changed from a plain `insert_many`.

### `_pg_write()` — reconnect on drop, rollback on poisoned transaction

```python
def _pg_write(pg_conn, spans, results, model_version, latency_ms, mongo_db) -> psycopg.Connection:
    try:
        write_classifications(pg_conn, spans, results, model_version, latency_ms)
        write_flagged_content(mongo_db, spans, results, model_version, SAFE_SAMPLE_RATE)
        return pg_conn
    except psycopg.OperationalError:
        # connection actually lost — close, reconnect, retry once
        ...
    except psycopg.Error:
        # connection alive but transaction poisoned — roll back so the NEXT
        # call can use it, then re-raise so THIS batch isn't committed
        pg_conn.rollback()
        raise
```

Two distinct PostgreSQL failure modes need different recovery, and
conflating them was a real bug found by live-testing, not a hypothetical:

1. **`psycopg.OperationalError`** — the connection itself is gone (network
   drop, PG pod restart). Recovery: close the dead connection, open a new
   one, retry the write once on the fresh connection. If the retry also
   fails, close that connection too before raising — otherwise a
   double-failure would leak the second connection silently.
2. **Any other `psycopg.Error`** (e.g. a constraint violation) — the TCP
   connection is still perfectly alive, but the **current transaction is
   aborted**. Every subsequent command on that same connection then fails
   with `psycopg.errors.InFailedSqlTransaction` until something calls
   `.rollback()` — psycopg3 doesn't do this automatically. This was
   reproduced live: a stale `model_version` (the classifier had
   self-registered a model version the DB didn't actually have a matching
   row for — see `services/classifier/explanation.md`'s model-registration
   bug) triggered a `ForeignKeyViolation` on `classifications`. Before this
   fix, that left the long-lived `pg_conn` permanently poisoned — **every
   subsequent poll cycle's write failed the same way forever**, not just
   the one batch that hit the actual FK violation, because nothing ever
   rolled back the aborted transaction state. The fix calls
   `pg_conn.rollback()` in this branch before re-raising: the current
   batch still correctly fails (so its Kafka offset isn't committed and it
   gets redelivered), but the connection is usable again for the *next*
   poll cycle instead of being permanently wedged.

`ON CONFLICT DO NOTHING` on `(span_id, text_type)` makes the retry-after-
reconnect path idempotent — if the first attempt's `write_classifications`
partially succeeded before the connection dropped, replaying it on the new
connection just no-ops on the rows that already landed.

### `consumer.poll()` vs iterating the consumer

kafka-python supports two consumption styles:

```python
# Iterator style — one message at a time
for msg in consumer:
    process(msg)
    consumer.commit()

# Poll style — one batch per call
records = consumer.poll(timeout_ms=1000, max_records=50)
for tp, messages in records.items():
    for msg in messages:
        process(msg)
consumer.commit()  # commits all fetched offsets at once
```

The stream processor uses `poll()` for batching. Instead of classifying one span
per HTTP request, it collects all spans from a full poll cycle and sends them
in one or more `/v1/moderations` calls (chunked to `CLASSIFY_CHUNK_SIZE`, see
above). This is significantly more efficient:
- A small, bounded number of HTTP round-trips per poll cycle instead of one per message
- One `executemany` INSERT instead of one per row
- One MongoDB `bulk_write` instead of one operation per document

**`timeout_ms=1000`** — if no messages are available, `poll()` blocks for up to
1 second then returns an empty dict. The `while _running` loop immediately calls
`poll()` again. This keeps the consumer alive during quiet periods and gives the
heartbeat thread time to fire (keeping the consumer group session alive).

**`max_records=50`** — caps the batch size from Kafka per poll call. Without
this, a backlogged topic could deliver hundreds of messages in one poll,
producing an unbounded number of texts to classify at once. At 50 records,
and assuming each OTLP message contains one LLM span with both prompt and
response, the worst case is 100 texts per poll cycle — split into two
`CLASSIFY_CHUNK_SIZE=64`-sized chunks (64 + 36) rather than one oversized
call that would 422. In practice, the `MAX_POLL_RECORDS` env var lets you
tune this.

### KafkaConsumer configuration

```python
consumer = KafkaConsumer(
    TOPIC,
    bootstrap_servers=KAFKA_BOOTSTRAP,
    group_id=GROUP_ID,
    enable_auto_commit=False,
    auto_offset_reset="earliest",
    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    request_timeout_ms=40_000,
    session_timeout_ms=30_000,
    heartbeat_interval_ms=10_000,
)
```

**`enable_auto_commit=False`** — disables Kafka's built-in periodic offset
commits. Auto-commit happens on a timer regardless of whether processing succeeded,
which would silently lose messages on failure. Manual commit via `consumer.commit()`
after confirmed writes is the correct at-least-once pattern.

**`auto_offset_reset="earliest"`** — if the consumer group has no committed offset
(first run, or after the group was deleted), start from the beginning of the topic.
`"latest"` would skip all historical messages and only process new ones. For a
content safety system, re-processing historical data is preferable to missing
any content.

**`group_id=GROUP_ID`** (fixed string: `"sentinel-stream-processor"`) — all
instances of the stream processor join the same consumer group. Kafka distributes
the 3 partitions of `traces.raw` across group members — with 1 instance it owns
all 3 partitions, with 3 instances each owns 1. Scaling beyond 3 replicas gives
no benefit because there are only 3 partitions.

**`session_timeout_ms=30_000`** — if the broker doesn't receive a heartbeat from
this consumer within 30 seconds, it considers the consumer dead and triggers a
partition rebalance (another consumer takes over). The `heartbeat_interval_ms=10_000`
sends a heartbeat every 10 seconds — well within the 30-second timeout. A slow
DB write (which blocks the poll loop) can cause missed heartbeats and unintentional
rebalances. If DB writes consistently take > 20 seconds, increase `session_timeout_ms`.

**`request_timeout_ms=40_000`** — the maximum time to wait for a response to any
Kafka API request (fetch, produce, metadata). Kept larger than
`session_timeout_ms` (30s) — a request timeout shorter than or equal to the
session timeout risks the client giving up on a slow-but-alive broker
response right around the same time a rebalance would otherwise trigger,
compounding the two failure modes instead of keeping them independent.

### Signal handling

```python
_running = True

def _handle_signal(sig, frame):
    global _running
    _running = False

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)
```

When `dev-start.sh` receives Ctrl-C, it sends SIGTERM to the stream processor
process. The handler sets `_running = False`. The poll loop exits cleanly at
the next iteration, calls `consumer.close()` (commits any pending offsets and
sends a LeaveGroup request to the broker so the partition rebalance happens
immediately rather than waiting for the session timeout), and closes the
PostgreSQL connection.

Without signal handling, Ctrl-C raises a `KeyboardInterrupt` that would skip
the `consumer.close()` call. Kafka would wait 30 seconds (session timeout)
before reassigning the partitions to another consumer — making restarts slow.

### `/v1/moderations`, not `/classify/batch` — dogfooding the public endpoint

```python
resp = http.post(
    "/v1/moderations",
    json={"input": chunk},
    headers={"X-Sentinel-Skip-Persist": "true"},
)
```

The stream processor calls the classifier's **OpenAI-compatible**
`/v1/moderations` endpoint — the same one any external integration would
use — rather than a Sentinel-internal `/classify/batch` shape. This is a
deliberate architectural decision (see `CLAUDE.md`'s OTel GenAI semantic
conventions section): the project's own highest-volume internal traffic
exercises exactly the code path external callers get, instead of treating
`/classify/batch` as the "real" endpoint and `/v1/moderations` as a thin
facade nobody but external callers actually hits. If `/v1/moderations` ever
broke, the stream processor's own traffic would surface it immediately in
local dev, rather than only being caught when an external caller notices.

**This decision was accidentally reverted once, mid-session, and caught by
the user asking "isnt the /classify/batch endpoint modified to be
/v1/moderations?"** — a fix for an unrelated issue moved this call back to
`/classify/batch`, undoing a deliberate choice from an earlier commit
without checking `git log`/`git blame` first. The lesson generalizes: before
"fixing" something that touches an existing, working call path, check
whether its current shape was a deliberate decision (commit message, code
comment, or explanation.md note) rather than an oversight — matching an
older pattern isn't automatically correct if the code moved past it on
purpose.

**`_moderation_results_to_label_score()`** translates `/v1/moderations`'
OpenAI-shaped `{flagged, categories, category_scores}` results into this
service's internal `{label, score}` shape that `writer.py`'s functions
expect — named and factored out explicitly (not an inline dict comprehension
at the call site) so the translation reads as a deliberate boundary: calling
an OpenAI-compatible endpoint from internal code makes this remapping
inherent, not incidental, and worth a name.

**Skip-persist via `X-Sentinel-Skip-Persist` header, not a `persist` body
field.** Without suppressing it, the classifier would also write to
PostgreSQL asynchronously from inside `/v1/moderations` — the stream
processor would still write its own rows too, producing duplicates. Earlier
this was a `persist: bool` field on the request body (mirroring
`/classify/batch`'s still-present `persist` field). It moved to a header
specifically so `ModerationRequest` — the schema an external
`openai.moderations.create()`-style caller sends — stays a clean,
zero-Sentinel-internals OpenAI-compatible shape; see
`services/classifier/explanation.md`'s `/v1/moderations` section for the
full reasoning from the classifier side.

### Chunking to the classifier's batch limit

```python
chunks = [texts[i : i + CLASSIFY_CHUNK_SIZE] for i in range(0, len(texts), CLASSIFY_CHUNK_SIZE)]
```

A single Kafka poll (`max_records=50`) can extract more spans than the
classifier accepts in one request — up to 100 texts if every message has
both a prompt and a response. `CLASSIFY_CHUNK_SIZE` (default `64`, env var)
splits the poll's texts into `/v1/moderations`-sized chunks, one HTTP call
per chunk, results concatenated back into `all_results`.

**Not read from the same env var name as the classifier's own limit** —
`CLASSIFY_CHUNK_SIZE` here vs `MAX_BATCH_SIZE` in
`services/classifier/config.py` — because these are two separately deployed
services with independent configuration surfaces; sharing an env var name
across service boundaries would be an implicit, easy-to-break coupling. Both
default to `64` today, but if either is tuned away from that default, the
other has to be set explicitly too — nothing enforces they stay in sync
automatically. Exceeding the classifier's real limit gets a `422` from
Pydantic's `max_length` validation on `ModerationRequest.input`, which
`resp.raise_for_status()` below turns into a Kafka-redelivery retry rather
than a silent data loss.

### `per_span_latency_ms`

```python
per_span_latency_ms = classify_ms_total / max(len(texts), 1)
```

`classify_ms_total` accumulates wall-clock time across every chunk's HTTP
call in this poll cycle (there can be more than one now, per the chunking
above) — not a single batch's `latency_ms` field. Dividing by the total
span count across all chunks gives a reasonable per-span estimate to store
in the `classifications` table's `latency_ms` column, which is used for
latency trend analysis in the drift detection phase. Same caveat as before:
this assumes even latency distribution across spans within and across
chunks, which is true for the ORT matrix multiply's parallelism but not
exactly for tokenization or per-request Python/HTTP overhead — a reasonable
approximation, not an exact per-span measurement.

### httpx `Client` (not `AsyncClient`)

The stream processor is entirely synchronous — no `asyncio`, no `async def`.
`httpx.Client` (the synchronous variant) blocks the thread for the duration of
each HTTP request. This is correct here: the consumer poll loop runs on a single
thread and can afford to block during classification because there are no other
coroutines competing for the event loop.

`timeout=60.0` — allows up to 60 seconds for a classify call. At `MAX_BATCH_SIZE=64`
texts with the INT8 model, inference takes well under 1 second. The 60-second
budget covers extreme cases (model cold-start, temporary CPU saturation).

---

## processor.py

Responsible for one thing: parsing an OTLP JSON message into a flat list of
span dicts that the rest of the pipeline can use without knowing anything about
the OTLP wire format.

### OTLP JSON message structure

The OTel Collector publishes `ExportTraceServiceRequest` messages in JSON format.
The structure is nested:

```json
{
  "resourceSpans": [
    {
      "resource": { "attributes": [...] },
      "scopeSpans": [
        {
          "scope": { "name": "...", "version": "..." },
          "spans": [
            {
              "traceId": "hex-string",
              "spanId": "hex-string",
              "name": "llm.completion",
              "attributes": [
                {"key": "llm.request.prompt",   "value": {"stringValue": "..."}},
                {"key": "llm.response.content", "value": {"stringValue": "..."}},
                {"key": "llm.request.model",    "value": {"stringValue": "gpt-4o"}},
                {"key": "session.id",           "value": {"stringValue": "abc-123"}}
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

The three-level nesting (`resourceSpans → scopeSpans → spans`) is the OTLP
protocol's way of grouping: resource = the service that emitted the traces,
scope = the instrumentation library, span = one operation. A single message can
contain spans from multiple resources and scopes.

### `_attr(attributes, key)`

```python
def _attr(attributes: list[dict], key: str) -> str | None:
    for a in attributes:
        if a["key"] != key:
            continue
        v = a.get("value", {})
        if "stringValue" in v:  return v["stringValue"]
        if "intValue" in v:     return str(v["intValue"])
        if "doubleValue" in v:  return str(v["doubleValue"])
    return None
```

OTLP attribute values are tagged unions — `stringValue`, `intValue`,
`doubleValue`, `boolValue` etc. The function returns a string representation
regardless of the original type so downstream code never needs to handle multiple
types. `latency_ms` is a double in the OTel spec; returning `str(double)` lets
callers handle it uniformly.

Linear scan through `attributes` is fine — LLM spans have at most ~10 attributes.
A dict lookup would be faster but unnecessary at this scale.

### LLM span filtering

```python
prompt = _attr(attrs, "llm.request.prompt")
response = _attr(attrs, "llm.response.content")

if not prompt and not response:
    continue  # not an LLM span
```

The OTel Collector forwards ALL spans from the chat app, not just LLM spans —
HTTP request spans, database query spans, etc. The filter passes only spans that
have at least one of the two LLM attributes. Non-LLM spans are silently skipped.

### One span → two classification targets

```python
if prompt:
    spans.append({"text": prompt, "text_type": "prompt", **meta})
if response:
    spans.append({"text": response, "text_type": "response", **meta})
```

A single LLM span carries both the input and output. Classifying them separately
means:
- The user's prompt is evaluated independently of the model's response. A safe
  prompt + harmful response and a harmful prompt + safe response are both caught.
- The `text_type` label in PostgreSQL lets you analyze harm rates by input vs
  output separately — useful for understanding whether the model itself is
  generating harmful content or whether users are injecting it.
- The `(span_id, text_type)` unique constraint provides idempotency at the
  sub-span level — you can replay the same span without duplicating rows.

---

## writer.py

### `write_classifications` — PostgreSQL

```python
cur.executemany(
    """
    INSERT INTO classifications
        (input_text, label, score, model_version, latency_ms, inference_at, span_id, text_type)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (span_id, text_type) WHERE span_id IS NOT NULL DO NOTHING
    """,
    records,
)
conn.commit()
```

**`executemany` vs individual inserts** — psycopg3's `executemany` sends all
rows in a single round-trip using PostgreSQL's extended query protocol with
pipelining. For 50 rows, this is ~50× fewer network round-trips compared to
individual INSERT statements.

**`ON CONFLICT (span_id, text_type) WHERE span_id IS NOT NULL DO NOTHING`** —
this is the idempotency mechanism. It resolves against the partial unique index
defined in the PostgreSQL schema:

```sql
CREATE UNIQUE INDEX classifications_span_id_text_type_idx
    ON classifications (span_id, text_type)
    WHERE span_id IS NOT NULL;
```

The `WHERE span_id IS NOT NULL` makes it a *partial* unique index. Rows inserted
by the classifier's own async write path (which doesn't have a span_id) have
`span_id = NULL`. NULL values are excluded from unique constraints in PostgreSQL
— multiple rows with `span_id = NULL` can coexist. The stream processor's rows
have non-null `span_id` and are deduplicated by the index.

**`conn.commit()` at the end of every write** — psycopg3 uses transactions by
default. Without an explicit commit, the INSERT is rolled back when the connection
closes. The commit is placed after the `executemany` so both the INSERT and the
commit succeed or both fail — partial writes don't happen.

**`span["span_id"] or None`** — OTLP `spanId` is a hex string. If the chat app
emits a span without a `spanId` (non-compliant), the default `""` from
`processor.py` is converted to `None` so the unique index ignores it.

### `write_flagged_content` — MongoDB

```python
for span, result in zip(spans, results):
    label = result["label"]
    if label == "harm" or random.random() < safe_sample_rate:
        docs.append({...})

if not docs:
    return

operations = [
    pymongo.UpdateOne(
        {"span_id": doc["span_id"], "text_type": doc["text_type"]},
        {"$set": doc},
        upsert=True,
    )
    if doc["span_id"]
    else pymongo.InsertOne(doc)
    for doc in docs
]
result = db.flagged_content.bulk_write(operations, ordered=False)
```

**Why not write everything to MongoDB?** The `flagged_content` collection is the
training dataset for the retrain pipeline. If you store 100% of safe content,
the dataset becomes massively imbalanced (safe examples vastly outnumber harm
examples). A model trained on imbalanced data learns to predict "safe" for
everything because that minimizes loss on 90%+ of examples.

**`safe_sample_rate=0.1`** — store 10% of safe content, all harmful content.
This gives the retrain pipeline a roughly 10:1 safe-to-harm ratio (depending on
actual traffic distribution). The ratio is tunable via `SAFE_SAMPLE_RATE` env var.

For very low-traffic early deployments where harm examples are rare, consider
`SAFE_SAMPLE_RATE=0.5` (50%) until you accumulate enough harm examples to balance.

**`bulk_write` with per-document upsert, not `insert_many` — MongoDB writes
are now idempotent too.** This replaced a plain `insert_many(docs)` call.
The old version was explicitly documented as non-idempotent ("a replayed
batch may produce duplicate `flagged_content` documents") on the reasoning
that the retrain pipeline would dedupe by `span_id` later anyway — accepted
as a known gap rather than fixed. It was fixed: each document with a
non-null `span_id` becomes a `pymongo.UpdateOne` filtered on
`{span_id, text_type}` with `upsert=True` — the same natural key as
PostgreSQL's partial unique index — so redelivering the same span overwrites
the same document instead of inserting a second one. Documents with no
`span_id` (same gap as the PostgreSQL side: nothing to dedupe on) fall back
to a plain `pymongo.InsertOne`.

**`ordered=False` on `bulk_write`** — with the default `ordered=True`,
MongoDB stops processing the batch at the first failing operation, leaving
every operation after it un-run even if they'd have succeeded independently.
`ordered=False` lets every operation attempt independently, so one bad
document doesn't block the rest of an otherwise-healthy batch from
committing — a partial failure then only leaves the genuinely-failed
documents to be retried on redelivery, and every other document in the
batch is already a safe upsert that won't duplicate when that redelivery
happens.

**Document shape:**

```json
{
  "ts":           "ISODate — when the classification ran",
  "input_text":   "the text that was classified",
  "text_type":    "prompt or response",
  "label":        "harm or safe",
  "score":        0.97,
  "model_version": "sentinel-roberta-20260627T003749Z-int8",
  "session_id":   "conversation session from the chat app",
  "span_id":      "OTLP hex span ID",
  "trace_id":     "OTLP hex trace ID (links to Jaeger)",
  "llm_model":    "gpt-4o (which LLM the chat app called)"
}
```

The `trace_id` field is particularly useful: it links each classified span to
the full distributed trace in Jaeger, letting you see the entire conversation
context when investigating a flagged document.

---

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9094` | Kafka EXTERNAL listener address |
| `CLASSIFIER_URL` | `http://localhost:8000` | Classifier HTTP base URL |
| `DATABASE_URL` | `postgresql://sentinel:sentinel@localhost:5432/sentinel` | PostgreSQL DSN |
| `MONGO_URI` | `mongodb://sentinel:sentinel@localhost:27017/sentinel` | MongoDB connection URI |
| `SAFE_SAMPLE_RATE` | `0.1` | Fraction of safe spans stored in MongoDB |
| `MAX_POLL_RECORDS` | `50` | Max messages per Kafka poll call |
| `CLASSIFY_CHUNK_SIZE` | `64` | Max texts per `/v1/moderations` call — must not exceed the classifier's own `MAX_BATCH_SIZE` (separate env var, separate service; keep both in sync manually if either is tuned) |

---

## Tips and tricks

**Watch the offset lag** — how far behind the consumer is from the latest message:
```bash
kubectl exec -n sentinel-data statefulset/kafka -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group sentinel-stream-processor
```
`LAG` = 0 means the consumer is caught up. A growing LAG means the consumer is
slower than the producer — scale replicas or investigate slow DB writes.

**Manually reset offsets** — to reprocess all historical messages from the beginning:
```bash
# Stop the stream processor first (Ctrl-C dev-start.sh or kill the process)
kubectl exec -n sentinel-data statefulset/kafka -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --group sentinel-stream-processor --reset-offsets \
  --to-earliest --topic traces.raw --execute
# Restart dev-start.sh
```

**Inspect raw Kafka messages** — to see what the OTel Collector is actually writing:
```bash
kubectl exec -n sentinel-data statefulset/kafka -- \
  kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic traces.raw --max-messages 1 --from-beginning | python3 -m json.tool
```

**Tune throughput** — the main levers:
- `MAX_POLL_RECORDS`: larger batches → fewer HTTP calls → higher throughput,
  but larger memory spikes and longer time between offset commits.
- `CLASSIFY_CHUNK_SIZE`: raising this reduces the number of `/v1/moderations`
  calls per poll cycle, but must stay ≤ the classifier's `MAX_BATCH_SIZE` or
  every oversized chunk 422s and the whole poll cycle fails (Kafka redelivers
  forever until the mismatch is fixed).
- `SAFE_SAMPLE_RATE`: lower value → fewer MongoDB writes → higher throughput.
- `session_timeout_ms`: increase if slow DB writes cause rebalances.
- Kafka partitions: currently 3. To scale beyond 3 stream processor replicas,
  increase partitions (requires recreating the topic or using `kafka-topics.sh
  --alter --partitions N`).

**Verify end-to-end flow:**
```bash
# 1. Send traces
python scripts/simulate-traces.py --count 10 --harm-pct 0.5

# 2. Check PostgreSQL
psql postgresql://sentinel:sentinel@localhost:5432/sentinel \
  -c "SELECT label, count(*) FROM classifications GROUP BY label ORDER BY 1;"

# 3. Check MongoDB (via mongo-express at http://localhost:8081
#    or mongosh)
mongosh "mongodb://sentinel:sentinel@localhost:27017/sentinel" \
  --eval "db.flagged_content.find().sort({ts:-1}).limit(5).pretty()"

# 4. Check traces in Jaeger
open http://localhost:16686
# Search service: chat-app-simulator
```
