# Store Intelligence System

End-to-end pipeline from CCTV footage to live store analytics. Built for the Brigade Road Bangalore Purplle store.

## Setup — 5 Commands

```bash
git clone https://github.com/devloperYash/Purplle-Challenge-2026.git
cd Purplle-Challenge-2026/store-intelligence
cp ../Brigade_Bangalore_10_April_26*.csv data/Brigade_Bangalore_transactions.csv
docker compose up --build
# API is live at http://localhost:8000
# Dashboard: http://localhost:8000/dashboard
```

That's it. No other setup required.

---

## Running the Detection Pipeline

The detection pipeline processes CCTV footage and emits structured events. It runs separately from the API.

### Prerequisites (on host machine, not Docker)

```bash
pip install -r requirements.txt
```

YOLOv8n weights download automatically on first run (~6MB).

### Process All Cameras

```bash
# Basic batch processing (all 5 cameras)
bash pipeline/run.sh "../CCTV Footage"

# Batch processing + stream events directly to the API in real time
bash pipeline/run.sh "../CCTV Footage" "http://localhost:8000"
```

Events are written to `events/CAM_N_events.jsonl` per camera, then merged into `events/all_events.jsonl`.

### Process a Single Camera

```bash
python pipeline/detect.py \
  --video "../CCTV Footage/CAM 1.mp4" \
  --camera-id CAM_1 \
  --store-id STORE_BLR_002 \
  --layout data/store_layout.json \
  --output events/cam1_events.jsonl \
  --clip-start 2026-04-10T10:00:00Z \
  --skip-frames 3 \
  --conf 0.35
```

### Ingest Events into the API

```bash
# After processing, push the merged events file to the API:
python -c "
import json, urllib.request
with open('events/all_events.jsonl') as f:
    events = [json.loads(l) for l in f if l.strip()]
for i in range(0, len(events), 500):
    batch = events[i:i+500]
    payload = json.dumps({'events': batch}).encode()
    req = urllib.request.Request('http://localhost:8000/events/ingest', data=payload,
          headers={'Content-Type': 'application/json'}, method='POST')
    print(urllib.request.urlopen(req).read().decode())
"
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/events/ingest` | Ingest up to 500 events. Idempotent by `event_id`. |
| GET | `/stores/{id}/metrics` | Unique visitors, conversion rate, dwell, queue depth |
| GET | `/stores/{id}/funnel` | Entry → Browse → Billing → Purchase with drop-off % |
| GET | `/stores/{id}/heatmap` | Zone activity normalized 0-100 |
| GET | `/stores/{id}/anomalies` | Active anomalies with severity and suggested actions |
| GET | `/health` | Service status + per-store feed lag |
| GET | `/dashboard` | Live web dashboard (WebSocket-powered) |

**Primary store ID:** `STORE_BLR_002`

Quick check:
```bash
curl http://localhost:8000/stores/STORE_BLR_002/metrics
curl http://localhost:8000/health
```

---

## Running Tests

```bash
cd store-intelligence
pip install pytest pytest-cov
pytest tests/ -v --cov=app --cov-report=term-missing
```

---

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py          YOLOv8 detection + event emission
│   ├── tracker.py         IoU tracking + HSV-based re-ID
│   ├── zone_mapper.py     Camera → floor zone coordinate mapping
│   ├── emit.py            Event schema + JSONL writer
│   └── run.sh             One-command pipeline for all cameras
├── app/
│   ├── main.py            FastAPI application + WebSocket
│   ├── models.py          Pydantic schemas for all endpoints
│   ├── database.py        SQLite setup (WAL mode)
│   ├── ingestion.py       Event ingest with idempotency
│   ├── metrics.py         Real-time KPI computation
│   ├── funnel.py          Session-based conversion funnel
│   ├── anomalies.py       Queue spike, dead zone, stale feed detection
│   └── health.py          Health check with feed lag monitoring
├── dashboard/
│   └── index.html         Live web UI (polls API + WebSocket)
├── data/
│   └── store_layout.json  Brigade Road zone definitions
├── tests/
│   ├── test_pipeline.py   Ingest, idempotency, re-entry, staff exclusion
│   ├── test_metrics.py    Metrics, funnel, POS correlation
│   └── test_anomalies.py  Queue spike, dead zone, stale feed
├── docs/
│   ├── DESIGN.md          Architecture + AI-assisted decisions
│   └── CHOICES.md         Three key engineering decisions
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Notes on the Detection Pipeline

- Runs YOLOv8n at skip-frame-3 (effective 5fps from 15fps footage) for CPU performance
- Staff classified by HSV purple-range detection (Purplle uniform color H: 120-160°)
- Re-ID window: 120 seconds — same person re-entering within 2 min gets preserved visitor_id
- All detections emitted with actual confidence score — nothing silently dropped
- Zone mapping uses foot position (bottom-center of bbox) projected to floor-plan coordinates

## Live Dashboard

Open `http://localhost:8000/dashboard` after starting the API. The dashboard:
- Shows real-time KPIs (visitors, conversion rate, dwell, queue depth)
- Updates via WebSocket when new events are ingested
- Displays the conversion funnel, zone heatmap, and active anomalies
- Polls every 15 seconds for fresh data even when no WebSocket events arrive
