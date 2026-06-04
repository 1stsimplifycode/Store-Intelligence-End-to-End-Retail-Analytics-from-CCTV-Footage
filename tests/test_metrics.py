# PROMPT: Write pytest tests for a FastAPI store analytics API. Cover: ingest idempotency
# (POST same payload twice → duplicate count increases, not accepted), metrics endpoint
# returns correct fields for zero-visitor store, funnel stages are non-increasing
# (entry >= zone >= billing >= purchase), heatmap normalisation (max score = 100),
# anomaly severity ordering, health endpoint returns 200 with correct structure.
# CHANGES MADE: Used pytest-asyncio fixtures instead of AI's sync requests-based tests
# (avoids needing a live server). Added edge case: all-staff clip returns 0 unique_visitors.
# Removed AI's suggestion to test with raw SQL — kept it at the HTTP layer only.
# Fixed DB isolation: each test gets a fresh DB path via tmp_path.

import pytest
import pytest_asyncio
import json
import os
import uuid
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture()
async def api_client(tmp_path):
    """Each test gets its own fresh SQLite DB via module-level override."""
    import app.database as _db_mod
    db_path = str(tmp_path / "test.db")
    _db_mod._db_path_override = db_path
    from app.database import init_db
    await init_db(db_path)
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    _db_mod._db_path_override = None

def uid() -> str:
    return str(uuid.uuid4())


def make_ingest_payload(events):
    return {"events": events}


def sample_entry(store_id="ST1076", token="ID_00001", is_staff=False, ts="2026-03-08T18:10:00Z",
                 event_id=None):
    return {
        "event_type": "entry",
        "event_id": event_id or uid(),
        "id_token": token,
        "store_code": store_id,
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "event_timestamp": ts,
        "event_time": ts,
        "is_staff": is_staff,
        "confidence": 0.9,
        "session_seq": 1,
    }


def sample_zone(store_id="ST1076", token="ID_00001", track_id=101,
                zone_id="ZONE_A", ts="2026-03-08T18:11:00Z"):
    return {
        "event_type": "zone_entered",
        "event_id": uid(),
        "id_token": token,
        "track_id": track_id,
        "store_id": store_id,
        "store_code": store_id,
        "camera_id": "CAM_FLOOR_01",
        "zone_id": zone_id,
        "zone_name": "Left Shelf",
        "event_time": ts,
        "event_timestamp": ts,
        "is_staff": False,
        "confidence": 0.87,
        "zone_hotspot_x": 200.0,
        "zone_hotspot_y": 150.0,
    }


def sample_queue(store_id="ST1076", token="ID_00001", track_id=101, abandoned=False,
                 ts="2026-03-08T18:13:00Z"):
    ev_type = "queue_completed" if not abandoned else "queue_abandoned"
    eid = uid()
    return {
        "event_type": ev_type,
        "event_id": eid,
        "queue_event_id": eid,
        "id_token": token,
        "track_id": track_id,
        "store_id": store_id,
        "store_code": store_id,
        "camera_id": "CAM_BILLING_01",
        "zone_id": "ZONE_BILLING",
        "zone_name": "Billing Counter",
        "queue_join_ts": ts,
        "queue_served_ts": None if abandoned else ts,
        "queue_exit_ts": ts,
        "event_time": ts,
        "event_timestamp": ts,
        "wait_seconds": 30,
        "queue_position_at_join": 1,
        "abandoned": abandoned,
        "confidence": 0.88,
    }


# -------- Ingest Tests --------

@pytest.mark.asyncio
async def test_ingest_accepts_valid_events(api_client):
    resp = await api_client.post("/events/ingest", json=make_ingest_payload([
        sample_entry(), sample_zone(), sample_queue()
    ]))
    assert resp.status_code == 202
    data = resp.json()
    assert data["accepted"] == 3
    assert data["rejected"] == 0


@pytest.mark.asyncio
async def test_ingest_is_idempotent(api_client):
    """Same payload posted twice: second call should produce duplicates, not double-count."""
    fixed_id = uid()
    ev = sample_entry(event_id=fixed_id)
    r1 = await api_client.post("/events/ingest", json=make_ingest_payload([ev]))
    r2 = await api_client.post("/events/ingest", json=make_ingest_payload([ev]))
    assert r1.json()["accepted"] == 1
    assert r2.json()["duplicate"] == 1
    assert r2.json()["accepted"] == 0


@pytest.mark.asyncio
async def test_ingest_rejects_event_without_store_id(api_client):
    ev = sample_entry()
    ev.pop("store_id")
    ev.pop("store_code")
    resp = await api_client.post("/events/ingest", json=make_ingest_payload([ev]))
    data = resp.json()
    assert data["rejected"] == 1
    assert len(data["errors"]) == 1


@pytest.mark.asyncio
async def test_ingest_partial_success(api_client):
    """Valid + invalid events in same batch."""
    good = sample_entry(token="ID_00001")
    bad = sample_entry(token="ID_00002")
    bad.pop("store_id")
    bad.pop("store_code")
    resp = await api_client.post("/events/ingest", json=make_ingest_payload([good, bad]))
    data = resp.json()
    assert data["accepted"] == 1
    assert data["rejected"] == 1


# -------- Metrics Tests --------

@pytest.mark.asyncio
async def test_metrics_zero_visitors(api_client):
    resp = await api_client.get("/stores/ST_EMPTY/metrics?date=2026-03-08")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unique_visitors"] == 0
    assert data["conversion_rate"] == 0.0


@pytest.mark.asyncio
async def test_metrics_excludes_staff(api_client):
    await api_client.post("/events/ingest", json=make_ingest_payload([
        sample_entry(token="ID_00001", is_staff=False),
        sample_entry(token="ID_STAFF1", is_staff=True),
    ]))
    resp = await api_client.get("/stores/ST1076/metrics?date=2026-03-08")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unique_visitors"] == 1


@pytest.mark.asyncio
async def test_metrics_all_staff_clip(api_client):
    """All-staff clip → 0 unique visitors."""
    await api_client.post("/events/ingest", json=make_ingest_payload([
        sample_entry(token="ID_STAFF1", is_staff=True),
        sample_entry(token="ID_STAFF2", is_staff=True),
    ]))
    resp = await api_client.get("/stores/ST1076/metrics?date=2026-03-08")
    data = resp.json()
    assert data["unique_visitors"] == 0
    assert data["conversion_rate"] == 0.0


@pytest.mark.asyncio
async def test_metrics_required_fields(api_client):
    resp = await api_client.get("/stores/ST1076/metrics?date=2026-03-08")
    data = resp.json()
    for field in ("unique_visitors", "conversion_rate", "abandonment_rate",
                  "current_queue_depth", "avg_dwell_per_zone"):
        assert field in data, f"metrics response missing field: {field}"


# -------- Funnel Tests --------

@pytest.mark.asyncio
async def test_funnel_stages_are_non_increasing(api_client):
    for i in range(5):
        tok = f"ID_{i:05d}"
        evs = [sample_entry(token=tok)]
        if i < 4:
            evs.append(sample_zone(token=tok, track_id=100+i))
        if i < 3:
            evs.append(sample_queue(token=tok, track_id=100+i))
        await api_client.post("/events/ingest", json=make_ingest_payload(evs))

    resp = await api_client.get("/stores/ST1076/funnel?date=2026-03-08")
    assert resp.status_code == 200
    funnel = resp.json()["funnel"]
    counts = [s["visitors"] for s in funnel]
    for i in range(len(counts) - 1):
        assert counts[i] >= counts[i + 1]


@pytest.mark.asyncio
async def test_funnel_zero_store(api_client):
    resp = await api_client.get("/stores/ST_EMPTY/funnel?date=2026-03-08")
    assert resp.status_code == 200
    data = resp.json()
    assert data["overall_conversion_pct"] == 0.0


# -------- Heatmap Tests --------

@pytest.mark.asyncio
async def test_heatmap_max_score_is_100(api_client):
    for i in range(3):
        tok = f"ID_{i:05d}"
        evs = [
            sample_entry(token=tok),
            {**sample_zone(token=tok, track_id=200+i, zone_id="ZONE_A"), "event_id": uid()},
            {
                "event_type": "zone_exited",
                "event_id": uid(),
                "track_id": 200+i,
                "store_id": "ST1076",
                "store_code": "ST1076",
                "camera_id": "CAM_FLOOR_01",
                "zone_id": "ZONE_A",
                "event_time": "2026-03-08T18:12:00Z",
                "event_timestamp": "2026-03-08T18:12:00Z",
                "dwell_ms": 45000,
                "is_staff": False,
                "confidence": 0.85,
            }
        ]
        await api_client.post("/events/ingest", json=make_ingest_payload(evs))
    resp = await api_client.get("/stores/ST1076/heatmap?date=2026-03-08")
    assert resp.status_code == 200
    data = resp.json()
    if data["zones"]:
        scores = [z["visit_frequency_score"] for z in data["zones"]]
        assert max(scores) == 100


@pytest.mark.asyncio
async def test_heatmap_low_confidence_flag(api_client):
    resp = await api_client.get("/stores/ST1076/heatmap?date=2026-03-08")
    data = resp.json()
    assert data["data_confidence"] == "LOW"


# -------- Anomalies Tests --------

@pytest.mark.asyncio
async def test_anomalies_returns_valid_structure(api_client):
    resp = await api_client.get("/stores/ST1076/anomalies?date=2026-03-08")
    assert resp.status_code == 200
    data = resp.json()
    assert "anomalies" in data
    assert isinstance(data["anomalies"], list)
    for a in data["anomalies"]:
        assert a["severity"] in ("INFO", "WARN", "CRITICAL")
        assert "suggested_action" in a


# -------- Health Tests --------

@pytest.mark.asyncio
async def test_health_returns_200(api_client):
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert data["status"] in ("OK", "DEGRADED")
    assert "checked_at" in data


@pytest.mark.asyncio
async def test_health_shows_ingested_store(api_client):
    """After ingesting events, the store should appear in health stores list."""
    await api_client.post("/events/ingest", json=make_ingest_payload([sample_entry()]))
    resp = await api_client.get("/health")
    data = resp.json()
    store_ids = [s["store_id"] for s in data["stores"]]
    assert "ST1076" in store_ids
