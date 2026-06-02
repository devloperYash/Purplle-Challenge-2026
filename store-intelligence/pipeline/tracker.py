import numpy as np
import hashlib
import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# How long (seconds) to keep an appearance fingerprint in memory for re-ID matching
REID_WINDOW_SECONDS = 120

# If we haven't seen a track for this many frames, consider it gone
MAX_MISSING_FRAMES = 30

# IoU threshold to associate detections with existing tracks
IOU_THRESHOLD = 0.3

# Minimum appearance similarity to match a re-entering visitor
REID_SIM_THRESHOLD = 0.88


@dataclass
class Track:
    track_id: int
    visitor_id: str
    bbox: list           # [x1, y1, x2, y2]
    centroid: tuple      # (cx, cy)
    appearance: np.ndarray
    is_staff: bool
    confidence: float
    last_seen_frame: int
    first_seen_frame: int
    zone_id: Optional[str] = None
    zone_enter_frame: Optional[int] = None
    session_seq: int = 0
    missing_frames: int = 0
    has_exited: bool = False

    def update(self, bbox, appearance, confidence, frame_idx):
        self.bbox = bbox
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        self.centroid = (cx, cy)
        self.appearance = 0.7 * self.appearance + 0.3 * appearance
        self.confidence = confidence
        self.last_seen_frame = frame_idx
        self.missing_frames = 0


class MultiCameraTracker:
    def __init__(self, fps: float = 15.0):
        self.fps = fps
        self.tracks: dict[int, Track] = {}
        self.next_track_id = 1
        self.exited_appearances: list[dict] = []
        self._frame_count = 0

    def _iou(self, a: list, b: list) -> float:
        xi1 = max(a[0], b[0])
        yi1 = max(a[1], b[1])
        xi2 = min(a[2], b[2])
        yi2 = min(a[3], b[3])
        inter = max(0, xi2 - xi1) * max(0, yi2 - yi1)
        if inter == 0:
            return 0.0
        area_a = (a[2] - a[0]) * (a[3] - a[1])
        area_b = (b[2] - b[0]) * (b[3] - b[1])
        return inter / (area_a + area_b - inter)

    def _appearance_sim(self, a: np.ndarray, b: np.ndarray) -> float:
        dot = np.dot(a, b)
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        return float(dot / norm) if norm > 0 else 0.0

    def _make_visitor_id(self, track_id: int) -> str:
        raw = f"track_{track_id}_{time.time_ns()}"
        return "VIS_" + hashlib.md5(raw.encode()).hexdigest()[:6]

    def _match_exited(self, appearance: np.ndarray, bbox: list) -> Optional[str]:
        """Check if this detection matches someone who recently left."""
        best_sim = 0.0
        best_vid = None
        now_ts = time.time()

        for record in self.exited_appearances:
            if now_ts - record["exit_time"] > REID_WINDOW_SECONDS:
                continue
            sim = self._appearance_sim(appearance, record["appearance"])
            if sim > best_sim:
                best_sim = sim
                best_vid = record["visitor_id"]

        if best_sim >= REID_SIM_THRESHOLD:
            return best_vid
        return None

    def update(self, detections: list[dict], frame_idx: int) -> tuple[list[Track], list[int]]:
        """
        Match detections to existing tracks using IoU + appearance.

        Returns:
            active_tracks: updated track objects for this frame
            lost_track_ids: track IDs that just went missing this frame
        """
        self._frame_count = frame_idx

        # Mark all existing tracks as missing for now
        for t in self.tracks.values():
            t.missing_frames += 1

        matched_track_ids = set()

        for det in detections:
            bbox = det["bbox"]
            appearance = det["appearance"]
            confidence = det["confidence"]
            is_staff = det["is_staff"]

            # Try to match with existing tracks by IoU
            best_iou = 0.0
            best_id = None
            for tid, track in self.tracks.items():
                if track.missing_frames > 1:
                    continue
                iou = self._iou(bbox, track.bbox)
                if iou > best_iou:
                    best_iou = iou
                    best_id = tid

            if best_iou >= IOU_THRESHOLD and best_id is not None:
                self.tracks[best_id].update(bbox, appearance, confidence, frame_idx)
                matched_track_ids.add(best_id)
            else:
                # New detection — check re-ID against exited visitors
                reentry_vid = self._match_exited(appearance, bbox)

                new_tid = self.next_track_id
                self.next_track_id += 1

                if reentry_vid:
                    visitor_id = reentry_vid
                    is_reentry = True
                else:
                    visitor_id = self._make_visitor_id(new_tid)
                    is_reentry = False

                cx = (bbox[0] + bbox[2]) / 2
                cy = (bbox[1] + bbox[3]) / 2

                track = Track(
                    track_id=new_tid,
                    visitor_id=visitor_id,
                    bbox=bbox,
                    centroid=(cx, cy),
                    appearance=appearance.copy(),
                    is_staff=is_staff,
                    confidence=confidence,
                    last_seen_frame=frame_idx,
                    first_seen_frame=frame_idx,
                    missing_frames=0,
                )
                track.is_reentry = is_reentry
                self.tracks[new_tid] = track
                matched_track_ids.add(new_tid)

        # Collect tracks that just became lost
        lost_ids = []
        to_remove = []
        for tid, track in self.tracks.items():
            if track.missing_frames >= MAX_MISSING_FRAMES:
                lost_ids.append(tid)
                to_remove.append(tid)
                self.exited_appearances.append({
                    "visitor_id": track.visitor_id,
                    "appearance": track.appearance.copy(),
                    "exit_time": time.time(),
                })

        for tid in to_remove:
            del self.tracks[tid]

        # Clean up old exit records
        cutoff = time.time() - REID_WINDOW_SECONDS
        self.exited_appearances = [
            r for r in self.exited_appearances if r["exit_time"] > cutoff
        ]

        return list(self.tracks.values()), lost_ids
