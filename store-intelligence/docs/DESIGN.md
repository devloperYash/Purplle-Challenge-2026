# DESIGN.md — Store Intelligence System

## What This System Does

The Store Intelligence system converts raw CCTV footage from a physical retail store into live business metrics — primarily the store's offline conversion rate. It's the equivalent of having web analytics for a physical shop.

The pipeline runs in four stages. You start with video clips, end up with a live web dashboard showing who visited which zone, how long they stayed, how many bought something, and whether there's a queue building up right now.

---

## System Architecture

```
[CCTV Clips] → [Detection Pipeline] → [JSONL Events] → [FastAPI] → [SQLite] → [Dashboard]
     CAM 1-5         detect.py             events/           app/        data/        /dashboard
```

The detection pipeline and the API are intentionally decoupled. The pipeline writes events to `.jsonl` files (or POSTs directly to the API in real-time mode). This separation means:
- You can reprocess video clips without touching the API
- You can replay events from the file for testing
- The API doesn't care how events were generated

### Detection Layer

**File: `pipeline/detect.py`**

Runs YOLOv8n on each frame (every 3rd frame at 15fps = 5fps effective). For each detected person:
1. Extracts a torso-region HSV color histogram as an appearance vector
2. Classifies staff by dominant hue in the purple/violet HSV range (130-160°) — Purplle staff wear branded purple uniforms
3. Matches to existing tracks via IoU (≥0.3 threshold)
4. For new tracks, checks appearance similarity against recently-exited visitors for re-ID

**File: `pipeline/tracker.py`**

The tracker maintains a list of active tracks. Each track has:
- `visitor_id` — persists across re-entries (same person, new session)
- `appearance` — exponentially weighted color histogram (0.7 old + 0.3 new per frame)
- `missing_frames` — tracks are dropped after 30 consecutive missed frames

**File: `pipeline/zone_mapper.py`**

Maps camera pixel coordinates to store floor zones. The foot position (bottom-center of bounding box) is projected to floor-plan millimeter coordinates using a proportional mapping. Real calibration would use a homography matrix from 4 reference points per camera — I've structured the code to accept this via `set_homography()`.

Zone priority order matters: ENTRY and BILLING are checked before general floor zones to handle people standing in threshold areas.

### Intelligence API

Built with FastAPI, SQLite (WAL mode for concurrent reads). All endpoints compute from the database in real time — no caching between days.

**Key design decision:** Session deduplication. The `visitor_id` from re-ID is the session key. A visitor who enters, exits, and re-enters has one `visitor_id` and appears once in the funnel, regardless of how many `ENTRY` events their track generated.

**POS Correlation:** The actual `pos_transactions.csv` has more fields than the challenge schema (invoice-level, not session-level). I map invoice numbers as transaction IDs and correlate by matching a visitor's `BILLING` zone timestamp with any POS transaction within a 5-minute window. One transaction can only "claim" one visitor (first-match basis).

### Event Schema

I chose to emit events for both high-confidence and low-confidence detections, keeping the `confidence` field populated. The API and tests filter on this but the pipeline never silently drops a detection — low-confidence events have the same schema, just a lower `confidence` value. This makes calibration analysis possible post-submission.

---

## AI-Assisted Decisions

**1. Staff detection approach**

I asked Claude to suggest how to classify retail staff from camera footage without a labeled training set. It suggested three approaches: uniform color classification (HSV), aspect ratio heuristics (staff often stand still for longer), and a VLM prompt.

I chose HSV color classification because:
- No labeled data needed
- Purplle's purple uniform is distinctive in HSV space (hue 130-160°)
- It runs on CPU in ~1ms per detection

The VLM approach Claude suggested was genuinely interesting but impractical — it would add 2-3 seconds per frame for GPT-4V calls, and the footage is blurred anyway. I documented this but decided against it.

**2. Re-ID strategy**

Claude initially suggested using OSNet (a dedicated re-ID model). I looked at the compute requirements and the deployment constraints (CPU-only) and decided that a color histogram + trajectory approach would be "good enough" for the challenge's known edge cases. The re-ID window is 120 seconds — someone who leaves and returns within 2 minutes gets their `visitor_id` preserved.

Where I pushed back on Claude's suggestion: it initially recommended using cosine similarity on full-body embeddings, which would require a pre-trained model download on first run. I switched to histogram similarity, which requires no external weights.

**3. SQLite over PostgreSQL**

Claude recommended PostgreSQL for production workloads. I agreed with the reasoning (write throughput at scale) but chose SQLite + WAL mode because:
- `docker compose up` works with zero external services
- WAL mode handles concurrent reads fine for this scale
- I didn't want to add a Postgres container that could fail the acceptance gate

If this were a real multi-store production system I'd switch to Postgres — and I've documented exactly where the bottleneck would appear first (the `/funnel` endpoint's visitor aggregation query).

---

## Edge Case Handling

| Edge Case | How I Handle It |
|-----------|----------------|
| Group entry | Each detection within a frame is a separate track. 3 people entering = 3 ENTRY events. |
| Staff movement | HSV-based uniform classifier sets `is_staff=true`. All API metrics exclude `is_staff=1`. |
| Re-entry | Appearance similarity check against 2-minute exit window. Same `visitor_id` = REENTRY event, not a second ENTRY. |
| Partial occlusion | Detections below confidence threshold still emit with actual confidence value. Not suppressed. |
| Billing queue | Queue depth counted as concurrent `BILLING` zone occupants at event time. ABANDON emitted if visitor leaves billing zone before a POS transaction follows. |
| Empty store | All metrics return 0.0 (not null, not error). Data confidence = "LOW". |
| Camera overlap | The `camera_id` field on each event lets the API detect potential cross-camera duplicates by visitor_id. Re-ID prevents double counting if the same person is tracked by two cameras simultaneously. |

---

## What I Would Change with More Time

1. **Proper homography calibration** per camera using floor markers or known fixed points
2. **ByteTrack** proper implementation instead of the simpler IoU tracker — ByteTrack handles occlusion significantly better
3. **Confidence calibration plot** — post-process all events to check if the confidence scores are actually well-calibrated (Platt scaling if not)
4. **WebSocket push from detection pipeline** instead of polling — the dashboard currently polls every 15 seconds
