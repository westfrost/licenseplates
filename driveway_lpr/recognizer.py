"""Plate detection + OCR backends."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class PlateDetection:
    text: str  # raw OCR text
    confidence: float  # mean OCR character confidence, 0..1
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2 in full-frame pixels
    det_confidence: float = 1.0
    region: str | None = None


class Recognizer(Protocol):
    def recognize(self, frame: np.ndarray) -> list[PlateDetection]: ...


def crop_roi(frame: np.ndarray, roi: list[float]) -> tuple[np.ndarray, int, int]:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = roi
    px1, py1 = int(max(0.0, x1) * w), int(max(0.0, y1) * h)
    px2, py2 = int(min(1.0, x2) * w), int(min(1.0, y2) * h)
    if px2 - px1 < 16 or py2 - py1 < 16:
        return frame, 0, 0
    return frame[py1:py2, px1:px2], px1, py1


class FastAlprRecognizer:
    """Local, CPU-friendly recogniser based on fast-alpr (ONNX models)."""

    def __init__(self, detector_model: str, ocr_model: str, detector_conf: float, roi: list[float]):
        import onnxruntime as ort
        from fast_alpr import ALPR  # heavy import, keep it lazy

        available = ort.get_available_providers()
        # Use a GPU / OpenVINO if onnxruntime was installed with one, else plain CPU.
        preferred = ["CUDAExecutionProvider", "OpenVINOExecutionProvider", "CPUExecutionProvider"]
        providers = [p for p in preferred if p in available] or ["CPUExecutionProvider"]

        log.info("Loading ALPR models (%s, %s)", detector_model, ocr_model)
        self.alpr = ALPR(
            detector_model=detector_model,
            detector_conf_thresh=detector_conf,
            ocr_model=ocr_model,
            ocr_device="cpu",
            # Explicit providers: keeps onnxruntime's Azure provider (network calls) out.
            detector_providers=providers,
            ocr_providers=providers,
        )
        self.roi = roi

    def recognize(self, frame: np.ndarray) -> list[PlateDetection]:
        crop, ox, oy = crop_roi(frame, self.roi)
        results = []
        for res in self.alpr.predict(crop):
            if res.ocr is None or not res.ocr.text:
                continue
            text = res.ocr.text
            conf = res.ocr.confidence
            if isinstance(conf, list):
                # The OCR returns one value per slot (including padding); only
                # count the slots that hold actual characters.
                used = conf[: max(1, len(text.replace("_", "")))]
                conf = float(sum(used) / len(used))
            bb = res.detection.bounding_box
            results.append(
                PlateDetection(
                    text=text,
                    confidence=float(conf),
                    bbox=(bb.x1 + ox, bb.y1 + oy, bb.x2 + ox, bb.y2 + oy),
                    det_confidence=float(res.detection.confidence),
                    region=res.ocr.region if res.ocr.region not in (None, "Unknown") else None,
                )
            )
        return results


def build_recognizer(cfg) -> Recognizer:
    d = cfg.detection
    return FastAlprRecognizer(d.detector_model, d.ocr_model, d.detector_conf, d.roi)
