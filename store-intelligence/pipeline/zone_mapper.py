import json
import logging
from pathlib import Path
import numpy as np

logger = logging.getLogger(__name__)


class ZoneMapper:
    """
    Maps pixel coordinates (from any camera) to named store zones.

    Each camera has a calibration matrix that projects from image space
    to approximate floor-plan coordinates (mm). We then do a simple
    point-in-rectangle check against zone bounding boxes.

    Calibration is done manually by identifying 4 floor reference points
    in each camera view and their known real-world mm coordinates.
    """

    def __init__(self, layout_path: str):
        with open(layout_path) as f:
            layout = json.load(f)
        self.zones = layout["zones"]
        self.store_dims = layout["dimensions_mm"]
        self._homographies: dict[str, np.ndarray] = {}
        self._camera_configs = layout["cameras"]

        # Default homography fallback: treat frame as proportional to floor
        # Real calibration should replace this per camera
        self._frame_size = (1920, 1080)

    def set_homography(self, camera_id: str, H: np.ndarray):
        """Register a 3x3 homography matrix for a specific camera."""
        self._homographies[camera_id] = H

    def pixel_to_floor(self, camera_id: str, px: float, py: float, frame_w: int, frame_h: int) -> tuple[float, float]:
        """Convert pixel coords to floor-plan mm coords."""
        if camera_id in self._homographies:
            pt = np.array([[[px, py]]], dtype=np.float32)
            import cv2
            mapped = cv2.perspectiveTransform(pt, self._homographies[camera_id])
            return float(mapped[0][0][0]), float(mapped[0][0][1])
        else:
            # Proportional fallback — good enough without calibration targets
            x_mm = (px / frame_w) * self.store_dims["width"]
            y_mm = (py / frame_h) * self.store_dims["depth"]
            return x_mm, y_mm

    def get_zone(self, camera_id: str, bbox: list, frame_w: int, frame_h: int) -> str | None:
        """
        Given a bounding box in pixel space, return which store zone it falls in.
        We use the bottom-center of the bbox as the foot position.
        """
        # CAM_ENTRY_01 special case — glass door camera
        # Poora frame ENTRY zone hai
        if camera_id == "CAM_ENTRY_01":
            return "ENTRY"


        foot_x = (bbox[0] + bbox[2]) / 2
        foot_y = bbox[3]

        x_mm, y_mm = self.pixel_to_floor(camera_id, foot_x, foot_y, frame_w, frame_h)

        # Check zones in priority order (more specific zones first)
        # Billing before FOH, Entry before everything
        priority_order = [
            "ENTRY", "BILLING", "ASSIST",
            "SKINCARE_PREMIUM", "FRAGRANCE", "NAIL_UNIT",
            "MAKEUP_UNIT", "MAKEUP_MASS", "MENS_CARE", "HAIRCARE_MASS",
            "FOH"
        ]

        zone_map = {z["zone_id"]: z for z in self.zones}

        for zone_id in priority_order:
            if zone_id not in zone_map:
                continue
            z = zone_map[zone_id]
            b = z["bbox_mm"]
            if b["x1"] <= x_mm <= b["x2"] and b["y1"] <= y_mm <= b["y2"]:
                return zone_id

        return "FOH"  # Default to main floor if nothing matches

    def is_entry_zone(self, zone_id: str) -> bool:
        return zone_id == "ENTRY"

    def is_billing_zone(self, zone_id: str) -> bool:
        return zone_id == "BILLING"

    def get_sku_zone(self, zone_id: str) -> str | None:
        for z in self.zones:
            if z["zone_id"] == zone_id:
                return z.get("sku_zone")
        return None
