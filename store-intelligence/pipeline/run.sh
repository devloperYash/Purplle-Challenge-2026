#!/bin/bash
set -e

STORE_ID="STORE_BLR_002"
LAYOUT="data/store_layout.json"
FOOTAGE_DIR="${1:-../CCTV Footage}"
OUTPUT_DIR="events"
API_URL="${2:-}"

mkdir -p "$OUTPUT_DIR"

CLIP_START_DATE="2026-04-10"

# Map each camera file to a camera ID and clip start time
# CAM 1 — Entry/Exit threshold
# CAM 2 — Premium brand back wall
# CAM 3 — Main floor (FOH + consultation)
# CAM 4 — Mass brand front wall
# CAM 5 — Billing area

declare -A CAM_STARTS=(
    ["CAM 1"]="2026-04-10T10:00:00Z"
    ["CAM 2"]="2026-04-10T10:00:00Z"
    ["CAM 3"]="2026-04-10T10:00:00Z"
    ["CAM 4"]="2026-04-10T10:00:00Z"
    ["CAM 5"]="2026-04-10T10:00:00Z"
)

declare -A CAM_IDS=(
    ["CAM 1"]="CAM_1"
    ["CAM 2"]="CAM_2"
    ["CAM 3"]="CAM_3"
    ["CAM 4"]="CAM_4"
    ["CAM 5"]="CAM_5"
)

for CAM_NAME in "CAM 1" "CAM 2" "CAM 3" "CAM 4" "CAM 5"; do
    VIDEO_FILE="$FOOTAGE_DIR/$CAM_NAME.mp4"
    CAM_ID="${CAM_IDS[$CAM_NAME]}"
    CLIP_START="${CAM_STARTS[$CAM_NAME]}"
    OUTPUT_FILE="$OUTPUT_DIR/${CAM_ID}_events.jsonl"

    if [ ! -f "$VIDEO_FILE" ]; then
        echo "Warning: $VIDEO_FILE not found, skipping"
        continue
    fi

    echo "Processing $CAM_NAME → $OUTPUT_FILE"

    API_FLAG=""
    if [ -n "$API_URL" ]; then
        API_FLAG="--push-to-api $API_URL/events/ingest"
    fi

    python pipeline/detect.py \
        --video "$VIDEO_FILE" \
        --camera-id "$CAM_ID" \
        --store-id "$STORE_ID" \
        --layout "$LAYOUT" \
        --output "$OUTPUT_FILE" \
        --clip-start "$CLIP_START" \
        --skip-frames 3 \
        --conf 0.35 \
        $API_FLAG

    echo "$CAM_NAME done → $(wc -l < $OUTPUT_FILE) events"
done

# Merge all event files into one, sorted by timestamp
echo "Merging all events..."
MERGED="$OUTPUT_DIR/all_events.jsonl"
cat $OUTPUT_DIR/CAM_*_events.jsonl | python -c "
import sys, json
lines = [l.strip() for l in sys.stdin if l.strip()]
events = []
for l in lines:
    try:
        events.append(json.loads(l))
    except: pass
events.sort(key=lambda e: e.get('timestamp', ''))
for e in events:
    print(json.dumps(e))
" > "$MERGED"

echo "Merged: $(wc -l < $MERGED) total events → $MERGED"

# Ingest into the API if running
if [ -n "$API_URL" ]; then
    echo "Ingesting merged events into API..."
    python -c "
import json, urllib.request, sys
with open('$MERGED') as f:
    events = [json.loads(l) for l in f if l.strip()]

batch_size = 500
for i in range(0, len(events), batch_size):
    batch = events[i:i+batch_size]
    payload = json.dumps({'events': batch}).encode()
    req = urllib.request.Request(
        '$API_URL/events/ingest',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    resp = urllib.request.urlopen(req, timeout=30)
    print(f'Batch {i//batch_size + 1}: {resp.status}')
print('Done.')
"
fi
