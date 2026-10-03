"""Threaded frame grabber with automatic reconnect.

Reading continuously in a background thread keeps the RTSP buffer drained, so
the pipeline always works on the newest frame instead of one that is seconds
old.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


class Camera:
    def __init__(self, source: str, rtsp_tcp: bool = True, realtime: bool = True, loop: bool = False,
                 reconnect_delay_s: float = 5.0):
        self.source = source
        self.realtime = realtime
        self.loop = loop
        self.reconnect_delay_s = reconnect_delay_s
        self.is_file = Path(source).is_file()
        if rtsp_tcp and source.lower().startswith("rtsp"):
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

        self._frame: np.ndarray | None = None
        self._frame_ts = 0.0
        self._seq = 0
        self._consumed = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.finished = False  # a non-looping file has ended
        self.fps = 0.0
        self.resolution: tuple[int, int] | None = None
        self.last_error = ""

    def start(self) -> "Camera":
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    def read(self, last_seq: int = -1, timeout: float = 2.0) -> tuple[int, float, np.ndarray | None]:
        """Wait for a frame newer than `last_seq`. Returns (seq, timestamp, frame)."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq or self._stop.is_set() or self.finished,
                                timeout=timeout)
            if self._seq == last_seq:
                return self._seq, self._frame_ts, None
            return self._seq, self._frame_ts, self._frame

    def _open(self) -> cv2.VideoCapture:
        src: str | int = int(self.source) if self.source.isdigit() else self.source
        cap = cv2.VideoCapture(src)
        if isinstance(src, int):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _run(self) -> None:
        while not self._stop.is_set():
            cap = self._open()
            if not cap.isOpened():
                self.connected = False
                self.last_error = f"Kunne ikke åbne kamera: {self._safe_source()}"
                log.warning("%s - retrying in %.0fs", self.last_error, self.reconnect_delay_s)
                self._stop.wait(self.reconnect_delay_s)
                continue

            log.info("Camera connected: %s", self._safe_source())
            self.connected, self.last_error = True, ""
            native_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            frame_interval = 1.0 / native_fps if 0 < native_fps < 120 else 0.04
            # Simulated clock for files so timestamps follow the video, not the CPU.
            file_clock = time.time()
            count, window_start = 0, time.time()

            while not self._stop.is_set():
                t0 = time.time()
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                if self.is_file:
                    file_clock += frame_interval
                    ts = file_clock
                else:
                    ts = time.time()
                with self._cond:
                    self._frame, self._frame_ts = frame, ts
                    self._seq += 1
                    self._cond.notify_all()
                self.resolution = (frame.shape[1], frame.shape[0])

                count += 1
                if t0 - window_start >= 2.0:
                    self.fps = count / (t0 - window_start)
                    count, window_start = 0, t0
                if self.is_file and self.realtime:
                    time.sleep(max(0.0, frame_interval - (time.time() - t0)))
                elif self.is_file:
                    # Batch mode: wait until the consumer has taken this frame so none are skipped.
                    with self._cond:
                        self._cond.wait_for(lambda: self._consumed >= self._seq or self._stop.is_set(),
                                            timeout=30)

            cap.release()
            self.connected = False
            if self.is_file and not self.loop:
                log.info("End of video file")
                self.finished = True
                with self._cond:
                    self._cond.notify_all()
                return
            if not self.is_file:
                self.last_error = "Forbindelsen til kameraet blev afbrudt"
                log.warning("Camera stream lost - reconnecting in %.0fs", self.reconnect_delay_s)
                self._stop.wait(self.reconnect_delay_s)

    def notify_consumed(self, seq: int) -> None:
        """Batch file mode: the consumer is done with frame `seq` and wants the next one."""
        with self._cond:
            self._consumed = max(self._consumed, seq)
            self._cond.notify_all()

    def _safe_source(self) -> str:
        """Source without the password, for logs and the dashboard."""
        src = self.source
        if "@" in src and "://" in src:
            scheme, rest = src.split("://", 1)
            creds, host = rest.rsplit("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
        return src
