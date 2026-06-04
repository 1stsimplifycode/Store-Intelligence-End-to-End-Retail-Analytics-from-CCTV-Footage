#!/bin/bash
# run.sh — Process all clips for a store folder -> events -> ingest to API
# Usage: bash pipeline/run.sh <store_dir> <store_id> [output_dir]
# Examples:
#   bash pipeline/run.sh ./data/Store_1 ST1008
#   bash pipeline/run.sh ./data/Store_2 ST1076

set -euo pipefail

STORE_DIR="${1:-./data/Store_1}"
STORE_ID="${2:-ST1008}"
OUTPUT_DIR="${3:-./events}"
API_URL="${API_URL:-http://localhost:8000}"
STAFF_COLOUR="${STAFF_COLOUR:-black}"
MODEL="${MODEL:-yolov8n.pt}"

mkdir -p "${OUTPUT_DIR}"

echo "======================================================"
echo " Store Intelligence Detection Pipeline"
echo " Store dir : ${STORE_DIR}"
echo " Store ID  : ${STORE_ID}"
echo " Output    : ${OUTPUT_DIR}"
echo " API       : ${API_URL}"
echo "======================================================"

# Check for store_layout.json
LAYOUT="${STORE_DIR}/store_layout.json"
if [ ! -f "${LAYOUT}" ]; then
    echo "[WARN] No store_layout.json found at ${LAYOUT}"
    echo "       Zone classification will use grid fallback."
    LAYOUT=""
fi

process_video() {
    local video="$1"
    local cam_type="$2"
    local cam_id="$3"
    local out="${OUTPUT_DIR}/${STORE_ID}_${cam_id}.jsonl"

    echo "[detect] ${video} -> ${out}"

    python3 -m pipeline.detect \
        --video "${video}" \
        --store-id "${STORE_ID}" \
        --camera-id "${cam_id}" \
        --camera-type "${cam_type}" \
        ${LAYOUT:+--layout "${LAYOUT}"} \
        --output "${out}" \
        --staff-colour "${STAFF_COLOUR}" \
        --model "${MODEL}"
}

# --- Auto-discover clips by filename pattern ---
# Entry cameras
CAM_IDX=1
for v in "${STORE_DIR}"/CAM*entry*.mp4 \
          "${STORE_DIR}"/CAM*Entry*.mp4 \
          "${STORE_DIR}"/entry*.mp4; do
    [ -f "$v" ] || continue
    process_video "$v" "entry" "CAM_ENTRY_0${CAM_IDX}"
    CAM_IDX=$((CAM_IDX+1))
done

# Floor / zone cameras
CAM_IDX=1
for v in "${STORE_DIR}"/CAM*zone*.mp4 \
          "${STORE_DIR}"/CAM*Zone*.mp4 \
          "${STORE_DIR}"/zone*.mp4 \
          "${STORE_DIR}"/floor*.mp4; do
    [ -f "$v" ] || continue
    process_video "$v" "floor" "CAM_FLOOR_0${CAM_IDX}"
    CAM_IDX=$((CAM_IDX+1))
done

# Billing cameras
CAM_IDX=1
for v in "${STORE_DIR}"/CAM*billing*.mp4 \
          "${STORE_DIR}"/CAM*Billing*.mp4 \
          "${STORE_DIR}"/billing*.mp4; do
    [ -f "$v" ] || continue
    process_video "$v" "billing" "CAM_BILLING_0${CAM_IDX}"
    CAM_IDX=$((CAM_IDX+1))
done

# --- Merge and ingest ---
MERGED="${OUTPUT_DIR}/${STORE_ID}_all_events.jsonl"
cat "${OUTPUT_DIR}/${STORE_ID}_"*.jsonl > "${MERGED}" 2>/dev/null || true

TOTAL=$(wc -l < "${MERGED}" 2>/dev/null || echo 0)
echo ""
echo "[ingest] ${TOTAL} events -> ${API_URL}/events/ingest"

python3 - << PYEOF
import json, requests, sys

merged = "${MERGED}"
api    = "${API_URL}"

with open(merged) as f:
    events = [json.loads(l) for l in f if l.strip()]

print(f"  Total events: {len(events)}")
accepted = dup = rejected = 0
for i in range(0, len(events), 500):
    batch = events[i:i+500]
    try:
        r = requests.post(f"{api}/events/ingest",
                          json={"events": batch}, timeout=30)
        d = r.json()
        accepted  += d.get("accepted", 0)
        dup       += d.get("duplicate", 0)
        rejected  += d.get("rejected", 0)
    except Exception as e:
        print(f"  [ERROR] {e}")

print(f"  accepted={accepted} duplicate={dup} rejected={rejected}")
PYEOF

echo ""
echo "[done] ${STORE_ID} pipeline complete."
echo "  ${API_URL}/stores/${STORE_ID}/metrics"
echo "  ${API_URL}/stores/${STORE_ID}/funnel"
echo "  ${API_URL}/stores/${STORE_ID}/anomalies"
