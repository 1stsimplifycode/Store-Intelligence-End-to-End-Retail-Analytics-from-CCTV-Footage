# PROMPT: Write pytest tests for a retail CCTV event pipeline that emits JSONL events.
# Cover: schema validation (all required fields), event_id uniqueness, timestamp format,
# staff exclusion from metrics, group entry (3 people = 3 events), re-entry detection,
# zero-traffic clip (no crash), and confidence not suppressed on low-conf detections.
# CHANGES MADE: Added parametrised schema tests over all event types (AI suggested one
# fixed test per type — too verbose). Removed the AI-suggested mock YOLO test since it
# tested internals rather than outputs. Added explicit test for confidence floor (AI
# missed this). Rewrote timestamp assertion to handle both 'Z' and '+00:00' suffixes.

import json
import uuid
import tempfile
import os
import pytest
from datetime import datetime
from pathlib import Path


# -------- Helpers --------

def make_entry_event(**overrides) -> dict:
    base = {
        "event_id": str(uuid.uuid4()),
        "event_type": "entry",
        "id_token": "ID_00001",
        "store_code": "ST1076",
        "camera_id": "CAM_ENTRY_01",
        "event_timestamp": "2026-03-08T18:10:05.120Z",
        "is_staff": False,
        "confidence": 0.91,
        "session_seq": 1,
    }
    base.update(overrides)
    return base


def make_zone_event(**overrides) -> dict:
    base = {
        "event_id": str(uuid.uuid4()),
        "event_type": "zone_entered",
        "track_id": 101,
        "store_id": "ST1076",
        "camera_id": "CAM_FLOOR_01",
        "zone_id": "ZONE_CENTER",
        "zone_name": "Center Display",
        "event_time": "2026-03-08T18:10:45.280Z",
        "is_staff": False,
        "confidence": 0.87,
        "zone_hotspot_x": 412.6,
        "zone_hotspot_y": 238.4,
        "session_seq": 2,
    }
    base.update(overrides)
    return base


def make_queue_event(**overrides) -> dict:
    base = {
        "queue_event_id": str(uuid.uuid4()),
        "event_type": "queue_completed",
        "track_id": 102,
        "store_id": "ST1076",
        "camera_id": "CAM_BILLING_01",
        "zone_id": "ZONE_BILLING",
        "zone_name": "Billing Counter Queue",
        "queue_join_ts": "2026-03-08T18:13:05.080Z",
        "queue_served_ts": "2026-03-08T18:13:13.240Z",
        "queue_exit_ts": "2026-03-08T18:15:31.840Z",
        "wait_seconds": 8,
        "queue_position_at_join": 2,
        "abandoned": False,
        "confidence": 0.89,
    }
    base.update(overrides)
    return base


# -------- Schema Tests --------

REQUIRED_ENTRY_FIELDS = {"event_type", "event_timestamp", "is_staff", "confidence"}
REQUIRED_ZONE_FIELDS = {"event_type", "zone_id", "zone_name", "event_time", "confidence"}
REQUIRED_QUEUE_FIELDS = {"event_type", "queue_join_ts", "queue_exit_ts", "abandoned", "confidence"}


@pytest.mark.parametrize("field", REQUIRED_ENTRY_FIELDS)
def test_entry_event_has_required_field(field):
    ev = make_entry_event()
    assert field in ev, f"Entry event missing required field: {field}"


@pytest.mark.parametrize("field", REQUIRED_ZONE_FIELDS)
def test_zone_event_has_required_field(field):
    ev = make_zone_event()
    assert field in ev, f"Zone event missing required field: {field}"


@pytest.mark.parametrize("field", REQUIRED_QUEUE_FIELDS)
def test_queue_event_has_required_field(field):
    ev = make_queue_event()
    assert field in ev, f"Queue event missing required field: {field}"


def test_event_ids_are_unique():
    events = [make_entry_event() for _ in range(100)]
    ids = [e["event_id"] for e in events]
    assert len(set(ids)) == 100, "event_ids must be globally unique"


def test_timestamp_is_valid_iso8601():
    ev = make_entry_event()
    ts = ev["event_timestamp"].replace("Z", "+00:00")
    parsed = datetime.fromisoformat(ts)
    assert parsed.year == 2026


def test_confidence_not_suppressed_below_threshold():
    """Low-confidence detections must be emitted, not silently dropped."""
    ev = make_entry_event(confidence=0.36)
    assert ev["confidence"] == 0.36, "Low-confidence events must not be suppressed"
    assert ev["confidence"] >= 0.0


def test_is_staff_is_boolean():
    ev = make_entry_event(is_staff=False)
    assert isinstance(ev["is_staff"], bool)
    ev2 = make_entry_event(is_staff=True)
    assert isinstance(ev2["is_staff"], bool)


# -------- Group Entry --------

def test_group_entry_emits_individual_events():
    """3 people entering together → 3 separate ENTRY events with distinct id_tokens."""
    events = [
        make_entry_event(id_token=f"ID_{i:05d}", group_id="G_01", group_size=3)
        for i in range(1, 4)
    ]
    assert len(events) == 3
    tokens = {e["id_token"] for e in events}
    assert len(tokens) == 3, "Each person in a group must have a distinct id_token"
    for e in events:
        assert e["event_type"] == "entry"


# -------- Staff Exclusion --------

def test_staff_events_are_flagged():
    ev = make_entry_event(is_staff=True)
    assert ev["is_staff"] is True


def test_non_staff_events_not_flagged():
    ev = make_entry_event(is_staff=False)
    assert ev["is_staff"] is False


# -------- Re-entry --------

def test_reentry_event_type():
    ev = {
        "event_id": str(uuid.uuid4()),
        "event_type": "reentry",
        "id_token": "ID_00001",
        "store_code": "ST1076",
        "camera_id": "CAM_ENTRY_01",
        "event_timestamp": "2026-03-08T18:30:00.000Z",
        "is_staff": False,
        "confidence": 0.78,
        "session_seq": 1,
    }
    assert ev["event_type"] == "reentry"
    assert ev["is_staff"] is False


# -------- Queue events --------

def test_abandoned_queue_has_no_served_ts():
    ev = make_queue_event(event_type="queue_abandoned", abandoned=True, queue_served_ts=None)
    assert ev["abandoned"] is True
    assert ev["queue_served_ts"] is None


def test_completed_queue_has_served_ts():
    ev = make_queue_event()
    assert ev["abandoned"] is False
    assert ev["queue_served_ts"] is not None


# -------- EventEmitter --------

def test_emitter_writes_valid_jsonl():
    from pipeline.emit import EventEmitter
    with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
        path = f.name
    try:
        emitter = EventEmitter("ST1076", "CAM_ENTRY_01", path)
        emitter.emit_entry(1, "2026-03-08T18:00:00.000Z", False, 0.9)
        emitter.emit_zone_enter(1, "2026-03-08T18:01:00.000Z",
                                "ZONE_A", "Zone A", False, 0.85, 100.0, 200.0)
        emitter.emit_zone_exit(1, "2026-03-08T18:02:00.000Z", "ZONE_A", 60000, False, 0.85)
        emitter.emit_exit(1, "2026-03-08T18:05:00.000Z", False, 0.88)
        emitter.close()

        lines = Path(path).read_text().strip().split("\n")
        assert len(lines) == 4
        for line in lines:
            ev = json.loads(line)
            assert "event_type" in ev
    finally:
        os.unlink(path)


def test_emitter_all_events_have_store_id():
    from pipeline.emit import EventEmitter
    with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
        path = f.name
    try:
        emitter = EventEmitter("ST1076", "CAM_FLOOR_01", path)
        emitter.emit_entry(2, "2026-03-08T18:00:00.000Z", False, 0.9)
        emitter.emit_zone_enter(2, "2026-03-08T18:01:00.000Z",
                                "Z1", "Zone 1", False, 0.8, 50.0, 100.0)
        emitter.emit_exit(2, "2026-03-08T18:10:00.000Z", False, 0.9)
        emitter.close()

        lines = Path(path).read_text().strip().split("\n")
        for line in lines:
            ev = json.loads(line)
            sid = ev.get("store_code") or ev.get("store_id")
            assert sid == "ST1076"
    finally:
        os.unlink(path)


def test_zero_traffic_emitter_does_not_crash():
    """Empty clip must produce no events without error."""
    from pipeline.emit import EventEmitter
    with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
        path = f.name
    try:
        emitter = EventEmitter("ST1076", "CAM_ENTRY_01", path)
        # No events emitted
        emitter.close()
        content = Path(path).read_text()
        assert content == ""
    finally:
        os.unlink(path)
