"""
Main detection script — runs YOLOv8 on CCTV clips and emits structured events.

Usage:
    python detect.py --video path/to/CAM_1.mp4 --camera-id CAM_1 --store-id STORE_BLR_002 \\
                     --layout data/store_layout.json --output events/cam1_events.jsonl \\
                     --clip-start 2026-04-10T10:00:00Z

This script processes one video clip at a time. For batch processing all cameras,
use run.sh which calls this script in sequence and merges the output.
"""

import argparse
import logging
import sys
import os
import json
import time
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("detect")


def get_appearance_vector(frame, bbox: list) -> np.ndarray:
    """
    Extract a color histogram from the torso region of a detected person.
    This is the core of our re-ID — HSV histograms are lighting-robust.
    """
    import cv2
    x1, y1, x2, y2 = [int(v) for v in bbox]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(frame.shape[1] - 1, x2), min(frame.shape[0] - 1, y2)

    if x2 <= x1 or y2 <= y1:
        return np.zeros(96, dtype=np.float32)

    # Focus on the torso (middle third vertically) for clothing color
    h = y2 - y1
    torso_y1 = y1 + h // 3
    torso_y2 = y1 + 2 * h // 3
    crop = frame[torso_y1:torso_y2, x1:x2]

    if crop.size == 0:
        return np.zeros(96, dtype=np.float32)

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h_hist = cv2.calcHist([hsv], [0], None, [32], [0, 180]).flatten()
    s_hist = cv2.calcHist([hsv], [1], None, [32], [0, 256]).flatten()
    v_hist = cv2.calcHist([hsv], [2], None, [32], [0, 256]).flatten()

    vec = np.concatenate([h_hist, s_hist, v_hist]).astype(np.float32)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def classify_staff(frame, bbox: list) -> bool:
    """
    Classify whether a detection is store staff based on uniform color.
    Purplle staff wear purple/violet uniforms.
    We look for a dominant hue in the purple range (HSV H: 120-160).
    """
    import cv2
    x1, y1, x2, y2 = [int(v) for v in bbox]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(frame.shape[1] - 1, x2), min(frame.shape[0] - 1, y2)

    crop = frame[max(y1, 0):y2, x1:x2]
    if crop.size == 0:
        return False

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    # Purple/violet range in HSV
    lower_purple = np.array([120, 50, 50])
    upper_purple = np.array([160, 255, 255])
    mask = cv2.inRange(hsv, lower_purple, upper_purple)

    purple_ratio = mask.sum() / (mask.size * 255)
    return purple_ratio > 0.25


def determine_direction(centroids: list[tuple], entry_x_threshold: int, frame_w: int) -> str | None:
    """
    Determine movement direction across the entry threshold.
    Returns 'ENTRY' if moving inward, 'EXIT' if moving outward, None if unclear.

    We track whether the centroid crosses the threshold line left-to-right (entry)
    or right-to-left (exit) for left-side store entries.
    """
    if len(centroids) < 3:
        return None

    xs = [c[0] for c in centroids[-5:]]
    if len(xs) < 2:
        return None

    delta = xs[-1] - xs[0]
    if abs(delta) < 20:
        return None

    # Check if any centroid is near the threshold
    near_threshold = any(abs(c[0] - entry_x_threshold) < 60 for c in centroids[-5:])
    if not near_threshold:
        return None

    return "ENTRY" if delta > 0 else "EXIT"


def run_detection(
    video_path: str,
    camera_id: str,
    store_id: str,
    layout_path: str,
    output_path: str,
    clip_start: datetime,
    skip_frames: int = 3,
    confidence_threshold: float = 0.35,
    push_to_api: str = None,
):
    try:
        import cv2
        from ultralytics import YOLO
    except ImportError as e:
        logger.error(f"Missing dependency: {e}. Run: pip install ultralytics opencv-python")
        sys.exit(1)

    from tracker import MultiCameraTracker
    from zone_mapper import ZoneMapper
    from emit import StoreEvent, EventEmitter, EventMetadata, make_timestamp

    logger.info(f"Loading model...")
    model = YOLO("yolov8n.pt")

    zone_mapper = ZoneMapper(layout_path)
    tracker = MultiCameraTracker(fps=15.0)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error(f"Cannot open: {video_path}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    logger.info(f"Video: {Path(video_path).name} | {frame_w}x{frame_h} @ {fps:.1f}fps | {total_frames} frames")

    # Entry threshold is at ~12% from left for CAM_1 (entry camera)
    entry_x_threshold = int(frame_w * 0.12)

    # Track centroid history for direction detection
    centroid_history: dict[int, list] = defaultdict(list)
    # Track zone history for dwell events
    zone_dwell_frames: dict[int, dict] = defaultdict(dict)
    # Session sequence per visitor
    session_seq: dict[str, int] = defaultdict(int)
    # Active zones per track
    track_zone_history: dict[int, str | None] = {}
    # Billing zone visitors (for queue detection)
    billing_visitors: dict[str, float] = {}
    # Tracks that have emitted ENTRY (avoid double counting)
    emitted_entry: set[int] = set()
    # Tracks that have emitted EXIT
    emitted_exit: set[int] = set()
    # Re-entry visitor IDs (detected by re-ID)
    reentry_ids: set[str] = set()

    frame_idx = 0
    events_written = 0

    with EventEmitter(output_path, store_id) as emitter:

        def emit_event(event_type, track, zone_id=None, dwell_ms=0, extra_meta=None):
            nonlocal events_written
            vid = track.visitor_id
            session_seq[vid] += 1
            seq = session_seq[vid]

            meta = EventMetadata(
                sku_zone=zone_mapper.get_sku_zone(zone_id) if zone_id else None,
                session_seq=seq,
            )
            if extra_meta:
                for k, v in extra_meta.items():
                    setattr(meta, k, v)

            ts = make_timestamp(clip_start, frame_idx / fps)
            evt = StoreEvent(
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=vid,
                event_type=event_type,
                timestamp=ts,
                zone_id=zone_id,
                dwell_ms=dwell_ms,
                is_staff=track.is_staff,
                confidence=round(track.confidence, 3),
                metadata=meta,
            )
            emitter.emit(evt)
            events_written += 1

            if push_to_api:
                try:
                    import urllib.request
                    payload = json.dumps({"events": [evt.to_dict()]}).encode()
                    req = urllib.request.Request(
                        push_to_api,
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    urllib.request.urlopen(req, timeout=2)
                except Exception as e:
                    logger.debug(f"API push failed (non-fatal): {e}")

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            frame_idx += 1
            if frame_idx % skip_frames != 0:
                continue

            results = model(frame, classes=[0], verbose=False, conf=confidence_threshold)

            detections = []
            for r in results:
                for box in r.boxes:
                    bbox = box.xyxy[0].cpu().numpy().tolist()
                    conf = float(box.conf[0])
                    appearance = get_appearance_vector(frame, bbox)
                    is_staff = classify_staff(frame, bbox)
                    detections.append({
                        "bbox": bbox,
                        "confidence": conf,
                        "appearance": appearance,
                        "is_staff": is_staff,
                    })

            active_tracks, lost_ids = tracker.update(detections, frame_idx)

            # Handle lost tracks — they've exited or disappeared
            for tid in lost_ids:
                # We can't get the track anymore after removal, but we tracked exits
                pass

            for track in active_tracks:
                tid = track.track_id
                centroid_history[tid].append(track.centroid)

                zone_id = zone_mapper.get_zone(camera_id, track.bbox, frame_w, frame_h)
                prev_zone = track_zone_history.get(tid)

                # Entry detection — check crossing the threshold line
                if zone_mapper.is_entry_zone(zone_id):
                    direction = determine_direction(centroid_history[tid], entry_x_threshold, frame_w)

                    if direction == "ENTRY" and tid not in emitted_entry:
                        emitted_entry.add(tid)
                        if track.visitor_id in reentry_ids or getattr(track, 'is_reentry', False):
                            emit_event("REENTRY", track)
                        else:
                            emit_event("ENTRY", track)

                    elif direction == "EXIT" and tid not in emitted_exit and tid in emitted_entry:
                        emitted_exit.add(tid)
                        emit_event("EXIT", track)

                # Zone change detection
                if prev_zone != zone_id:
                    if prev_zone and prev_zone not in ("ENTRY",):
                        emit_event("ZONE_EXIT", track, zone_id=prev_zone)

                    if zone_id and zone_id not in ("ENTRY",):
                        emit_event("ZONE_ENTER", track, zone_id=zone_id)

                        if zone_mapper.is_billing_zone(zone_id):
                            billing_count = sum(
                                1 for t in active_tracks
                                if track_zone_history.get(t.track_id) == "BILLING"
                                and not t.is_staff
                            )
                            if billing_count > 0:
                                emit_event("BILLING_QUEUE_JOIN", track, zone_id=zone_id,
                                           extra_meta={"queue_depth": billing_count})
                            billing_visitors[track.visitor_id] = frame_idx / fps

                    track_zone_history[tid] = zone_id
                    zone_dwell_frames[tid] = {"zone_id": zone_id, "start_frame": frame_idx, "last_dwell_emit": frame_idx}

                # Dwell event — emit every 30 seconds of continued presence
                if zone_id and zone_id not in ("ENTRY",) and tid in zone_dwell_frames:
                    dwell_info = zone_dwell_frames[tid]
                    if dwell_info.get("zone_id") == zone_id:
                        frames_in_zone = frame_idx - dwell_info["start_frame"]
                        frames_since_dwell = frame_idx - dwell_info.get("last_dwell_emit", frame_idx)
                        dwell_ms = int((frames_in_zone / fps) * 1000)

                        if frames_in_zone > 0 and dwell_ms >= 30000 and frames_since_dwell >= (30 * fps):
                            emit_event("ZONE_DWELL", track, zone_id=zone_id, dwell_ms=dwell_ms)
                            zone_dwell_frames[tid]["last_dwell_emit"] = frame_idx

                if frame_idx % 450 == 0:
                    pct = (frame_idx / total_frames) * 100
                    logger.info(f"Progress: {pct:.1f}% | Active tracks: {len(active_tracks)} | Events: {events_written}")

    cap.release()
    logger.info(f"Finished. Total events: {events_written} → {output_path}")
    return events_written


def main():
    parser = argparse.ArgumentParser(description="CCTV person detection and event emission")
    parser.add_argument("--video", required=True, help="Path to the video clip")
    parser.add_argument("--camera-id", required=True, help="Camera identifier (e.g. CAM_1)")
    parser.add_argument("--store-id", default="STORE_BLR_002")
    parser.add_argument("--layout", default="data/store_layout.json")
    parser.add_argument("--output", required=True, help="Output .jsonl file path")
    parser.add_argument("--clip-start", required=True, help="ISO-8601 UTC start time of clip")
    parser.add_argument("--skip-frames", type=int, default=3, help="Process every Nth frame (default: 3)")
    parser.add_argument("--conf", type=float, default=0.35, help="Detection confidence threshold")
    parser.add_argument("--push-to-api", default=None, help="If set, stream events to this URL in real time")
    args = parser.parse_args()

    from datetime import datetime
    clip_start = datetime.strptime(args.clip_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    run_detection(
        video_path=args.video,
        camera_id=args.camera_id,
        store_id=args.store_id,
        layout_path=args.layout,
        output_path=args.output,
        clip_start=clip_start,
        skip_frames=args.skip_frames,
        confidence_threshold=args.conf,
        push_to_api=args.push_to_api,
    )


if __name__ == "__main__":
    from datetime import timezone
    main()
