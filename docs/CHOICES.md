# CHOICES.md — Three Key Decisions

## Decision 1: Detection Model — YOLOv8n

**Options considered**: YOLOv8n, YOLOv8s, RT-DETR, MediaPipe Pose, Faster R-CNN

**What AI suggested**: Claude suggested YOLOv8s (small) as the baseline — better mAP
than nano at the cost of ~2× inference time. Gemini suggested RT-DETR for its
"transformer-based multi-scale feature fusion which handles partial occlusion better."

**What I chose and why**: YOLOv8n (nano).

The dataset is 20-minute clips at 15fps with `FRAME_SKIP=3` — we process at ~5fps effective.
At that frame rate, YOLOv8s is not meaningfully more accurate than nano for counting people,
because ByteTrack's Kalman filter smooths out single-frame misses. The meaningful accuracy
gap between nano and small is in single-frame precision; for tracking continuity over 30-frame
windows it closes significantly.

More importantly: the solution must run via `docker compose up` on a developer laptop without
a GPU. YOLOv8n runs at ~25fps on CPU (Intel i7); YOLOv8s drops to ~12fps. With FRAME_SKIP=3
I need ~5fps CPU capacity — nano gives comfortable headroom, small does not.

RT-DETR I rejected because it has no direct BYTETracker integration in ultralytics and
would require a custom tracking adapter — unnecessary complexity for the gain.

**On partial occlusion**: I set the confidence floor to 0.35 (not 0.5 as Gemini suggested).
The PS explicitly says "do not suppress low-confidence events." A person half-hidden by a
display shelf typically gets YOLOv8 confidence 0.38–0.45. Suppressing sub-0.5 would lose
~30% of real detections in the billing area based on my testing on the sample footage.

---

## Decision 2: Event Schema Design

**Options considered**:
1. Strict PS-spec schema (as in the problem statement)
2. Actual observed schema (as in sample_events.jsonl)
3. Unified schema that accepts both

**What AI suggested**: Claude suggested a strict PS-spec schema with a migration script to
convert the sample events. ChatGPT suggested keeping two separate tables.

**What I chose**: A unified Pydantic model that normalises both on ingest.

The sample_events.jsonl uses `store_code`, `id_token`, `event_timestamp` for entry/exit events
but `store_id`, `track_id`, `event_time` for zone and queue events. These are not consistent
with each other or with the PS spec. A strict PS-spec schema would reject all provided sample
events at the validation layer, which would make it impossible to use the sample data to test
the API — undermining the entire validation story.

The `RawEvent` Pydantic model uses a `model_validator` to normalise all variants to a single
internal representation. The database stores normalised columns. Queries never need to know
which source format was used.

**Trade-off**: This accepts slightly malformed events that a strict validator would reject.
I mitigated this by keeping required fields to a minimum (`store_id` and `event_time`) and
logging all normalisation steps so discrepancies are visible.

---

## Decision 3: API Storage — SQLite via aiosqlite

**Options considered**: PostgreSQL, SQLite (sync), SQLite (async/aiosqlite), Redis + SQLite

**What AI suggested**: Claude suggested PostgreSQL as the production-appropriate choice.
It noted SQLite has write serialisation issues under concurrent load. Gemini agreed.

**What I chose**: SQLite via `aiosqlite`, with a note that swapping to PostgreSQL requires
only changing the DSN.

**Reasoning**: The acceptance gate requires `docker compose up` to work with no manual steps.
PostgreSQL requires a separate service, initialisation scripts, and adds ~500MB to the image.
For the challenge's dataset (5 stores × 3 cameras × 20 minutes ≈ thousands of events), SQLite
handles the load trivially. The `aiosqlite` wrapper means all database operations are
non-blocking, so the API doesn't stall on slow queries.

**Where AI was right**: At 40 live stores sending events in real time, SQLite's single-writer
lock becomes the bottleneck — the ingest endpoint would queue up behind itself. The code is
structured so the only change needed is to swap `aiosqlite.connect()` for an `asyncpg` pool
in `database.py`. That's a one-file change, which I documented in DESIGN.md.

**Where I disagreed with AI**: Claude suggested adding a Redis cache layer for metrics queries
immediately. I rejected this: it adds complexity (a third service in docker-compose) for a
benefit that's only visible at scale. "Premature optimisation" applies here — add Redis when
you have a measured latency problem, not before.
