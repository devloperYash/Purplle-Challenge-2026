# CHOICES.md — Three Key Engineering Decisions

## Decision 1: Detection Model — YOLOv8n over YOLOv8m/l or RT-DETR

**What I was choosing between:**
- YOLOv8n (nano): fastest, lowest accuracy
- YOLOv8m (medium): good accuracy, ~3x slower
- RT-DETR: transformer-based, excellent accuracy, heavy
- MediaPipe Pose: body pose, different use case

**What AI suggested:**

I asked Claude to compare these for a CPU-only retail deployment. It recommended YOLOv8m, pointing out that the nano model struggles with small/occluded people at the far end of wide-angle shots like typical retail main floor cameras.

**What I chose and why:**

YOLOv8n with skip-frame processing (every 3rd frame). Here's my reasoning:

The footage is 1080p at 15fps. Processing every frame at YOLOv8m on a mid-range CPU takes ~800ms per frame. That's unusable in real time. YOLOv8n takes ~120ms. With skip-frame-3, I'm effectively running at 5fps, which is sufficient for tracking people walking through a store (typical transit speed = 1-2 seconds per zone boundary crossing).

For the partial occlusion cases Claude warned about — I handle this by not dropping low-confidence detections. They get emitted with their actual confidence score. The reviewer can see the confidence distribution and evaluate it. Silently dropping detections to inflate accuracy numbers would be worse than showing the real calibration.

**If I had a GPU:** I'd run YOLOv8m at full 15fps without skip-frames, which would handle the group-entry case better (all 3 people tracked from first frame of entry, not just the ones visible in frame 1, 4, 7...).

---

## Decision 2: Event Schema Design

**What I was choosing between:**

Option A: Flat schema — every field at the top level, duplicating zone info in each event.

Option B: Nested schema with `metadata` block — matches the challenge spec, keeps the top-level clean.

Option C: Multiple event types with specialized schemas per type (e.g., `BillingEvent` has `queue_depth` required).

**What AI suggested:**

Claude suggested Option C — separate Pydantic models per event type. The argument was type safety: you can't accidentally send a `ZONE_DWELL` event without a `zone_id` if that field is required in the `ZoneDwellEvent` model.

**What I chose and why:**

Option B (matching the spec), with field-level validators to enforce the constraint.

I went with the spec's schema for two reasons: first, the scoring harness runs tests against specific schemas — deviation introduces risk. Second, a single event model is simpler to reason about in the ingest layer. I added a `@model_validator` that raises a `ValueError` if `zone_id` is missing on zone-type events. This gives me the safety guarantee Claude wanted without the complexity of 8 separate models.

Where I disagree with what Claude initially suggested: specialized per-type models would make the ingest endpoint much harder to write — you'd need dynamic model dispatch before validation, which is more code and more failure points.

**The `confidence` field design choice:**

The spec says "do not suppress low-conf events." I kept this in the schema by making confidence a required float with no minimum threshold. If a detection is confidence=0.38, it goes in with confidence=0.38. This is important for calibration analysis and because the challenge specifically evaluates confidence calibration as a scoring criterion.

---

## Decision 3: API Architecture — SQLite with Real-Time Queries vs. Pre-aggregated Tables

**What I was choosing between:**

Option A: Compute all metrics fresh from the events table on every API request (what I built).

Option B: Background job pre-aggregates into summary tables every 60 seconds, API reads from summaries.

Option C: Redis for real-time counters, SQLite for persistence.

**What AI suggested:**

Claude initially recommended Option B — pre-aggregation into summary tables — because at 40 stores sending events continuously, live query aggregation would get expensive fast. It also suggested adding a Redis layer for current queue depth since that's the most time-sensitive metric.

**What I chose and why:**

Option A, with one exception: current queue depth reads from a 5-minute event window rather than a full scan.

For this challenge's scale (one store, batch-processed clips), real-time query computation is completely fine. The `events` table has indexes on `(store_id, timestamp)` and `(visitor_id)`, so the aggregation queries are fast even with thousands of events.

I chose this over pre-aggregation because:
1. Pre-aggregation adds a background thread/process to manage — that's more failure surface in Docker
2. If a background job fails, the API serves stale data silently — that's worse than slightly slower fresh data
3. The acceptance gate tests are functional, not performance tests

**Where I agree with Claude:**

At 40 live stores in production, the `/funnel` endpoint's `GROUP BY visitor_id` aggregation would be the first thing to break under load. The fix is exactly what Claude described: move from per-request aggregation to incremental session state, updated on each event ingest. I documented this in DESIGN.md as the first scaling bottleneck.

The Redis queue depth suggestion is also right for production — the current queue depth needs sub-second freshness, and SQLite isn't ideal for that. But for the challenge scope, the 5-minute window query is a reasonable trade-off.
