import itertools

from driveway_lpr.config import DirectionConfig, TrackingConfig
from driveway_lpr.plates import PlateRules
from driveway_lpr.recognizer import PlateDetection
from driveway_lpr.tracker import IN, OUT, THROUGH, Obs, Tracker, classify_direction

W, H = 1920, 1080
RULES = PlateRules([r"^[A-Z]{2}\d{5}$", r"^[A-Z0-9]{2,7}$"], ["LLDDDDD"])


class FakeSink:
    def __init__(self):
        self.ids = itertools.count(1)
        self.events = {}
        self.departed = {}

    def create_event(self, data):
        i = next(self.ids)
        self.events[i] = {"plate": data.plate, "direction": data.direction, "parked": data.parked,
                          "first_seen": data.first_seen, "last_seen": data.last_seen}
        return i

    def update_event(self, event_id, **fields):
        self.events[event_id].update(fields)

    def mark_departed(self, event_id, ts):
        self.departed[event_id] = ts

    def mark_departed_plate(self, plate, ts):
        pass


def det(text, cx, cy, w, conf=0.9):
    h = w * 0.22
    return PlateDetection(text=text, confidence=conf,
                          bbox=(int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2)))


def make(**dir_overrides):
    sink = FakeSink()
    d = DirectionConfig(parked_after_s=20, **dir_overrides)
    t = TrackingConfig(lost_after_s=3, parked_lost_after_s=60, min_reads=2, dedupe_s=60)
    return sink, Tracker(sink, RULES, d, t, min_ocr_conf=0.5)


def feed(tracker, t0, steps, fps=5):
    """steps: list of detection lists, one per frame."""
    t = t0
    for dets in steps:
        tracker.update(dets, t, None, (W, H))
        t += 1 / fps
    return t


def test_classify_size():
    cfg = DirectionConfig()
    grow = [Obs(i, 0.5, 0.5, 0.05 + i * 0.01, (0, 0, 1, 1)) for i in range(10)]
    assert classify_direction(grow, cfg) == IN
    assert classify_direction(list(reversed(grow)), cfg) == OUT
    side = [Obs(i, 0.1 + i * 0.08, 0.5, 0.06, (0, 0, 1, 1)) for i in range(10)]
    assert classify_direction(side, cfg) == THROUGH
    cfg_rev = DirectionConfig(approaching_is_in=False)
    assert classify_direction(grow, cfg_rev) == OUT


def test_classify_vector():
    cfg = DirectionConfig(method="vector", in_vector=[0, 1])
    down = [Obs(i, 0.5, 0.2 + i * 0.05, 0.06, (0, 0, 1, 1)) for i in range(10)]
    assert classify_direction(down, cfg) == IN
    assert classify_direction(list(reversed(down)), cfg) == OUT


def test_car_drives_in_and_parks_then_leaves():
    sink, tr = make()
    # Approaching: plate grows from 100 to 300 px over 3 s.
    steps = [[det("AB12345", 960, 500 + i * 10, 100 + i * 14)] for i in range(15)]
    t = feed(tr, 0.0, steps)
    # Standing still for 25 s (idle checks every frame here).
    t = feed(tr, t, [[det("AB12345", 960, 640, 310)]] * 125)
    assert len(sink.events) == 1
    ev = sink.events[1]
    assert ev["plate"] == "AB12345" and ev["direction"] == IN and ev["parked"] is True

    # Later it reverses out: plate shrinks, then vanishes.
    steps = [[det("AB12345", 960, 640 - i * 10, 310 - i * 14)] for i in range(15)]
    t = feed(tr, t + 1, steps)
    feed(tr, t, [[]] * 30)  # 6 s with nothing -> lost
    assert len(sink.events) == 2
    assert sink.events[2]["direction"] == OUT
    assert 1 in sink.departed


def test_car_passes_through():
    sink, tr = make()
    steps = [[det("CD54321", 200 + i * 100, 300, 120)] for i in range(15)]
    t = feed(tr, 0.0, steps)
    feed(tr, t, [[]] * 20)
    assert [e["direction"] for e in sink.events.values()] == [THROUGH]


def test_single_misread_is_ignored():
    sink, tr = make()
    t = feed(tr, 0.0, [[det("ZZ99999", 500, 500, 100)]])
    feed(tr, t, [[]] * 30)
    assert sink.events == {}


def test_noisy_ocr_is_voted():
    sink, tr = make()
    texts = ["AB12345", "A812345", "AB12345", "AB1234S", "AB12345", "AB12845"]
    steps = [[det(texts[i % len(texts)], 960, 500 + i * 10, 100 + i * 14)] for i in range(15)]
    t = feed(tr, 0.0, steps)
    feed(tr, t, [[]] * 30)
    assert sink.events[1]["plate"] == "AB12345"


def test_duplicate_passes_are_merged():
    sink, tr = make()
    for start in (0.0, 10.0):  # same car seen twice within dedupe window
        steps = [[det("AB12345", 960, 500 + i * 10, 100 + i * 14)] for i in range(10)]
        t = feed(tr, start, steps)
        feed(tr, t, [[]] * 20)
    assert len(sink.events) == 1


def test_two_cars_tracked_separately():
    sink, tr = make()
    steps = [[det("AB12345", 400, 500 + i * 10, 100 + i * 14), det("XY98765", 1500, 300, 120 - i * 3)]
             for i in range(15)]
    t = feed(tr, 0.0, steps)
    feed(tr, t, [[]] * 30)
    by_plate = {e["plate"]: e["direction"] for e in sink.events.values()}
    assert by_plate == {"AB12345": IN, "XY98765": OUT}


def test_parked_car_reattached_after_track_loss():
    sink, tr = make()
    t = feed(tr, 0.0, [[det("AB12345", 960, 500 + i * 10, 100 + i * 14)] for i in range(15)])
    t = feed(tr, t, [[det("AB12345", 960, 640, 310)]] * 125)
    t = feed(tr, t, [[]] * 400)  # 80 s without detections -> parked track lost
    assert tr.tracks == []
    # Car is seen again at the same spot, standing still.
    feed(tr, t, [[det("AB12345", 962, 641, 309)]] * 125)
    assert len(sink.events) == 1
