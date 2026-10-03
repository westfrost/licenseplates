"""Glue: camera -> motion gate -> recogniser -> tracker -> storage / snapshots / webhook."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from .camera import Camera
from .config import Config
from .motion import MotionDetector
from .notifier import WebhookNotifier
from .plates import PlateRules, format_plate
from .recognizer import PlateDetection, Recognizer, build_recognizer
from .storage import Storage
from .tracker import EventData, Tracker

log = logging.getLogger(__name__)

DIRECTION_DA = {"in": "ind", "out": "ud", "through": "forbi", "unknown": "ukendt"}


class EventWriter:
    """Tracker sink: writes snapshots, rows and notifications."""

    def __init__(self, cfg: Config, storage: Storage, notifier: WebhookNotifier | None = None):
        self.cfg = cfg
        self.storage = storage
        self.notifier = notifier
        self.snap_dir = cfg.snapshot_path
        self.tz = ZoneInfo(cfg.timezone)
        self.on_event: list[Any] = []  # callbacks, e.g. to wake up the dashboard

    def _save_images(self, data: EventData) -> tuple[str | None, str | None]:
        if not self.cfg.storage.save_snapshots or data.frame is None or data.bbox is None:
            return None, None
        local = datetime.fromtimestamp(data.first_seen, self.tz)
        rel_dir = Path(local.strftime("%Y/%m/%d"))
        (self.snap_dir / rel_dir).mkdir(parents=True, exist_ok=True)
        stem = f"{local.strftime('%H%M%S')}_{data.plate}_{data.track_id}"

        x1, y1, x2, y2 = data.bbox
        h, w = data.frame.shape[:2]
        pad_x, pad_y = int((x2 - x1) * 0.15), int((y2 - y1) * 0.3)
        crop = data.frame[max(0, y1 - pad_y):min(h, y2 + pad_y), max(0, x1 - pad_x):min(w, x2 + pad_x)]
        if crop.size and crop.shape[1] < 240:
            scale = 240 / crop.shape[1]
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        img = data.frame.copy()
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 255), max(2, w // 640))
        if w > self.cfg.storage.snapshot_max_width:
            scale = self.cfg.storage.snapshot_max_width / w
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        snap, crop_rel = rel_dir / f"{stem}.jpg", rel_dir / f"{stem}_plade.jpg"
        cv2.imwrite(str(self.snap_dir / snap), img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if crop.size:
            cv2.imwrite(str(self.snap_dir / crop_rel), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return snap.as_posix(), crop_rel.as_posix() if crop.size else None

    def create_event(self, data: EventData) -> int:
        try:
            snapshot, crop = self._save_images(data)
        except Exception:  # noqa: BLE001 - a full disk must not stop recognition
            log.exception("Could not save snapshot")
            snapshot, crop = None, None
        event_id = self.storage.add_event(
            plate=data.plate, first_seen=data.first_seen, last_seen=data.last_seen, direction=data.direction,
            parked=data.parked, confidence=data.confidence, reads=data.reads, alternatives=data.alternatives,
            valid_format=data.valid_format, region=data.region, snapshot=snapshot, crop=crop,
        )
        self._notify(event_id)
        for cb in self.on_event:
            cb(event_id)
        return event_id

    def update_event(self, event_id: int, **fields: Any) -> None:
        self.storage.update_event(event_id, **fields)

    def mark_departed(self, event_id: int, ts: float) -> None:
        self.storage.mark_departed(event_id, ts)

    def mark_departed_plate(self, plate: str, ts: float) -> None:
        self.storage.mark_departed_plate(plate, ts)

    def _notify(self, event_id: int) -> None:
        if self.notifier is None:
            return
        ev = self.storage.get_event(event_id)
        if ev is None:
            return
        vehicle = self.storage.get_vehicle(ev["plate"])
        wanted = (not self.cfg.notify.only_unknown or vehicle is None
                  or bool(vehicle and (vehicle["notify"] or vehicle["category"] == "blokeret")))
        if not wanted:
            return
        name = vehicle["label"] if vehicle and vehicle["label"] else ("Ukendt bil" if vehicle is None else ev["plate"])
        direction = DIRECTION_DA.get(ev["direction"], ev["direction"])
        title = f"{format_plate(ev['plate'])} - {direction}" + (" (parkeret)" if ev["parked"] else "")
        self.notifier.send({
            "title": title,
            "message": f"{name}: {format_plate(ev['plate'])} kørte {direction} "
                       f"kl. {datetime.fromtimestamp(ev['first_seen'], self.tz):%H:%M}",
            "event_id": event_id,
            "plate": ev["plate"],
            "plate_formatted": format_plate(ev["plate"]),
            "direction": ev["direction"],
            "direction_da": direction,
            "parked": ev["parked"],
            "known": vehicle is not None,
            "label": vehicle["label"] if vehicle else None,
            "category": vehicle["category"] if vehicle else None,
            "confidence": ev["confidence"],
            "time": datetime.fromtimestamp(ev["first_seen"], self.tz).isoformat(),
            "snapshot": f"/snapshots/{ev['snapshot']}" if ev["snapshot"] else None,
        })


class Pipeline:
    def __init__(self, cfg: Config, storage: Storage, recognizer: Recognizer | None = None,
                 camera: Camera | None = None):
        self.cfg = cfg
        self.storage = storage
        notifier = WebhookNotifier(cfg.notify.webhook_url, cfg.notify.timeout_s) if cfg.notify.webhook_url else None
        self.writer = EventWriter(cfg, storage, notifier)
        self.rules = PlateRules(cfg.detection.plate_patterns, cfg.detection.correction_templates)
        self.tracker = Tracker(self.writer, self.rules, cfg.direction, cfg.tracking, cfg.detection.min_ocr_conf)
        self.motion = MotionDetector(cfg.detection.roi, cfg.detection.motion_threshold)
        self._recognizer = recognizer
        self.camera = camera or Camera(cfg.camera.source, cfg.camera.rtsp_tcp, cfg.camera.realtime,
                                       cfg.camera.loop, cfg.camera.reconnect_delay_s)
        self._lock = threading.Lock()
        self._live_cond = threading.Condition()
        self._live_jpeg: bytes | None = None
        self._live_seq = 0
        self._live_wanted_until = 0.0
        self._last_dets: list[PlateDetection] = []
        self._overlay: list[dict[str, Any]] = []
        self.started = time.time()
        self.stats: dict[str, Any] = {"processed": 0, "proc_fps": 0.0, "infer_ms": 0.0, "motion": False,
                                      "motion_level": 0.0, "last_detection": None, "error": ""}

    # ------------------------------------------------------------ live view

    def want_live(self) -> None:
        self._live_wanted_until = time.time() + 10

    def live_frame(self, last_seq: int = -1, timeout: float = 5.0) -> tuple[int, bytes | None]:
        self.want_live()
        with self._live_cond:
            self._live_cond.wait_for(lambda: self._live_seq != last_seq, timeout=timeout)
            return self._live_seq, self._live_jpeg

    def _draw_live(self, frame: np.ndarray, ts: float) -> None:
        img = frame.copy()
        h, w = img.shape[:2]
        th = max(1, w // 800)
        x1, y1, x2, y2 = self.cfg.detection.roi
        cv2.rectangle(img, (int(x1 * w), int(y1 * h)), (int(x2 * w) - 1, int(y2 * h) - 1), (0, 255, 255), th)
        for d in self._last_dets:
            cv2.rectangle(img, d.bbox[:2], d.bbox[2:], (80, 220, 80), th + 1)
        for tr in self._overlay:
            bx1, by1, bx2, by2 = tr["bbox"]
            pts = np.array([(int(px * w), int(py * h)) for px, py in tr["path"]], np.int32)
            if len(pts) > 1:
                cv2.polylines(img, [pts], False, (0, 140, 255), th + 1)
            label = f"{format_plate(tr['text'])}  {DIRECTION_DA.get(tr['direction'], 'parkeret')}"
            scale = max(0.6, w / 1600)
            (tw, tht), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, th + 1)
            ty = max(tht + 6, by1 - 8)
            cv2.rectangle(img, (bx1, ty - tht - 6), (bx1 + tw + 8, ty + 6), (0, 0, 0), -1)
            cv2.putText(img, label, (bx1 + 4, ty), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), th + 1,
                        cv2.LINE_AA)
        stamp = datetime.fromtimestamp(ts, ZoneInfo(self.cfg.timezone)).strftime("%Y-%m-%d %H:%M:%S")
        status = f"{stamp}  bevægelse: {'JA' if self.stats['motion'] else 'nej'}"
        cv2.putText(img, status, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, max(0.6, w / 1800), (255, 255, 255),
                    th + 1, cv2.LINE_AA)
        if w > 1280:
            img = cv2.resize(img, (1280, int(h * 1280 / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok:
            with self._live_cond:
                self._live_jpeg = buf.tobytes()
                self._live_seq += 1
                self._live_cond.notify_all()

    # ------------------------------------------------------------ main loop

    def status(self) -> dict[str, Any]:
        cam = self.camera
        return {
            **self.stats,
            "camera_connected": cam.connected,
            "camera_fps": round(cam.fps, 1),
            "camera_error": cam.last_error,
            "camera_source": cam._safe_source(),
            "resolution": cam.resolution,
            "tracks": self._overlay,
            "uptime_s": round(time.time() - self.started),
            "roi": self.cfg.detection.roi,
        }

    def run(self, stop: threading.Event) -> None:
        if self._recognizer is None:
            self._recognizer = build_recognizer(self.cfg)
        self.camera.start()
        seq, last_proc, last_motion_check, last_live, last_cleanup = -1, 0.0, 0.0, 0.0, 0.0
        proc_times: list[float] = []
        dcfg = self.cfg.detection
        try:
            while not stop.is_set():
                seq, ts, frame = self.camera.read(seq)
                if frame is None:
                    if self.camera.finished:
                        break
                    continue

                if ts - last_motion_check >= 0.1:
                    self.stats["motion"] = self.motion.update(frame)
                    self.stats["motion_level"] = round(self.motion.level, 4)
                    last_motion_check = ts

                busy = self.stats["motion"] or self.tracker.has_active()
                due = ts - last_proc >= 1.0 / dcfg.process_fps
                idle_due = ts - last_proc >= dcfg.idle_check_s
                with self._lock:
                    if (busy and due) or idle_due:
                        t0 = time.perf_counter()
                        try:
                            dets = self._recognizer.recognize(frame)
                            self.stats["error"] = ""
                        except Exception as exc:  # noqa: BLE001
                            log.exception("Recognition failed")
                            self.stats["error"] = str(exc)
                            dets = []
                        self.stats["infer_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                        self._last_dets = dets
                        if dets:
                            self.stats["last_detection"] = ts
                        self.tracker.update(dets, ts, frame)
                        last_proc = ts
                        self.stats["processed"] += 1
                        proc_times = [p for p in proc_times if ts - p < 5] + [ts]
                        self.stats["proc_fps"] = round(len(proc_times) / 5, 1)
                    else:
                        if ts - last_proc > 1.0:
                            self._last_dets = []
                        self.tracker.update([], ts, None, (frame.shape[1], frame.shape[0]))
                    self._overlay = self.tracker.overlay()

                if time.time() < self._live_wanted_until and ts - last_live >= 1.0 / self.cfg.web.live_fps:
                    self._draw_live(frame, ts)
                    last_live = ts

                if time.time() - last_cleanup > 3600:
                    last_cleanup = time.time()
                    self.cleanup_snapshots()
                self.camera.notify_consumed(seq)
        finally:
            with self._lock:
                self.tracker.flush()
            self.camera.stop()

    def cleanup_snapshots(self) -> None:
        days = self.cfg.storage.snapshot_retention_days
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        old = self.storage.snapshot_paths_before(cutoff)
        for _, snap, crop in old:
            for rel in (snap, crop):
                if rel:
                    (self.cfg.snapshot_path / rel).unlink(missing_ok=True)
        if old:
            self.storage.clear_snapshots([i for i, _, _ in old])
            log.info("Removed snapshots for %d old events", len(old))
