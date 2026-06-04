"""
emit.py — Event schema definition and JSONL emission.
Produces events matching the actual sample_events.jsonl schema observed in the dataset.
"""
import json
import uuid
from typing import Optional


class EventEmitter:
    def __init__(self, store_id: str, camera_id: str, output_path: str):
        self.store_id = store_id
        self.camera_id = camera_id
        self._fh = open(output_path, "a", buffering=1)  # line-buffered
        self._session_seq: dict[int, int] = {}  # track_id → event count in session

    def _seq(self, track_id: int) -> int:
        self._session_seq[track_id] = self._session_seq.get(track_id, 0) + 1
        return self._session_seq[track_id]

    def _write(self, event: dict):
        self._fh.write(json.dumps(event) + "\n")

    # ---- Entry / Exit ----
    def emit_entry(self, track_id: int, timestamp: str, is_staff: bool, confidence: float):
        self._session_seq[track_id] = 0
        self._write({
            "event_id": str(uuid.uuid4()),
            "event_type": "entry",
            "id_token": f"ID_{track_id:05d}",
            "store_code": self.store_id,
            "camera_id": self.camera_id,
            "event_timestamp": timestamp,
            "is_staff": is_staff,
            "confidence": round(confidence, 4),
            "session_seq": self._seq(track_id),
        })

    def emit_exit(self, track_id: int, timestamp: str, is_staff: bool, confidence: float):
        self._write({
            "event_id": str(uuid.uuid4()),
            "event_type": "exit",
            "id_token": f"ID_{track_id:05d}",
            "store_code": self.store_id,
            "camera_id": self.camera_id,
            "event_timestamp": timestamp,
            "is_staff": is_staff,
            "confidence": round(confidence, 4),
            "session_seq": self._seq(track_id),
        })

    def emit_reentry(self, track_id: int, timestamp: str, confidence: float):
        self._session_seq[track_id] = 0
        self._write({
            "event_id": str(uuid.uuid4()),
            "event_type": "reentry",
            "id_token": f"ID_{track_id:05d}",
            "store_code": self.store_id,
            "camera_id": self.camera_id,
            "event_timestamp": timestamp,
            "is_staff": False,
            "confidence": round(confidence, 4),
            "session_seq": 1,
        })

    # ---- Zone events ----
    def emit_zone_enter(self, track_id: int, timestamp: str, zone_id: str, zone_name: str,
                        is_staff: bool, confidence: float, cx: float = 0.0, cy: float = 0.0):
        self._write({
            "event_id": str(uuid.uuid4()),
            "event_type": "zone_entered",
            "track_id": track_id,
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "zone_id": zone_id,
            "zone_name": zone_name,
            "event_time": timestamp,
            "is_staff": is_staff,
            "confidence": round(confidence, 4),
            "zone_hotspot_x": round(cx, 1),
            "zone_hotspot_y": round(cy, 1),
            "session_seq": self._seq(track_id),
        })

    def emit_zone_exit(self, track_id: int, timestamp: str, zone_id: str,
                       dwell_ms: int, is_staff: bool, confidence: float):
        self._write({
            "event_id": str(uuid.uuid4()),
            "event_type": "zone_exited",
            "track_id": track_id,
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "zone_id": zone_id,
            "event_time": timestamp,
            "dwell_ms": dwell_ms,
            "is_staff": is_staff,
            "confidence": round(confidence, 4),
            "session_seq": self._seq(track_id),
        })

    def emit_zone_dwell(self, track_id: int, timestamp: str, zone_id: str, zone_name: str,
                        dwell_ms: int, is_staff: bool, confidence: float):
        self._write({
            "event_id": str(uuid.uuid4()),
            "event_type": "ZONE_DWELL",
            "track_id": track_id,
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "zone_id": zone_id,
            "zone_name": zone_name,
            "event_time": timestamp,
            "dwell_ms": dwell_ms,
            "is_staff": is_staff,
            "confidence": round(confidence, 4),
            "session_seq": self._seq(track_id),
        })

    # ---- Billing / Queue ----
    def emit_billing_queue_join(self, track_id: int, timestamp: str, zone_id: str,
                                 zone_name: str, queue_depth: int,
                                 is_staff: bool, confidence: float):
        self._write({
            "queue_event_id": str(uuid.uuid4()),
            "event_type": "BILLING_QUEUE_JOIN",
            "track_id": track_id,
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "zone_id": zone_id,
            "zone_name": zone_name,
            "event_time": timestamp,
            "queue_position_at_join": queue_depth,
            "is_staff": is_staff,
            "abandoned": False,
            "confidence": round(confidence, 4),
            "session_seq": self._seq(track_id),
        })

    def emit_queue_completed(self, track_id: int, join_ts: str, served_ts: str,
                              exit_ts: str, wait_s: int, queue_pos: int,
                              zone_id: str, zone_name: str,
                              cx: float, cy: float, confidence: float):
        self._write({
            "queue_event_id": str(uuid.uuid4()),
            "event_type": "queue_completed",
            "track_id": track_id,
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "zone_id": zone_id,
            "zone_name": zone_name,
            "queue_join_ts": join_ts,
            "queue_served_ts": served_ts,
            "queue_exit_ts": exit_ts,
            "wait_seconds": wait_s,
            "queue_position_at_join": queue_pos,
            "abandoned": False,
            "zone_hotspot_x": round(cx, 1),
            "zone_hotspot_y": round(cy, 1),
            "confidence": round(confidence, 4),
        })

    def emit_queue_abandoned(self, track_id: int, join_ts: str, exit_ts: str,
                              wait_s: int, queue_pos: int,
                              zone_id: str, zone_name: str,
                              cx: float, cy: float, confidence: float):
        self._write({
            "queue_event_id": str(uuid.uuid4()),
            "event_type": "queue_abandoned",
            "track_id": track_id,
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "zone_id": zone_id,
            "zone_name": zone_name,
            "queue_join_ts": join_ts,
            "queue_served_ts": None,
            "queue_exit_ts": exit_ts,
            "wait_seconds": wait_s,
            "queue_position_at_join": queue_pos,
            "abandoned": True,
            "zone_hotspot_x": round(cx, 1),
            "zone_hotspot_y": round(cy, 1),
            "confidence": round(confidence, 4),
        })

    def close(self):
        self._fh.close()
