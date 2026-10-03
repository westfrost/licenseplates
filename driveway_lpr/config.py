"""Configuration loaded from a YAML file, with environment variable overrides.

Every option has a sensible default, so an empty config file (or none at all)
gives a working system that reads from the first local webcam.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CameraConfig:
    # RTSP URL, HTTP MJPEG URL, video file path or a local device index ("0").
    source: str = "0"
    # Force TCP for RTSP - far more reliable than UDP over Wi-Fi / busy LANs.
    rtsp_tcp: bool = True
    # When the source is a file: play it back at its native speed (True) or as
    # fast as possible (False, useful for batch-processing recordings).
    realtime: bool = True
    loop: bool = False
    reconnect_delay_s: float = 5.0


@dataclass
class DetectionConfig:
    # fast-alpr models. The 384 detector is fast; use 512/640 if plates are small.
    detector_model: str = "yolo-v9-t-384-license-plate-end2end"
    ocr_model: str = "cct-xs-v2-global-model"
    detector_conf: float = 0.4
    # Minimum OCR confidence (mean over characters) for a read to count.
    min_ocr_conf: float = 0.5
    # Region of interest as fractions of the frame: [x1, y1, x2, y2].
    # Cropping to the driveway both speeds things up and makes plates bigger
    # relative to the detector input.
    roi: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])
    # How often to run recognition while there is motion (frames per second).
    process_fps: float = 5.0
    # How often to look anyway when nothing moves (keeps parked cars "alive").
    idle_check_s: float = 10.0
    # Fraction of ROI pixels that must change to count as motion.
    motion_threshold: float = 0.004
    # Accepted plate formats (regex, applied to text without spaces). Reads that
    # match get a voting bonus and are marked as valid. Default: Danish plates
    # (2 letters + 5 digits). Personalised plates are matched by the last one.
    plate_patterns: list[str] = field(
        default_factory=lambda: [r"^[A-Z]{2}\d{5}$", r"^[A-Z0-9]{2,7}$"]
    )
    # Patterns for which letter/digit confusions are auto-corrected
    # (L = letter, D = digit). "LLDDDDD" is the standard Danish format.
    correction_templates: list[str] = field(default_factory=lambda: ["LLDDDDD"])


@dataclass
class DirectionConfig:
    # "size": a plate getting bigger is moving towards the camera.
    # "vector": movement along `in_vector` in the image means "in".
    method: str = "size"
    # With method=size: does approaching the camera mean driving in?
    # True when the camera hangs on the house looking out over the driveway.
    approaching_is_in: bool = True
    # With method=vector: image direction (dx, dy) that means "in". [0, 1] = moving down.
    in_vector: list[float] = field(default_factory=lambda: [0.0, 1.0])
    # Relative plate-width change needed to call a direction with method=size.
    min_size_change: float = 0.15
    # Movement (fraction of frame size) needed to call a direction.
    min_move: float = 0.05
    # A car that is seen without moving for this long is "parked".
    parked_after_s: float = 20.0
    # Movement below this (fraction of frame) counts as standing still.
    stationary_eps: float = 0.015


@dataclass
class TrackingConfig:
    # A moving plate not seen for this long ends its track.
    lost_after_s: float = 4.0
    # A parked car can disappear for longer (people walking past, night etc.).
    parked_lost_after_s: float = 180.0
    # Reads needed before a track becomes an event (filters one-off misreads).
    min_reads: int = 2
    # The same plate + direction within this window is merged into one event.
    dedupe_s: float = 90.0


@dataclass
class StorageConfig:
    data_dir: str = "data"
    # Keep snapshots for this many days (0 = forever). Events are always kept.
    snapshot_retention_days: int = 90
    # Save a full-frame snapshot (with the plate marked) for each event.
    save_snapshots: bool = True
    snapshot_max_width: int = 1280


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    # Set both to require HTTP basic auth on the dashboard and API.
    username: str = ""
    password: str = ""
    # Max frame rate for the live view.
    live_fps: float = 5.0


@dataclass
class NotifyConfig:
    # POST a JSON payload to this URL for every event (Home Assistant webhook,
    # ntfy, n8n, Node-RED, ...). Empty = disabled.
    webhook_url: str = ""
    # Only notify for plates that are not registered as known vehicles.
    only_unknown: bool = False
    timeout_s: float = 5.0


@dataclass
class LookupConfig:
    """Cross-check plates against the Danish motor register (DMR)."""

    # "none", "motorregister" (free: the official public lookup on
    # motorregister.skat.dk, ~3-5 s per lookup) or "motorapi" (motorapi.dk,
    # needs an API key, 100 free lookups per day).
    provider: str = "none"
    api_key: str = ""
    api_url: str = "https://v1.motorapi.dk"
    api_header: str = "X-AUTH-TOKEN"
    timeout_s: float = 20.0
    # Results are cached; a plate is only looked up again after this many days.
    cache_days: int = 30
    not_found_cache_days: int = 3
    # If the read plate does not exist in the register, try the runner-up reads
    # from the vote and switch to the one that does exist.
    auto_correct: bool = True
    max_alternatives: int = 3
    # Politeness / quota guard across all lookups.
    max_per_hour: int = 30
    # Optional link shown in the dashboard, e.g. to nummerplade.net. {plate} is replaced.
    link_template: str = ""


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    direction: DirectionConfig = field(default_factory=DirectionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    web: WebConfig = field(default_factory=WebConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    lookup: LookupConfig = field(default_factory=LookupConfig)
    timezone: str = "Europe/Copenhagen"

    @property
    def data_path(self) -> Path:
        return Path(self.storage.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "lpr.sqlite3"

    @property
    def snapshot_path(self) -> Path:
        return self.data_path / "snapshots"


def _merge(obj: Any, values: dict[str, Any], prefix: str = "") -> None:
    for key, value in values.items():
        if not hasattr(obj, key):
            raise ValueError(f"Unknown config option: {prefix}{key}")
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"Config section {prefix}{key} must be a mapping")
            _merge(current, value, f"{prefix}{key}.")
        else:
            setattr(obj, key, value)


def _coerce(current: Any, raw: str) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return yaml.safe_load(raw)
    return raw


def _apply_env(cfg: Config, environ: dict[str, str]) -> None:
    """LPR_<SECTION>__<OPTION>=value, e.g. LPR_CAMERA__SOURCE=rtsp://..."""
    for name, raw in environ.items():
        if not name.startswith("LPR_"):
            continue
        parts = name[4:].lower().split("__")
        obj: Any = cfg
        for part in parts[:-1]:
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if obj is None or not hasattr(obj, parts[-1]):
            continue
        setattr(obj, parts[-1], _coerce(getattr(obj, parts[-1]), raw))


def load_config(path: str | os.PathLike | None = None, environ: dict[str, str] | None = None) -> Config:
    cfg = Config()
    if path is not None and Path(path).exists():
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _merge(cfg, data)
    elif path is not None:
        raise FileNotFoundError(f"Config file not found: {path}")
    _apply_env(cfg, dict(os.environ) if environ is None else environ)
    return cfg
