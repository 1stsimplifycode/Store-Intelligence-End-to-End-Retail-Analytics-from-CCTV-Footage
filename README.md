<div align="center"><img src="https://raw.githubusercontent.com/1stsimplifycode/1stsimplifycode/main/assets/universal-project-3d.svg" width="100%" alt="Animated isometric engineering project showcase" /></div>

[← Engineering Portfolio](https://github.com/1stsimplifycode) · [Explore Domains](https://github.com/1stsimplifycode/1stsimplifycode/blob/main/docs/PROJECT_DIRECTORY.md)

# Store Intelligence — Purplle Tech Challenge 2026

End-to-end retail analytics: CCTV → Events → API → Dashboard.

## Quick Start (5 commands)

```bash
git clone <your-repo-url> store-intelligence && cd store-intelligence
cp -r /path/to/Store_2 ./data/                   # paste the footage folder here
docker compose up --build -d                      # starts API + dashboard
bash pipeline/run.sh ./data/Store_2 ST1076        # process clips → events → ingest
open http://localhost:8000/docs                   # explore the API
```

The dashboard auto-starts in the `dashboard` container. To view it:
```bash
docker compose logs -f dashboard
```

## Architecture

```
pipeline/
  detect.py      YOLOv8n detection + BYTETracker Re-ID
  tracker.py     Colour-histogram Re-ID (CPU, no GPU needed)
  emit.py        JSONL event emission
  run.sh         One command: clips → events → POST to API

app/
  main.py        FastAPI entrypoint + logging middleware
  database.py    aiosqlite / SQLite (swap to PostgreSQL: change DB_PATH)
  models.py      Pydantic intake (accepts both PS-spec and observed schemas)
  routers/
    events.py    POST /events/ingest  — idempotent, batch ≤ 500
    metrics.py   GET  /stores/{id}/metrics
    funnel.py    GET  /stores/{id}/funnel
    heatmap.py   GET  /stores/{id}/heatmap
    anomalies.py GET  /stores/{id}/anomalies
    health.py    GET  /health

dashboard/live.py   rich terminal dashboard, refreshes every 5s
```

## Running the Detection Pipeline

### Against a single clip
```bash
python -m pipeline.detect \
  --video ./data/Store_2/entry\ 1.mp4 \
  --store-id ST1076 \
  --camera-id CAM_ENTRY_01 \
  --camera-type entry \
  --layout ./data/Store_2/store_layout.json \
  --output ./events/ST1076_CAM_ENTRY_01.jsonl \
  --clip-start 2026-03-08T09:00:00Z \
  --model yolov8n.pt \
  --staff-colour blue
```

### Against all clips in a store folder
```bash
STAFF_COLOUR=blue bash pipeline/run.sh ./data/Store_2 ST1076
```

Output lands in `./events/`. The script automatically POSTs events to the API.

### Installing pipeline dependencies (not needed for API-only Docker run)
```bash
pip install ultralytics opencv-python-headless
```

## Ingesting the Sample Events

```bash
python3 -c "
import json, requests
events = [json.loads(l) for l in open('./data/sample_events.jsonl') if l.strip()]
r = requests.post('http://localhost:8000/events/ingest', json={'events': events})
print(r.json())
"
```

## Ingesting POS Data

```bash
python3 -c "
import csv, json, requests
rows = list(csv.DictReader(open('./data/POS_-_sample_transactions.csv')))
r = requests.post('http://localhost:8000/events/pos/ingest',
    json=[{'order_id': r['order_id'], 'store_id': r['store_id'],
           'order_date': r['order_date'], 'order_time': r['order_time'],
           'product_id': r.get('product_id',''),
           'brand_name': r.get('brand_name',''),
           'total_amount': float(r['total_amount'])} for r in rows])
print(r.json())
"
```

## API Reference

| Endpoint | Description |
|---|---|
| `POST /events/ingest` | Ingest ≤500 events. Idempotent by `event_id`. |
| `POST /events/pos/ingest` | Ingest POS transactions. |
| `GET /stores/{id}/metrics` | Visitors, conversion rate, queue depth, dwell. |
| `GET /stores/{id}/funnel` | 4-stage conversion funnel with drop-off %. |
| `GET /stores/{id}/heatmap` | Zone visit frequency + dwell, normalised 0–100. |
| `GET /stores/{id}/anomalies` | Active anomalies (queue spike, conversion drop, dead zone). |
| `GET /health` | Service status + STALE_FEED warnings per store. |

All endpoints accept an optional `?date=YYYY-MM-DD` query param (defaults to today UTC).

Interactive docs: `http://localhost:8000/docs`

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v --tb=short
```

Expected coverage: >70% of app/ statements.

## Edge Cases Handled

| Edge Case | How |
|---|---|
| Group entry | ByteTrack assigns distinct `track_id` per person; each emits its own ENTRY event |
| Staff exclusion | HSV torso histogram classifies uniform colour; `is_staff=true` events excluded from all metrics |
| Re-entry | Colour histogram Re-ID: if a new track at entry zone matches a recent exit embedding, a REENTRY event is emitted instead of ENTRY |
| Partial occlusion | Confidence floor set to 0.35 (not suppressed); low-conf events emitted with actual confidence value |
| Empty store | All metric queries return 0/0.0 on zero events, never crash or return null |
| Billing queue | Queue depth tracked as join-count minus exit-count; abandonment requires no following POS transaction |
| Cross-camera dedup | entry + floor cameras process separate JSONL files; visitor tokens keyed to `id_token` or `track_id` are scoped per camera |

## Dashboard (Part E)

The `dashboard` container runs `dashboard/live.py` — a `rich` terminal UI that polls
`/health`, `/metrics`, and `/anomalies` every 5 seconds and renders a live table.

To simulate real-time: run `pipeline/run.sh` while watching the dashboard. Events
ingested mid-processing will appear in the live table within the next 5-second refresh.

## Submission Notes

- Video files and dataset are not included in this repository per the competition rules.
- Place the store footage folders under `./data/` before running the pipeline.
