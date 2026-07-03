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
  ├─ POST /classify/batch (persist=False)
  │    Classifier returns labels + scores
  │    No classifier-side PG write (stream processor owns PG writes)
  │
  ├─ writer.write_classifications()   → PostgreSQL (all results)
  │    ON CONFLICT DO NOTHING on (span_id, text_type)
  │
  ├─ writer.write_flagged_content()   → MongoDB (harm + 10% safe)
  │
  └─ consumer.commit()                ← ONLY if all writes succeed
```

---

## main.py

### At-least-once delivery guarantee

This is the core design principle. Kafka offset commits are manual and happen
only after both database writes succeed:

```python
try:
    write_classifications(...)
    write_flagged_content(...)
except Exception:
    logger.exception("DB write failed — not committing, Kafka will redeliver")
    continue   # skip consumer.commit()

consumer.commit()  # only reached if no exception was raised
```

If the process crashes, the PostgreSQL connection drops, MongoDB times out, or
the classifier returns a 5xx — the offset is not committed. On restart (or after
the session timeout expires), Kafka redelivers the same messages from the last
committed offset.

**"At-least-once" means the same message can be processed more than once.** The
`ON CONFLICT DO NOTHING` on the partial unique index `(span_id, text_type) WHERE
span_id IS NOT NULL` in the `classifications` table prevents duplicate PostgreSQL
rows on redelivery. MongoDB writes are not deduplicated — a replayed batch may
produce duplicate `flagged_content` documents for the same span. This is
acceptable because the retraining pipeline deduplicates by `span_id` before
training.

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
per HTTP request, it collects all spans from a full poll cycle and sends them in
a single `/classify/batch` call. This is significantly more efficient:
- One HTTP round-trip per poll cycle instead of one per message
- One `executemany` INSERT instead of one per row
- One MongoDB `insert_many` instead of one per document

**`timeout_ms=1000`** — if no messages are available, `poll()` blocks for up to
1 second then returns an empty dict. The `while _running` loop immediately calls
`poll()` again. This keeps the consumer alive during quiet periods and gives the
heartbeat thread time to fire (keeping the consumer group session alive).

**`max_records=50`** — caps the batch size from Kafka per poll call. Without
this, a backlogged topic could deliver hundreds of messages in one poll, making
the subsequent `/classify/batch` call too large (exceeds `MAX_BATCH_SIZE=64`).
At 50 records, and assuming each OTLP message contains one LLM span with both
prompt and response, the worst case is 100 texts per classify call — the stream
processor uses the full `MAX_BATCH_SIZE=64` on the first call and leftovers on a
second if needed. In practice, the `MAX_POLL_RECORDS` env var lets you tune this.

### KafkaConsumer configuration

```python
consumer = KafkaConsumer(
    TOPIC,
    bootstrap_servers=KAFKA_BOOTSTRAP,
    group_id=GROUP_ID,
    enable_auto_commit=False,
    auto_offset_reset="earliest",
    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    request_timeout_ms=30_000,
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

**`request_timeout_ms=30_000`** — the maximum time to wait for a response to any
Kafka API request (fetch, produce, metadata). Keep this larger than
`session_timeout_ms`.

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

### `persist=False` on the classify call

```python
resp = http.post("/classify/batch", json={"texts": texts, "persist": False})
```

Without this flag, the classifier would also write to PostgreSQL asynchronously
(`asyncio.create_task`). The stream processor would still write its own rows,
producing duplicates. More critically, the classifier's async write is
fire-and-forget — it completes after the HTTP response, meaning the stream
processor cannot know if it succeeded before committing the Kafka offset.

`persist=False` gives the stream processor full control over when PG writes
happen relative to the offset commit. The classifier becomes a pure inference
service for this call path.

### `per_span_latency_ms`

```python
per_span_latency_ms = body["latency_ms"] / max(len(texts), 1)
```

The batch endpoint returns a single `latency_ms` for the entire batch. There is
no per-span latency. Dividing by batch size is an approximation: it assumes the
batch processed all texts in parallel, which is true for the ORT matrix multiply
but not for tokenization or Python overhead. It gives a reasonable per-span
estimate to store in the `classifications` table's `latency_ms` column, which
is used for latency trend analysis in the drift detection phase.

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

if docs:
    db.flagged_content.insert_many(docs)
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

**`insert_many` is not idempotent** — MongoDB's `insert_many` does not have a
built-in equivalent of `ON CONFLICT DO NOTHING`. On Kafka redelivery, the same
span may produce duplicate documents in `flagged_content`. This is acceptable:
- Duplicates are a small fraction of total documents (only during replay).
- The retrain pipeline deduplicates by `span_id` when building the training set.
- Adding a unique index on `(span_id, text_type)` in MongoDB would make
  `insert_many` fail on conflict unless `ordered=False` is set. A future hardening
  step could add this.

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
