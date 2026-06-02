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

**Key design decision:** 
* **Session Deduplication & Robust Funnels**: While the `visitor_id` is used as the grouping key, tracking splits can happen when visitors move between cameras (e.g. entry camera vs floor cameras). To make the funnel robust, Stage 1 (Entered Store) counts any visitor who registers either an `ENTRY` event or a zone event (`ZONE_ENTER`, `ZONE_DWELL`). Subsequent stages track visits to product zones and billing counters, ensuring accurate step-by-step conversion funnel tracking regardless of track fragmentation.
* **Cross-Camera Safe Conversion**: Because distinct cameras have independent tracking instances, a single customer may have different visitor IDs assigned at the entrance camera versus the billing camera. To solve this, the conversion metric is cross-camera safe: it tracks the total count of unique non-staff visitors in the `BILLING` zone within the window, capped at the total unique entries to prevent mathematically impossible conversion rates (>100%).
* **POS Correlation**: The actual `pos_transactions.csv` has more fields than the challenge schema (invoice-level, not session-level). If matching timestamp data is present, we correlate a visitor's `BILLING` zone timestamp with POS transactions within a 5-minute window.

### Event Schema

I chose to emit events for both high-confidence and low-confidence detections, keeping the `confidence` field populated. The API and tests filter on this but the pipeline never silently drops a detection — low-confidence events have the same schema, just a lower `confidence` value. This makes calibration analysis possible post-submission.

---

## Design & Trade-off Decisions

**1. Staff detection approach**

Three potential approaches were evaluated to classify retail staff from camera footage without a labeled training set: uniform color classification (HSV), aspect ratio heuristics (staff standing still for longer), and a Vision-Language Model (VLM) prompt.

I chose HSV color classification because:
- No labeled data is needed.
- Purplle's purple uniform is highly distinctive in HSV space (hue 130-160°).
- It runs efficiently on CPU (~1ms per detection).

A VLM-based approach was rejected because it would add 2-3 seconds of latency per frame (e.g., for cloud VLM API calls) and the source footage is typically blurred, making detailed visual prompts impractical.

**2. Re-ID strategy**

We evaluated using a dedicated re-ID model like OSNet. However, considering the compute requirements and CPU-only deployment constraints, a color histogram + trajectory approach was chosen. This method is highly effective for the challenge's edge cases. The re-ID window is set to 120 seconds — a visitor leaving and returning within 2 minutes preserves their `visitor_id`.

I chose histogram similarity over cosine similarity on full-body embeddings because cosine similarity would require downloading a pre-trained model on the first run, whereas histogram similarity requires no external weights and runs locally out-of-the-box.

**3. SQLite over PostgreSQL**

While PostgreSQL is standard for production workloads due to write throughput at scale, SQLite with WAL (Write-Ahead Logging) mode was selected because:
- `docker compose up` works out-of-the-box with zero external service dependencies.
- WAL mode handles concurrent reads efficiently at this scale.
- It simplifies deployment and minimizes potential points of failure during automated evaluation.

For a real multi-store production environment, transitioning to PostgreSQL would be recommended. The first scaling bottleneck would be the `/funnel` endpoint's visitor aggregation query, which has been documented.

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
