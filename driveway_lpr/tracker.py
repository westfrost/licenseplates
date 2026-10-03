"""Follows plates across frames and turns them into events.

A *track* is one plate seen over consecutive frames. While it is followed we
collect OCR reads (for voting) and positions (for direction). The tracker
emits events through a small `EventSink` interface so it has no knowledge of
databases or files, which keeps it easy to test.

Lifecycle of a track::

    MOVING --(still for parked_after_s)--> PARKED --(moves again)--> MOVING
       |                                     |                         |
     lost: emit pass event            lost: keep event,          lost: emit departure
       (in / out / through)            just stop updating          and close the stay
"""

from __future__ import annotations

import itertools
import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

import numpy as np

from .config import DirectionConfig, TrackingConfig
from .plates import PlateRules, Read, similarity, vote
from .recognizer import PlateDetection

log = logging.getLogger(__name__)

IN, OUT, THROUGH, UNKNOWN = "in", "out", "through", "unknown"


class State(str, Enum):
    MOVING = "moving"
    PARKED = "parked"


@dataclass
class Obs:
    t: float
    cx: float  # plate centre, fraction of frame width
    cy: float  # plate centre, fraction of frame height
    w: float  # plate width, fraction of frame width
    bbox: tuple[int, int, int, int]


@dataclass
class EventData:
    plate: str
    confidence: float
    reads: int
    alternatives: list[tuple[str, int]]
    valid_format: bool
    region: str | None
    direction: str
    parked: bool
    first_seen: float
    last_seen: float
    frame: np.ndarray | None
    bbox: tuple[int, int, int, int] | None
    track_id: int


class EventSink(Protocol):
    def create_event(self, data: EventData) -> int: ...

    def update_event(self, event_id: int, **fields: Any) -> None: ...

    def mark_departed(self, event_id: int, ts: float) -> None: ...

    def mark_departed_plate(self, plate: str, ts: float) -> None: ...


@dataclass
class Track:
    id: int
    created: float
    last_seen: float
    state: State = State.MOVING
    reads: list[Read] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    segment: list[Obs] = field(default_factory=list)  # positions since the segment started
    anchor: Obs | None = None  # where the plate has been standing still since
    still_since: float = 0.0
    event_id: int | None = None  # arrival / stay event while parked
    voted_text: str = ""
    last_db_update: float = 0.0
    best_score: float = -1.0
    best_frame: np.ndarray | None = None
    best_bbox: tuple[int, int, int, int] | None = None

    @property
    def last(self) -> Obs:
        return self.segment[-1]


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _median(values: list[float]) -> float:
    return float(np.median(values)) if values else 0.0


def classify_direction(obs: list[Obs], cfg: DirectionConfig) -> str:
    """Decide in / out / through / unknown from a list of positions."""
    if len(obs) < 2:
        return UNKNOWN
    k = max(1, min(3, len(obs) // 3))
    head, tail = obs[:k], obs[-k:]
    w0, w1 = _median([o.w for o in head]), _median([o.w for o in tail])
    dx = _median([o.cx for o in tail]) - _median([o.cx for o in head])
    dy = _median([o.cy for o in tail]) - _median([o.cy for o in head])
    moved = float(np.hypot(dx, dy))
    size_change = (w1 / w0 - 1.0) if w0 > 0 else 0.0

    if cfg.method == "vector":
        vx, vy = cfg.in_vector
        norm = float(np.hypot(vx, vy)) or 1.0
        proj = (dx * vx + dy * vy) / norm
        if abs(proj) >= cfg.min_move:
            return IN if proj > 0 else OUT
        if moved >= cfg.min_move:
            return THROUGH
        return UNKNOWN

    if abs(size_change) >= cfg.min_size_change:
        approaching = size_change > 0
        return IN if approaching == cfg.approaching_is_in else OUT
    if moved >= cfg.min_move:
        return THROUGH
    return UNKNOWN


class Tracker:
    REATTACH_DISTANCE = 0.12  # max centre distance to call it "the same parked car"

    def __init__(
        self,
        sink: EventSink,
        rules: PlateRules,
        direction: DirectionConfig,
        tracking: TrackingConfig,
        min_ocr_conf: float = 0.5,
        reattach_parked_s: float = 12 * 3600,
    ):
        self.sink = sink
        self.rules = rules
        self.dcfg = direction
        self.tcfg = tracking
        self.min_ocr_conf = min_ocr_conf
        self.reattach_parked_s = reattach_parked_s
        self.tracks: list[Track] = []
        self._ids = itertools.count(1)
        # (plate, direction, event_id, last_seen) of recently finished passes
        self.recent: deque[tuple[str, str, int, float]] = deque(maxlen=50)
        # plate -> (event_id, last position, last_seen) of cars that were parked
        self.parked_memory: dict[str, tuple[int, Obs, float]] = {}

    # ------------------------------------------------------------------ public

    def update(self, detections: list[PlateDetection], t: float, frame: np.ndarray | None,
               frame_size: tuple[int, int] | None = None) -> None:
        """Feed one processed frame. `frame_size` is (width, height)."""
        if frame_size is None:
            if frame is None:
                raise ValueError("Need a frame or frame_size")
            frame_size = (frame.shape[1], frame.shape[0])
        fw, fh = frame_size

        cleaned = []
        for d in detections:
            text = self.rules.clean(d.text)
            x1, y1, x2, y2 = d.bbox
            obs = Obs(t=t, cx=(x1 + x2) / 2 / fw, cy=(y1 + y2) / 2 / fh, w=(x2 - x1) / fw, bbox=d.bbox)
            cleaned.append((d, text, obs))

        for track, (d, text, obs) in self._assign(cleaned):
            if track is None:
                if d.confidence < self.min_ocr_conf or not text:
                    continue
                track = Track(id=next(self._ids), created=t, last_seen=t, still_since=t)
                self.tracks.append(track)
                log.debug("New track %d (%s)", track.id, text)
            self._observe(track, d, text, obs, frame)

        for track in list(self.tracks):
            timeout = self.tcfg.parked_lost_after_s if track.state is State.PARKED else self.tcfg.lost_after_s
            if t - track.last_seen > timeout:
                self._finish(track)
                self.tracks.remove(track)

    def flush(self) -> None:
        """Finish all tracks (on shutdown)."""
        for track in self.tracks:
            self._finish(track)
        self.tracks.clear()

    def has_active(self) -> bool:
        return any(t.state is State.MOVING for t in self.tracks)

    def overlay(self) -> list[dict[str, Any]]:
        """Current tracks for drawing on the live view."""
        out = []
        for tr in self.tracks:
            out.append({
                "id": tr.id,
                "bbox": tr.last.bbox,
                "text": tr.voted_text,
                "state": tr.state.value,
                "direction": classify_direction(tr.segment, self.dcfg) if tr.state is State.MOVING else "parked",
                "age": tr.last.t - tr.created,
                "path": [(o.cx, o.cy) for o in tr.segment[-40:]],
            })
        return out

    # --------------------------------------------------------------- matching

    def _assign(self, cleaned):
        """Greedy matching of detections to existing tracks.

        Returns (track or None, (detection, text, obs)) pairs.
        """
        pairs = []
        for di, (d, text, obs) in enumerate(cleaned):
            for tr in self.tracks:
                last = tr.last
                dist = float(np.hypot(obs.cx - last.cx, obs.cy - last.cy))
                reach = max(obs.w, last.w) * 3.0 + 0.02
                iou = _iou(obs.bbox, last.bbox)
                text_sim = similarity(text, tr.voted_text) if tr.voted_text and text else 0.0
                if iou <= 0 and dist > reach and text_sim < 0.7:
                    continue
                score = iou + max(0.0, 1.0 - dist / reach) + text_sim
                pairs.append((score, di, tr))
        pairs.sort(key=lambda p: -p[0])
        used_d, used_t = set(), set()
        result = []
        for _, di, tr in pairs:
            if di in used_d or tr.id in used_t:
                continue
            used_d.add(di)
            used_t.add(tr.id)
            result.append((tr, cleaned[di]))
        for di, item in enumerate(cleaned):
            if di not in used_d:
                result.append((None, item))
        return result

    # ------------------------------------------------------------- per track

    def _observe(self, tr: Track, d: PlateDetection, text: str, obs: Obs, frame) -> None:
        tr.last_seen = obs.t
        tr.segment.append(obs)
        if len(tr.segment) > 600:  # keep the start (needed for direction) + recent history
            del tr.segment[10:-500]

        if d.confidence >= self.min_ocr_conf and text:
            tr.reads.append(Read(text, d.confidence))
            if len(tr.reads) > 300:
                tr.reads = tr.reads[-300:]
            if d.region:
                tr.regions.append(d.region)
            score = d.confidence * (1.5 if self.rules.is_strict(text) else 1.0) * (0.5 + obs.w)
            if frame is not None and score > tr.best_score:
                tr.best_score, tr.best_frame, tr.best_bbox = score, frame, d.bbox
            v = vote(tr.reads, self.rules)
            if v is not None:
                tr.voted_text = v.text

        self._update_stillness(tr, obs)

    def _update_stillness(self, tr: Track, obs: Obs) -> None:
        a = tr.anchor
        moved = a is None or (
            np.hypot(obs.cx - a.cx, obs.cy - a.cy) > self.dcfg.stationary_eps
            or abs(obs.w / a.w - 1.0) > self.dcfg.stationary_eps * 6
        )
        if moved:
            tr.anchor = obs
            tr.still_since = obs.t
            if tr.state is State.PARKED:
                log.info("Track %d (%s) is moving again", tr.id, tr.voted_text)
                tr.state = State.MOVING
                tr.segment = tr.segment[-2:]  # departure segment starts at the parking spot
                tr.best_score = -1.0
            return

        if tr.state is State.MOVING and obs.t - tr.still_since >= self.dcfg.parked_after_s:
            self._park(tr)
        elif tr.state is State.PARKED and tr.event_id is not None:
            self._refresh_parked(tr, obs.t)

    def _park(self, tr: Track) -> None:
        tr.state = State.PARKED
        if tr.event_id is not None:  # re-parked after a small move
            return
        v = vote(tr.reads, self.rules)
        if v is None or v.reads < self.tcfg.min_reads:
            return

        # Same car we already know is parked here (e.g. track lost overnight)?
        mem = self.parked_memory.get(v.text)
        if mem is None:
            for plate, item in self.parked_memory.items():
                if similarity(plate, v.text) >= 0.85:
                    mem = item
                    break
        if mem is not None:
            ev_id, pos, seen = mem
            near = np.hypot(pos.cx - tr.last.cx, pos.cy - tr.last.cy) <= self.REATTACH_DISTANCE
            if near and tr.last.t - seen <= self.reattach_parked_s:
                tr.event_id = ev_id
                self.sink.update_event(ev_id, last_seen=tr.last.t)
                log.info("Track %d re-attached to parked event %d (%s)", tr.id, ev_id, v.text)
                return

        moving_part = [o for o in tr.segment if o.t <= tr.still_since]
        direction = classify_direction(moving_part, self.dcfg)
        tr.event_id = self._emit(tr, direction, parked=True, first_seen=tr.created, last_seen=tr.last_seen)
        tr.last_db_update = tr.last_seen
        tr.segment = tr.segment[-2:]
        tr.best_score = -1.0

    def _refresh_parked(self, tr: Track, t: float) -> None:
        if t - tr.last_db_update < 30:
            return
        tr.last_db_update = t
        fields: dict[str, Any] = {"last_seen": t}
        v = vote(tr.reads, self.rules)
        if v is not None:
            fields.update(plate=v.text, confidence=v.confidence, reads=v.reads,
                          valid_format=self.rules.is_valid(v.text))
        self.sink.update_event(tr.event_id, **fields)
        self.parked_memory[v.text if v else tr.voted_text] = (tr.event_id, tr.last, t)

    def _finish(self, tr: Track) -> None:
        if tr.state is State.PARKED:
            if tr.event_id is not None:
                self.sink.update_event(tr.event_id, last_seen=tr.last_seen)
                self.parked_memory[tr.voted_text] = (tr.event_id, tr.last, tr.last_seen)
            return

        direction = classify_direction(tr.segment, self.dcfg)
        if tr.event_id is not None:
            # Was parked, then moved and disappeared: a departure.
            if direction == UNKNOWN:
                self.sink.update_event(tr.event_id, last_seen=tr.last_seen)
                self.parked_memory[tr.voted_text] = (tr.event_id, tr.last, tr.last_seen)
                return
            start = tr.segment[0].t
            self.sink.mark_departed(tr.event_id, start)
            self.parked_memory.pop(tr.voted_text, None)
            self._emit(tr, direction, parked=False, first_seen=start, last_seen=tr.last_seen)
            return

        self._emit(tr, direction, parked=False, first_seen=tr.created, last_seen=tr.last_seen)

    def _emit(self, tr: Track, direction: str, parked: bool, first_seen: float, last_seen: float) -> int | None:
        v = vote(tr.reads, self.rules)
        if v is None or v.reads < self.tcfg.min_reads:
            log.debug("Track %d dropped (%d reads)", tr.id, len(tr.reads))
            return None

        if not parked:
            for plate, prev_dir, ev_id, seen in self.recent:
                if (prev_dir == direction and first_seen - seen <= self.tcfg.dedupe_s
                        and similarity(plate, v.text) >= 0.85):
                    self.sink.update_event(ev_id, last_seen=last_seen)
                    log.info("Merged duplicate %s %s into event %d", v.text, direction, ev_id)
                    return ev_id

        region = max(set(tr.regions), key=tr.regions.count) if tr.regions else None
        data = EventData(
            plate=v.text,
            confidence=v.confidence,
            reads=v.reads,
            alternatives=v.alternatives,
            valid_format=self.rules.is_valid(v.text),
            region=region,
            direction=direction,
            parked=parked,
            first_seen=first_seen,
            last_seen=last_seen,
            frame=tr.best_frame,
            bbox=tr.best_bbox,
            track_id=tr.id,
        )
        ev_id = self.sink.create_event(data)
        log.info("Event %s: %s %s%s (%d reads, conf %.2f)", ev_id, v.text, direction,
                 " parked" if parked else "", v.reads, v.confidence)
        self.recent.append((v.text, direction, ev_id, last_seen))
        if direction == OUT:
            # A car driving out without us having seen it park: close its open stay.
            self.sink.mark_departed_plate(v.text, first_seen)
        return ev_id
