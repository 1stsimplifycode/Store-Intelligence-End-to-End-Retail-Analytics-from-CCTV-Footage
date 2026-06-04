# PROMPT: Write pytest tests for retail anomaly detection endpoint. Cover: queue spike
# fires when depth exceeds threshold, conversion drop anomaly fires vs 7-day baseline,
# dead zone anomaly fires for zones with no recent visits, severity levels are correct,
# suggested_action is non-empty string for each anomaly type.
# CHANGES MADE: Anomaly thresholds imported from anomalies.py rather than hard-coded.
# Added test that severity escalates to CRITICAL at 2x threshold.
# Fixed DB isolation: each test uses fresh DB via api_client fixture.

import pytest
import pytest_asyncio
import os
import uuid

os.environ.setdefault("DB_PATH", "/tmp/test_anomalies_default.db")

from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture()
async def api_client(tmp_path):
    import app.database as _db_mod
    db_path = str(tmp_path / "anomalies_test.db")
    _db_mod._db_path_override = db_path
    from app.database import init_db
    await init_db(db_path)
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    _db_mod._db_path_override = None


def uid():
    return str(uuid.uuid4())


def bq_join(token: str, idx: int) -> dict:
    return {
        "event_id": uid(),
        "event_type": "BILLING_QUEUE_JOIN",
        "store_id": "ST1076",
        "store_code": "ST1076",
        "camera_id": "CAM_BILLING_01",
        "zone_id": "ZONE_BILLING",
        "zone_name": "Billing",
        "event_time": "2026-03-08T18:15:00Z",
        "event_timestamp": "2026-03-08T18:15:00Z",
        "id_token": token,
        "track_id": idx,
        "queue_position_at_join": idx,
        "abandoned": False,
        "is_staff": False,
        "confidence": 0.9,
    }


@pytest.mark.asyncio
async def test_anomalies_empty_store(api_client):
    resp = await api_client.get("/stores/ST_EMPTY/anomalies?date=2026-03-08")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["anomalies"], list)


@pytest.mark.asyncio
async def test_queue_spike_warn(api_client):
    """Queue depth ≥ threshold → BILLING_QUEUE_SPIKE WARN."""
    from app.routers.anomalies import QUEUE_SPIKE_THRESHOLD
    joins = [bq_join(f"ID_{i:05d}", i) for i in range(1, QUEUE_SPIKE_THRESHOLD + 1)]
    await api_client.post("/events/ingest", json={"events": joins})
    resp = await api_client.get("/stores/ST1076/anomalies?date=2026-03-08")
    data = resp.json()
    queue_anomalies = [a for a in data["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
    assert len(queue_anomalies) > 0
    assert queue_anomalies[0]["severity"] in ("WARN", "CRITICAL")
    assert len(queue_anomalies[0]["suggested_action"]) > 0


@pytest.mark.asyncio
async def test_queue_spike_critical(api_client):
    """Queue depth ≥ 2x threshold → BILLING_QUEUE_SPIKE CRITICAL."""
    from app.routers.anomalies import QUEUE_SPIKE_THRESHOLD
    joins = [bq_join(f"ID_{i:05d}", i) for i in range(1, QUEUE_SPIKE_THRESHOLD * 2 + 1)]
    await api_client.post("/events/ingest", json={"events": joins})
    resp = await api_client.get("/stores/ST1076/anomalies?date=2026-03-08")
    data = resp.json()
    queue_anomalies = [a for a in data["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
    assert any(a["severity"] == "CRITICAL" for a in queue_anomalies)


@pytest.mark.asyncio
async def test_anomaly_suggested_action_is_nonempty(api_client):
    from app.routers.anomalies import QUEUE_SPIKE_THRESHOLD
    joins = [bq_join(f"ID_{i:05d}", i) for i in range(1, QUEUE_SPIKE_THRESHOLD + 1)]
    await api_client.post("/events/ingest", json={"events": joins})
    resp = await api_client.get("/stores/ST1076/anomalies?date=2026-03-08")
    for a in resp.json()["anomalies"]:
        assert isinstance(a["suggested_action"], str)
        assert len(a["suggested_action"]) > 10


@pytest.mark.asyncio
async def test_anomaly_severity_valid_values(api_client):
    resp = await api_client.get("/stores/ST1076/anomalies?date=2026-03-08")
    for a in resp.json()["anomalies"]:
        assert a["severity"] in ("INFO", "WARN", "CRITICAL")
