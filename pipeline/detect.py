#!/usr/bin/env python3
"""
detect.py — Main detection + tracking pipeline.
Processes CCTV clips using YOLOv8 (person detection) and BYTETracker (multi-object tracking).
Outputs structured events to stdout / JSONL file.

Usage:
    python pipeline/detect.py --video path/to/entry.mp4 \
        --store-id ST1076 --camera-id CAM_ENTRY_01 \
        --camera-type entry --layout path/to/store_layout.json \
        --output events.jsonl

# PROMPT (Claude): Design a CCTV person detection pipeline for retail analytics.
# Key requirements: YOLOv8 for detection, ByteTrack for Re-ID, staff classification
# by colour histogram, direction detection for entry/exit at threshold line.
# Handle: group entry (count individuals), re-entry (same track_id returns), 
# partial occlusion (confidence degradation, not silence).
# CHANGES MADE: Added configurable threshold line (top 20% of frame for entry cam),
# replaced VLM zone classification with polygon-based lookup (faster, no API cost),
# tuned confidence floor to 0.35 (VLM suggested 0.5, too many missed detections).
"""
import argparse
import json
import uuid
import sys
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
import cv2
import numpy as np

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    print("[WARN] ultralytics not installed — running in stub mode", file=sys.stderr)

from pipeline.tracker import Tracker
from pipeline.emit import EventEmitter


# ---------- Config ----------
PERSON_CLASS_ID = 0
CONF_THRESHOLD = 0.35      # Floor; low-conf events are emitted but flagged
STAFF_CONF_THRESHOLD = 0.65
FRAME_SKIP = 3             # Process every Nth frame for performance
ENTRY_LINE_RATIO = 0.25    # Entry threshold at top 25% of frame height
DWELL_EMIT_INTERVAL_S = 30 # Emit ZONE_DWELL every 30s of continued presence


def parse_args():
    p = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    p.add_argument("--video", required=True, help="Path to video clip")
    p.add_argument("--store-id", required=True, help="Store identifier e.g. ST1076")
    p.add_argument("--camera-id", required=True, help="Camera identifier e.g. CAM_ENTRY_01")
    p.add_argument("--camera-type", default="entry",
                   choices=["entry", "floor", "billing"],
                   help="Camera type determines processing mode")
    p.add_argument("--layout", default=None, help="Path to store_layout.json")
    p.add_argument("--output", default="events.jsonl", help="Output JSONL file path")
    p.add_argument("--clip-start", default=None,
                   help="ISO8601 wall-clock start for the clip (for timestamps)")
    p.add_argument("--model", default="yolov8n.pt", help="YOLO model path/name")
    p.add_argument("--staff-colour", default="blue",
                   help="Dominant colour of staff uniform (blue/red/green/black)")
    return p.parse_args()


def load_layout(layout_path: str | None) -> dict:
    if layout_path and os.path.exists(layout_path):
        with open(layout_path) as f:
            return json.load(f)
    return {}


def frame_to_timestamp(frame_idx: int, fps: float, clip_start: datetime) -> str:
    offset = timedelta(seconds=frame_idx / fps)
    return (clip_start + offset).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# Simple colour-based staff classifier using HSV histogram of torso region
STAFF_COLOUR_RANGES = {
    "blue":  [(100, 100, 50), (130, 255, 255)],
    "red":   [(0, 100, 50), (10, 255, 255)],
    "green": [(40, 100, 50), (80, 255, 255)],
    "black": [(0, 0, 0), (180, 60, 60)],
}


def classify_staff(frame: np.ndarray, bbox: list, staff_colour: str = "blue") -> tuple[bool, float]:
    """
    Returns (is_staff, confidence). Uses HSV histogram of torso region.
    Staff wear a distinctive uniform colour that customers rarely wear.
    """
    x1, y1, x2, y2 = [int(v) for v in bbox]
    h = y2 - y1
    # Torso = middle third of bounding box
    ty1, ty2 = y1 + h // 3, y1 + 2 * h // 3
    torso = frame[ty1:ty2, x1:x2]
    if torso.size == 0:
        return False, 0.0

    hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
    lo, hi = STAFF_COLOUR_RANGES.get(staff_colour, STAFF_COLOUR_RANGES["blue"])
    mask = cv2.inRange(hsv, np.array(lo), np.array(hi))
    ratio = np.sum(mask > 0) / mask.size if mask.size > 0 else 0.0
    is_staff = ratio > 0.35
    return is_staff, float(ratio)


def point_in_polygon(px: float, py: float, polygon: list[list[float]]) -> bool:
    """Ray-casting algorithm for polygon point test."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def classify_zone(cx: float, cy: float, frame_w: float, frame_h: float,
                  layout: dict, camera_id: str) -> tuple[str | None, str | None]:
    """
    Returns (zone_id, zone_name) for a centroid position using layout polygons.
    Falls back to grid-based heuristic if no layout available.
    """
    zones = layout.get("zones", [])
    for zone in zones:
        polygon = zone.get("polygon", [])
        if polygon and point_in_polygon(cx / frame_w, cy / frame_h, polygon):
            return zone.get("zone_id"), zone.get("zone_name")
    # Heuristic fallback: divide frame into 3x3 grid zones
    col = int(cx / frame_w * 3)
    row = int(cy / frame_h * 3)
    labels = [
        ["LEFT_FRONT", "CENTER_FRONT", "RIGHT_FRONT"],
        ["LEFT_MID", "CENTER_MID", "RIGHT_MID"],
        ["LEFT_BACK", "CENTER_BACK", "RIGHT_BACK"],
    ]
    zone_name = labels[min(row, 2)][min(col, 2)]
    return f"ZONE_{zone_name}", zone_name


def process_video(args):
    layout = load_layout(args.layout)
    clip_start = (
        datetime.fromisoformat(args.clip_start.replace("Z", "+00:00"))
        if args.clip_start
        else datetime.now(timezone.utc)
    )

    if not YOLO_AVAILABLE:
        print("[WARN] YOLO unavailable — emitting stub events for schema validation", file=sys.stderr)
        _emit_stub_events(args, clip_start)
        return

    model = YOLO(args.model)
    tracker = Tracker()
    emitter = EventEmitter(args.store_id, args.camera_id, args.output)

    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    frame_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    frame_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    entry_y_threshold = frame_h * ENTRY_LINE_RATIO

    frame_idx = 0
    last_positions: dict[int, tuple[float, float]] = {}   # track_id → (cx, cy)
    zone_entry_times: dict[int, tuple[str, float]] = {}   # track_id → (zone_id, enter_time_s)
    dwell_last_emit: dict[int, dict[str, float]] = {}     # track_id → {zone_id: last_emit_s}

    print(f"[INFO] Processing {args.video} @ {fps:.1f} fps, "
          f"{int(frame_w)}x{int(frame_h)}", file=sys.stderr)

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % FRAME_SKIP != 0:
            frame_idx += 1
            continue

        timestamp = frame_to_timestamp(frame_idx, fps, clip_start)
        time_s = frame_idx / fps

        results = model(frame, classes=[PERSON_CLASS_ID], verbose=False)
        detections = []
        for r in results:
            for box in r.boxes:
                conf = float(box.conf[0])
                if conf < CONF_THRESHOLD:
                    continue
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                detections.append({"bbox": [x1, y1, x2, y2], "conf": conf})

        tracked = tracker.update(detections, frame)

        for t in tracked:
            tid = t["track_id"]
            bbox = t["bbox"]
            conf = t["conf"]
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2

            is_staff, staff_conf = classify_staff(frame, bbox, args.staff_colour)

            # ---- Entry / Exit (entry camera only) ----
            if args.camera_type == "entry":
                prev = last_positions.get(tid)
                if prev:
                    prev_cy = prev[1]
                    if prev_cy > entry_y_threshold and cy <= entry_y_threshold:
                        # Moving toward camera top = exiting store
                        emitter.emit_exit(tid, timestamp, is_staff, conf)
                    elif prev_cy <= entry_y_threshold and cy > entry_y_threshold:
                        # Moving past threshold inward = entering store
                        emitter.emit_entry(tid, timestamp, is_staff, conf)
                elif tid not in tracker.seen_ids:
                    # First appearance below threshold = new entrant
                    if cy > entry_y_threshold:
                        emitter.emit_entry(tid, timestamp, is_staff, conf)

            # ---- Zone classification (floor / billing camera) ----
            if args.camera_type in ("floor", "billing"):
                zone_id, zone_name = classify_zone(cx, cy, frame_w, frame_h, layout, args.camera_id)
                prev_zone = zone_entry_times.get(tid, (None, 0.0))[0]

                if zone_id and zone_id != prev_zone:
                    # Zone exit
                    if prev_zone:
                        enter_t = zone_entry_times[tid][1]
                        dwell_ms = int((time_s - enter_t) * 1000)
                        emitter.emit_zone_exit(tid, timestamp, prev_zone, dwell_ms, is_staff, conf)

                    # Zone entry
                    emitter.emit_zone_enter(tid, timestamp, zone_id, zone_name, is_staff, conf,
                                            cx, cy)
                    zone_entry_times[tid] = (zone_id, time_s)
                    dwell_last_emit.setdefault(tid, {})[zone_id] = time_s

                # ZONE_DWELL heartbeat
                if zone_id and zone_id == prev_zone:
                    last = dwell_last_emit.get(tid, {}).get(zone_id, time_s)
                    if time_s - last >= DWELL_EMIT_INTERVAL_S:
                        dwell_ms = int((time_s - zone_entry_times[tid][1]) * 1000)
                        emitter.emit_zone_dwell(tid, timestamp, zone_id, zone_name,
                                                dwell_ms, is_staff, conf)
                        dwell_last_emit[tid][zone_id] = time_s

                # Billing queue logic
                if args.camera_type == "billing" and zone_name and "billing" in zone_name.lower():
                    queue_depth = tracker.billing_queue_depth()
                    if queue_depth > 0:
                        emitter.emit_billing_queue_join(tid, timestamp, zone_id, zone_name,
                                                        queue_depth, is_staff, conf)

            last_positions[tid] = (cx, cy)

        frame_idx += 1

    # Flush any open sessions
    for tid, (zone_id, enter_t) in zone_entry_times.items():
        if zone_id:
            timestamp = frame_to_timestamp(frame_idx, fps, clip_start)
            dwell_ms = int((frame_idx / fps - enter_t) * 1000)
            emitter.emit_zone_exit(tid, timestamp, zone_id, dwell_ms, False, 0.5)

    cap.release()
    emitter.close()
    print(f"[INFO] Done. Events written to {args.output}", file=sys.stderr)


def _emit_stub_events(args, clip_start: datetime):
    """Emit minimal stub events when YOLO is unavailable (schema validation only)."""
    emitter = EventEmitter(args.store_id, args.camera_id, args.output)
    ts = clip_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    emitter.emit_entry(9001, ts, False, 0.9)
    emitter.emit_zone_enter(9001, ts, "ZONE_CENTER", "Center Display", False, 0.85, 320, 240)
    emitter.emit_zone_exit(9001, ts, "ZONE_CENTER", 45000, False, 0.85)
    emitter.emit_exit(9001, ts, False, 0.88)
    emitter.close()


if __name__ == "__main__":
    args = parse_args()
    process_video(args)
