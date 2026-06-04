#!/usr/bin/env python3
"""
run_store1.py — Run detection on Store 1 footage using OpenCV HOG person detector.
No YOLO download needed — HOG detector is built into OpenCV.

Processes all 4 cameras, generates events.jsonl, injects into API.

Usage:
    python3 run_store1.py [--api http://localhost:8000] [--output ./events]
"""
import cv2
import json
import uuid
import os
import sys
import argparse
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Config ─────────────────────────────────────────────────────────────────
STORE_ID      = "ST1008"
CLIP_DATE     = "2026-04-10"
CLIP_START_HH = 20  # 20:xx based on timestamp overlay in footage
CLIP_START_MM = 9

FRAME_SKIP    = 10      # process every Nth frame (30fps → ~3fps effective)
CONF_FLOOR    = 0.45    # HOG confidence floor
STAFF_BLACK_THRESH = 0.30  # fraction of dark pixels = staff in black uniform
IOU_MATCH_THRESH   = 0.30  # IoU to re-associate tracks
REENTRY_TIMEOUT_S  = 60    # seconds after exit before re-appearance = re-entry
DWELL_EMIT_S       = 30    # emit ZONE_DWELL every N seconds

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data", "Store_1")
LAYOUT_FILE = os.path.join(DATA_DIR, "store_layout.json")

CLIPS = [
    ("CAM 3 - entry.mp4",   "CAM_ENTRY_01",   "entry"),
    ("CAM 1 - zone.mp4",    "CAM_FLOOR_01",   "floor"),
    ("CAM 2 - zone.mp4",    "CAM_FLOOR_02",   "floor"),
    ("CAM 5 - billing.mp4", "CAM_BILLING_01", "billing"),
]

# ── HOG detector ───────────────────────────────────────────────────────────
HOG = cv2.HOGDescriptor()
HOG.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())


# ── Helpers ────────────────────────────────────────────────────────────────

def load_layout():
    with open(LAYOUT_FILE) as f:
        return json.load(f)


def ts_from_frame(frame_idx: int, fps: float, cam_id: str) -> str:
    """Convert frame index → ISO8601 wall-clock timestamp using clip date."""
    # Use offset per camera based on observed overlays:
    # entry: ~20:09, zone1/zone2: ~20:10-11, billing: ~20:10
    offsets = {
        "CAM_ENTRY_01":   timedelta(hours=20, minutes=9),
        "CAM_FLOOR_01":   timedelta(hours=20, minutes=10),
        "CAM_FLOOR_02":   timedelta(hours=20, minutes=10),
        "CAM_BILLING_01": timedelta(hours=20, minutes=10),
    }
    base = datetime(2026, 4, 10, tzinfo=timezone.utc) + offsets.get(cam_id, timedelta(hours=20))
    offset = timedelta(seconds=frame_idx / fps)
    return (base + offset).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int((frame_idx/fps % 1)*1000):03d}Z"


def iou(a, b):
    """Intersection over Union of two (x, y, w, h) rects."""
    ax1, ay1 = a[0], a[1]
    ax2, ay2 = a[0]+a[2], a[1]+a[3]
    bx1, by1 = b[0], b[1]
    bx2, by2 = b[0]+b[2], b[1]+b[3]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2-ix1) * max(0, iy2-iy1)
    if inter == 0:
        return 0.0
    union = (ax2-ax1)*(ay2-ay1) + (bx2-bx1)*(by2-by1) - inter
    return inter / union if union > 0 else 0.0


def is_staff(frame, rect):
    """Classify staff by checking if torso region is predominantly dark (black uniform)."""
    x, y, w, h = [int(v) for v in rect]
    ty1 = y + h // 3
    ty2 = y + 2 * h // 3
    torso = frame[max(0, ty1):ty2, max(0, x):x+w]
    if torso.size == 0:
        return False, 0.0
    gray = cv2.cvtColor(torso, cv2.COLOR_BGR2GRAY)
    dark_ratio = float((gray < 60).sum()) / gray.size
    return dark_ratio > STAFF_BLACK_THRESH, dark_ratio


def classify_zone(cx_norm, cy_norm, zones, cam_id):
    """Find which zone a normalised centroid falls in."""
    for zone in zones:
        if zone.get("camera_id") != cam_id:
            continue
        poly = zone.get("polygon", [])
        if not poly:
            continue
        # Ray-casting
        n = len(poly)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > cy_norm) != (yj > cy_norm)) and \
               (cx_norm < (xj - xi) * (cy_norm - yi) / (yj - yi + 1e-9) + xi):
                inside = not inside
            j = i
        if inside:
            return zone["zone_id"], zone["zone_name"]
    return None, None


# ── Simple IoU tracker ─────────────────────────────────────────────────────

class Track:
    _next_id = 1

    def __init__(self, rect, frame_idx, conf):
        self.id = Track._next_id
        Track._next_id += 1
        self.rect = rect
        self.last_frame = frame_idx
        self.first_frame = frame_idx
        self.conf = conf
        self.active = True


class SimpleTracker:
    def __init__(self, max_miss_frames=15):
        self.tracks: list[Track] = []
        self.max_miss = max_miss_frames

    def update(self, detections, frame_idx, confs):
        matched = set()
        for i, det in enumerate(detections):
            best_iou, best_t = 0, None
            for t in self.tracks:
                if not t.active:
                    continue
                score = iou(t.rect, det)
                if score > best_iou:
                    best_iou, best_t = score, t
            if best_t and best_iou >= IOU_MATCH_THRESH:
                best_t.rect = det
                best_t.last_frame = frame_idx
                best_t.conf = confs[i]
                matched.add(best_t.id)
            else:
                self.tracks.append(Track(det, frame_idx, confs[i]))

        # Deactivate stale tracks
        new_exits = []
        for t in self.tracks:
            if t.active and frame_idx - t.last_frame > self.max_miss:
                t.active = False
                new_exits.append(t.id)

        return [t for t in self.tracks if t.active], new_exits

    def active_tracks(self):
        return [t for t in self.tracks if t.active]


# ── Event emitter ──────────────────────────────────────────────────────────

class Emitter:
    def __init__(self, store_id, cam_id, out_path):
        self.store_id = store_id
        self.cam_id = cam_id
        self.fh = open(out_path, "w", buffering=1)
        self.seq: dict[int, int] = {}

    def _s(self, tid):
        self.seq[tid] = self.seq.get(tid, 0) + 1
        return self.seq[tid]

    def _w(self, ev):
        self.fh.write(json.dumps(ev) + "\n")

    def entry(self, tid, ts, staff, conf):
        self.seq[tid] = 0
        self._w({"event_id": str(uuid.uuid4()), "event_type": "entry",
                 "id_token": f"ID_{tid:05d}", "store_code": self.store_id,
                 "store_id": self.store_id, "camera_id": self.cam_id,
                 "event_timestamp": ts, "event_time": ts,
                 "is_staff": staff, "confidence": round(conf, 4),
                 "session_seq": self._s(tid)})

    def exit(self, tid, ts, staff, conf):
        self._w({"event_id": str(uuid.uuid4()), "event_type": "exit",
                 "id_token": f"ID_{tid:05d}", "store_code": self.store_id,
                 "store_id": self.store_id, "camera_id": self.cam_id,
                 "event_timestamp": ts, "event_time": ts,
                 "is_staff": staff, "confidence": round(conf, 4),
                 "session_seq": self._s(tid)})

    def reentry(self, tid, ts, conf):
        self.seq[tid] = 0
        self._w({"event_id": str(uuid.uuid4()), "event_type": "reentry",
                 "id_token": f"ID_{tid:05d}", "store_code": self.store_id,
                 "store_id": self.store_id, "camera_id": self.cam_id,
                 "event_timestamp": ts, "event_time": ts,
                 "is_staff": False, "confidence": round(conf, 4),
                 "session_seq": 1})

    def zone_enter(self, tid, ts, zid, zname, staff, conf, cx, cy):
        self._w({"event_id": str(uuid.uuid4()), "event_type": "zone_entered",
                 "track_id": tid, "store_id": self.store_id, "camera_id": self.cam_id,
                 "zone_id": zid, "zone_name": zname, "event_time": ts,
                 "is_staff": staff, "confidence": round(conf, 4),
                 "zone_hotspot_x": round(cx, 1), "zone_hotspot_y": round(cy, 1),
                 "session_seq": self._s(tid)})

    def zone_exit(self, tid, ts, zid, dwell_ms, staff, conf):
        self._w({"event_id": str(uuid.uuid4()), "event_type": "zone_exited",
                 "track_id": tid, "store_id": self.store_id, "camera_id": self.cam_id,
                 "zone_id": zid, "event_time": ts, "dwell_ms": dwell_ms,
                 "is_staff": staff, "confidence": round(conf, 4),
                 "session_seq": self._s(tid)})

    def zone_dwell(self, tid, ts, zid, zname, dwell_ms, staff, conf):
        self._w({"event_id": str(uuid.uuid4()), "event_type": "ZONE_DWELL",
                 "track_id": tid, "store_id": self.store_id, "camera_id": self.cam_id,
                 "zone_id": zid, "zone_name": zname, "event_time": ts,
                 "dwell_ms": dwell_ms, "is_staff": staff, "confidence": round(conf, 4),
                 "session_seq": self._s(tid)})

    def queue_join(self, tid, ts, zid, zname, queue_depth, conf):
        self._w({"event_id": str(uuid.uuid4()), "queue_event_id": str(uuid.uuid4()),
                 "event_type": "BILLING_QUEUE_JOIN",
                 "track_id": tid, "store_id": self.store_id, "camera_id": self.cam_id,
                 "zone_id": zid, "zone_name": zname, "event_time": ts,
                 "queue_join_ts": ts, "queue_exit_ts": ts,
                 "queue_position_at_join": queue_depth,
                 "wait_seconds": 0, "abandoned": False,
                 "is_staff": False, "confidence": round(conf, 4)})

    def close(self):
        self.fh.close()


# ── Main processing function ────────────────────────────────────────────────

def process_clip(clip_file, cam_id, cam_type, layout, out_dir):
    path = os.path.join(DATA_DIR, clip_file)
    out_path = os.path.join(out_dir, f"{STORE_ID}_{cam_id}.jsonl")

    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"\n[{cam_id}] {clip_file} — {fw}x{fh} @ {fps:.0f}fps, {total_frames} frames")

    emitter   = Emitter(STORE_ID, cam_id, out_path)
    tracker   = SimpleTracker(max_miss_frames=int(fps * FRAME_SKIP / fps * 3))
    zones     = layout.get("zones", [])

    # State tracking
    entry_y_thresh = fh * 0.65   # entry line: bottom 35% of frame = inside store
    known_entries:  set[int] = set()   # track IDs that have fired ENTRY
    exit_times:     dict[int, float] = {}  # tid → frame_time_s of exit
    zone_states:    dict[int, tuple] = {}  # tid → (zone_id, enter_frame)
    dwell_emitted:  dict[int, dict]  = defaultdict(dict)  # tid → {zid: last_emit_frame}
    billing_in:     set[int] = set()  # tids currently in billing zone
    staff_cache:    dict[int, bool] = {}  # tid → is_staff

    frame_idx = 0
    n_events = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % FRAME_SKIP != 0:
            frame_idx += 1
            continue

        time_s = frame_idx / fps
        ts = ts_from_frame(frame_idx, fps, cam_id)

        # ── Detect ──
        rects, weights = HOG.detectMultiScale(
            frame, winStride=(8, 8), padding=(4, 4), scale=1.05
        )
        dets, confs = [], []
        for r, w in zip(rects, weights):
            conf = float(w) if not hasattr(w, '__len__') else float(w[0])
            if conf >= CONF_FLOOR:
                dets.append(tuple(r))
                confs.append(conf)

        active_tracks, new_exits = tracker.update(dets, frame_idx, confs)

        # ── Process exits ──
        for tid in new_exits:
            exit_times[tid] = time_s
            t_obj = next((t for t in tracker.tracks if t.id == tid), None)
            staff = staff_cache.get(tid, False)

            # Close open zone
            if tid in zone_states:
                zid, enter_f = zone_states.pop(tid)
                dwell_ms = int((frame_idx - enter_f) / fps * 1000)
                emitter.zone_exit(tid, ts, zid, dwell_ms, staff, 0.5)
                n_events += 1

            if cam_type == "entry" and tid in known_entries:
                emitter.exit(tid, ts, staff, 0.6)
                n_events += 1

            if tid in billing_in:
                billing_in.discard(tid)

        # ── Process active tracks ──
        for t in active_tracks:
            tid = t.id
            rx, ry, rw, rh = t.rect
            cx = rx + rw / 2
            cy = ry + rh / 2
            cx_n = cx / fw
            cy_n = cy / fh

            # Staff classification (cached after first detection)
            if tid not in staff_cache:
                staff_flag, _ = is_staff(frame, t.rect)
                staff_cache[tid] = staff_flag
            staff = staff_cache[tid]

            # ── Entry camera logic ──
            if cam_type == "entry":
                if tid not in known_entries:
                    if cy > entry_y_thresh:
                        # Check if re-entry
                        is_reentry = False
                        if tid in exit_times and (time_s - exit_times[tid]) > REENTRY_TIMEOUT_S:
                            emitter.reentry(tid, ts, t.conf)
                            is_reentry = True
                            n_events += 1
                        if not is_reentry:
                            emitter.entry(tid, ts, staff, t.conf)
                            n_events += 1
                        known_entries.add(tid)

            # ── Floor / billing zone logic ──
            if cam_type in ("floor", "billing"):
                zone_id, zone_name = classify_zone(cx_n, cy_n, zones, cam_id)
                prev_zone_id = zone_states.get(tid, (None, 0))[0]

                if zone_id != prev_zone_id:
                    # Exit old zone
                    if prev_zone_id and tid in zone_states:
                        enter_f = zone_states[tid][1]
                        dwell_ms = int((frame_idx - enter_f) / fps * 1000)
                        emitter.zone_exit(tid, ts, prev_zone_id, dwell_ms, staff, t.conf)
                        n_events += 1

                    # Enter new zone
                    if zone_id:
                        emitter.zone_enter(tid, ts, zone_id, zone_name or "", staff, t.conf, cx, cy)
                        n_events += 1
                        zone_states[tid] = (zone_id, frame_idx)
                        dwell_emitted[tid][zone_id] = frame_idx

                        # Billing queue
                        if cam_type == "billing" and "BILLING" in zone_id:
                            q_depth = len(billing_in)
                            if q_depth > 0:
                                emitter.queue_join(tid, ts, zone_id, zone_name or "Billing", q_depth, t.conf)
                                n_events += 1
                            billing_in.add(tid)

                # ZONE_DWELL heartbeat
                if zone_id and tid in zone_states:
                    last_emit = dwell_emitted[tid].get(zone_id, zone_states[tid][1])
                    if (frame_idx - last_emit) / fps >= DWELL_EMIT_S:
                        dwell_ms = int((frame_idx - zone_states[tid][1]) / fps * 1000)
                        emitter.zone_dwell(tid, ts, zone_id, zone_name or "", dwell_ms, staff, t.conf)
                        n_events += 1
                        dwell_emitted[tid][zone_id] = frame_idx

        if frame_idx % 300 == 0:
            pct = frame_idx / total_frames * 100
            print(f"  [{cam_id}] {pct:.0f}% — {frame_idx}/{total_frames} frames, {n_events} events so far", end="\r")

        frame_idx += 1

    # Flush remaining open zones
    for tid, (zid, enter_f) in zone_states.items():
        ts_end = ts_from_frame(frame_idx, fps, cam_id)
        dwell_ms = int((frame_idx - enter_f) / fps * 1000)
        emitter.zone_exit(tid, ts_end, zid, dwell_ms, staff_cache.get(tid, False), 0.5)
        n_events += 1

    cap.release()
    emitter.close()
    print(f"\n  [{cam_id}] Done — {n_events} events → {out_path}")
    return out_path, n_events


def ingest_events(events_file, api_url):
    with open(events_file) as f:
        events = [json.loads(l) for l in f if l.strip()]
    if not events:
        print(f"  [ingest] {events_file}: no events to ingest")
        return

    total_accepted = total_dup = total_rejected = 0
    for i in range(0, len(events), 500):
        batch = events[i:i+500]
        try:
            r = requests.post(f"{api_url}/events/ingest", json={"events": batch}, timeout=30)
            d = r.json()
            total_accepted += d.get("accepted", 0)
            total_dup      += d.get("duplicate", 0)
            total_rejected += d.get("rejected", 0)
        except Exception as e:
            print(f"  [ingest] ERROR: {e}")
    print(f"  [ingest] {os.path.basename(events_file)}: "
          f"accepted={total_accepted} dup={total_dup} rejected={total_rejected}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--output", default="./events")
    parser.add_argument("--no-ingest", action="store_true")
    parser.add_argument("--cam", default=None, help="Process only this camera ID")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)
    layout = load_layout()

    print(f"Store Intelligence — Store 1 Detection Pipeline")
    print(f"Store ID : {STORE_ID}")
    print(f"Date     : {CLIP_DATE}")
    print(f"API      : {args.api}")
    print(f"Output   : {args.output}")
    print("=" * 60)

    all_event_files = []
    total_events = 0

    for clip_file, cam_id, cam_type in CLIPS:
        if args.cam and cam_id != args.cam:
            continue
        path = os.path.join(DATA_DIR, clip_file)
        if not os.path.exists(path):
            print(f"[SKIP] {clip_file} not found at {path}")
            continue
        t0 = time.time()
        out_path, n = process_clip(clip_file, cam_id, cam_type, layout, args.output)
        print(f"  Elapsed: {time.time()-t0:.1f}s")
        all_event_files.append(out_path)
        total_events += n

    print(f"\n{'='*60}")
    print(f"Total events generated: {total_events}")

    if not args.no_ingest and all_event_files:
        # Also ingest sample events and POS data
        sample_path = os.path.join(DATA_DIR, "..", "sample_events.jsonl")
        if os.path.exists(sample_path):
            print(f"\n[ingest] Ingesting sample_events.jsonl...")
            ingest_events(sample_path, args.api)

        pos_path = os.path.join(DATA_DIR, "..", "POS_-_sample_transactions.csv")
        if os.path.exists(pos_path):
            import csv
            rows = list(csv.DictReader(open(pos_path)))
            txns = []
            for r in rows:
                try:
                    txns.append({
                        "order_id": str(r.get("order_id", str(uuid.uuid4()))),
                        "store_id": r.get("store_id", STORE_ID),
                        "order_date": r.get("order_date", CLIP_DATE),
                        "order_time": r.get("order_time", "20:00:00"),
                        "product_id": r.get("product_id", ""),
                        "brand_name": r.get("brand_name", ""),
                        "total_amount": float(r.get("total_amount", r.get("basket_value_inr", 0))),
                    })
                except Exception:
                    pass
            if txns:
                try:
                    r2 = requests.post(f"{args.api}/events/pos/ingest", json=txns, timeout=30)
                    print(f"[ingest] POS: {r2.json()}")
                except Exception as e:
                    print(f"[ingest] POS error: {e}")

        print(f"\n[ingest] Ingesting {len(all_event_files)} event files...")
        for ef in all_event_files:
            ingest_events(ef, args.api)

    print("\nDone! Check the API:")
    print(f"  {args.api}/stores/{STORE_ID}/metrics?date={CLIP_DATE}")
    print(f"  {args.api}/stores/{STORE_ID}/funnel?date={CLIP_DATE}")
    print(f"  {args.api}/stores/{STORE_ID}/heatmap?date={CLIP_DATE}")
    print(f"  {args.api}/stores/{STORE_ID}/anomalies?date={CLIP_DATE}")


if __name__ == "__main__":
    main()
