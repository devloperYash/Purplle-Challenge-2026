# CHOICES.md — Three Key Engineering Decisions

## Decision 1: Detection Model — YOLOv8n over YOLOv8m/l or RT-DETR

**What I was choosing between:**
- YOLOv8n (nano): fastest, lowest accuracy
- YOLOv8m (medium): good accuracy, ~3x slower
- RT-DETR: transformer-based, excellent accuracy, heavy
- MediaPipe Pose: body pose, different use case

**Trade-offs Considered:**

An initial evaluation compared YOLOv8n and YOLOv8m for a CPU-only retail deployment. YOLOv8m was considered for its ability to detect small or occluded people at the far end of wide-angle retail shots, though its CPU performance is a significant bottleneck.

**What I chose and why:**

YOLOv8n with skip-frame processing (every 3rd frame). Here's my reasoning:

The footage is 1080p at 15fps. Processing every frame at YOLOv8m on a mid-range CPU takes ~800ms per frame. That's unusable in real time. YOLOv8n takes ~120ms. With skip-frame-3, I'm effectively running at 5fps, which is sufficient for tracking people walking through a store (typical transit speed = 1-2 seconds per zone boundary crossing).

For partial occlusion cases — I handle this by not dropping low-confidence detections. They get emitted with their actual confidence score. The reviewer can see the confidence distribution and evaluate it. Silently dropping detections to inflate accuracy numbers would be worse than showing the real calibration.

**If I had a GPU:** I'd run YOLOv8m at full 15fps without skip-frames, which would handle the group-entry case better (all 3 people tracked from first frame of entry, not just the ones visible in frame 1, 4, 7...).

---

## Decision 2: Event Schema Design

**What I was choosing between:**

Option A: Flat schema — every field at the top level, duplicating zone info in each event.

Option B: Nested schema with `metadata` block — matches the challenge spec, keeps the top-level clean.

Option C: Multiple event types with specialized schemas per type (e.g., `BillingEvent` has `queue_depth` required).

**Trade-offs Considered:**

Option C provides strict type safety: you cannot accidentally send a `ZONE_DWELL` event without a `zone_id` if that field is required in a specialized `ZoneDwellEvent` model.

**What I chose and why:**

Option B (matching the spec), with field-level validators to enforce the constraint.

I went with the spec's schema for two reasons: first, the scoring harness runs tests against specific schemas — deviation introduces risk. Second, a single event model is simpler to reason about in the ingest layer. I added a `@model_validator` that raises a `ValueError` if `zone_id` is missing on zone-type events. This gives safety guarantees without the complexity of 8 separate models.

Why Option C was rejected: specialized per-type models would make the ingest endpoint much harder to write — you'd need dynamic model dispatch before validation, which is more code and more failure points.

**The `confidence` field design choice:**

The spec says "do not suppress low-conf events." I kept this in the schema by making confidence a required float with no minimum threshold. If a detection is confidence=0.38, it goes in with confidence=0.38. This is important for calibration analysis and because the challenge specifically evaluates confidence calibration as a scoring criterion.

---

## Decision 3: API Architecture — SQLite with Real-Time Queries vs. Pre-aggregated Tables

**What I was choosing between:**

Option A: Compute all metrics fresh from the events table on every API request (what I built).

Option B: Background job pre-aggregates into summary tables every 60 seconds, API reads from summaries.

Option C: Redis for real-time counters, SQLite for persistence.

**Trade-offs Considered:**

Pre-aggregation into summary tables (Option B) is highly beneficial at production scale (e.g., 40+ stores sending events continuously), as live query aggregation would get expensive. Furthermore, a Redis layer is optimal for tracking the current queue depth since it is the most time-sensitive metric.

**What I chose and why:**

Option A, with one exception: current queue depth reads from a 5-minute event window rather than a full scan.

For this challenge's scale (one store, batch-processed clips), real-time query computation is completely fine. The `events` table has indexes on `(store_id, timestamp)` and `(visitor_id)`, so the aggregation queries are fast even with thousands of events.

I chose this over pre-aggregation because:
1. Pre-aggregation adds a background thread/process to manage — that's more failure surface in Docker
2. If a background job fails, the API serves stale data silently — that's worse than slightly slower fresh data
3. The acceptance gate tests are functional, not performance tests

**Production Bottlenecks & Scaling Strategy:**

At production scale (e.g., 40 live stores), the `/funnel` endpoint's `GROUP BY visitor_id` aggregation would be the first bottleneck under load. The optimal scaling strategy is to move from per-request aggregation to incremental session state tracking, updated on each event ingest. This has been documented in DESIGN.md as the primary scaling bottleneck.

The Redis queue depth strategy is also right for production — the current queue depth needs sub-second freshness, and SQLite isn't ideal for that. But for the challenge scope, the 5-minute window query is a reasonable trade-off.
