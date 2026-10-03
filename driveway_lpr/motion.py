"""Cheap motion gate so the (more expensive) plate detector only runs when needed."""

from __future__ import annotations

import cv2
import numpy as np

from .recognizer import crop_roi


class MotionDetector:
    def __init__(self, roi: list[float], threshold: float = 0.004, width: int = 320):
        self.roi = roi
        self.threshold = threshold
        self.width = width
        self._bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32, detectShadows=False)
        self.level = 0.0

    def update(self, frame: np.ndarray) -> bool:
        crop, _, _ = crop_roi(frame, self.roi)
        h, w = crop.shape[:2]
        small = cv2.resize(crop, (self.width, max(1, int(h * self.width / w))), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        mask = self._bg.apply(gray)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        self.level = float(np.count_nonzero(mask)) / mask.size
        return self.level >= self.threshold
