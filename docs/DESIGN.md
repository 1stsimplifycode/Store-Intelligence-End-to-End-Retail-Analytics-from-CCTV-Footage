# DESIGN.md — Store Intelligence System

## Architecture Overview

The system is a four-stage pipeline: **Detection → Events → API → Dashboard**.

```
CCTV clips
    │
    ▼
pipeline/detect.py
  YOLOv8n (person detection)
  BYTETracker (multi-object tracking)
  Staff classifier (HSV colour histogram)
  Zone classifier (polygon lookup)
  EventEmitter → events/*.jsonl
    │
    ▼ POST /events/ingest (batch ≤ 500)
    │
app/ (FastAPI + aiosqlite)
  ingestion.py  — dedup by event_id, partial success
  metrics.py    — real-time query, no caching
  funnel.py     — session-deduped conversion funnel
  heatmap.py    — normalised zone visit scores
  anomalies.py  — queue spike / conversion drop / dead zone
  health.py     — feed staleness per store
    │
    ▼
dashboard/live.py (rich terminal, polls every 5s)
```

The API uses **SQLite via aiosqlite**. The schema was designed to accept both the actual
`sample_events.jsonl` format observed in the dataset (which uses `store_code`, `id_token`,
`event_timestamp`) and the PS-spec format (which uses `store_id`, `visitor_id`, `timestamp`).
The `RawEvent` Pydantic model normalises these on ingest.

All metric queries run directly against SQLite without a caching layer. For the 40-store
production scale discussed in Section 8, this would need to move to PostgreSQL + Redis,
but for the challenge's dataset size SQLite is sufficient and eliminates operational complexity.

---

## AI-Assisted Decisions

### 1. Re-ID approach: colour histogram vs OSNet

I asked Claude to compare OSNet/torchreid against a colour histogram approach for Re-ID:

> *"Which approach gives better Re-ID in a 20-minute retail CCTV clip — an OSNet appearance
> embedding model or a per-channel colour histogram? Consider: accuracy, container size,
> CPU-only constraint, and the fact that staff wear a distinct uniform colour."*

Claude's suggestion was OSNet for higher accuracy. I **overrode** this: OSNet requires a
~3GB model download and a GPU or a very slow CPU inference path. For a containerised
solution that must run on a developer laptop via `docker compose up`, adding a 3GB layer
is a non-starter. Colour histogram Re-ID is "good enough" for a 20-minute clip window —
customers rarely change their outfit, and the cosine similarity threshold of 0.82 catches
obvious re-entries without false-merging two different people.

### 2. Event schema: two-format strategy

The sample events JSONL in the dataset uses completely different field names from the PS
schema (e.g. `store_code` vs `store_id`, `event_timestamp` vs `event_time`, `id_token`
vs `visitor_id`). I asked Claude whether to normalise on ingest or store both.

Claude suggested storing raw and normalised in separate tables. I chose to **normalise
on ingest** in a single table, using a Pydantic validator that maps both field-name
conventions to a common column set. This reduces schema complexity: every query uses the
same column names regardless of which format was ingested.

### 3. Staff detection: HSV torso histogram vs VLM

I considered using a VLM (Claude Vision) to classify whether a detected person is staff
based on a cropped torso patch. Claude Vision suggested: "extract a 64×64 patch from the
torso region, encode as base64, and prompt 'Is this person wearing a retail staff uniform?'"

I tested this on three frames. It worked but added ~1.2 seconds per detection (API latency).
At 15fps with FRAME_SKIP=3, that's 5 detections/second per camera — VLM classification
would be a 6× bottleneck. I **overrode** to use HSV histogram matching against a
configurable staff uniform colour. The `--staff-colour` flag lets the operator tune this
per store without retraining.

---

## Production Readiness Notes

- **Idempotency**: `event_id` is the dedup key. `INSERT OR IGNORE` makes every ingest call safe to retry.
- **Graceful degradation**: Database unavailability returns HTTP 503 with a structured JSON body, never a raw traceback.
- **Structured logging**: Every request emits a JSON log line with `trace_id`, `store_id`, `endpoint`, `latency_ms`.
- **Health endpoint**: Reports `STALE_FEED` if no events ingested from a store in 10 minutes. This is the first thing an on-call engineer checks.

---

## What I Would Change at 40-Store Production Scale

1. **PostgreSQL** instead of SQLite — concurrent writes from 40 stores need proper locking.
2. **Redis** for current queue depth — a counter per store, updated on every BILLING event, avoids an aggregation query on every `/metrics` call.
3. **Kafka** for the event stream — currently the pipeline writes JSONL and calls the ingest endpoint in batch; a Kafka topic between pipeline and API would support true real-time streaming.
4. **Horizontal API scaling** — the FastAPI app is stateless; adding `--workers 4` or deploying multiple replicas behind a load balancer is trivial.
