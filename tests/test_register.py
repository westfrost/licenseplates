import io
import sqlite3
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient

from driveway_lpr import register as reg
from driveway_lpr.config import Config, LookupConfig
from driveway_lpr.pipeline import EventWriter
from driveway_lpr.plates import PlateRules
from driveway_lpr.register import (MotorApiProvider, MotorregisterProvider, RateLimited, RegisterService,
                                   VehicleInfo, _TextAndForms, parse_labels)
from driveway_lpr.storage import Storage
from driveway_lpr.tracker import EventData
from driveway_lpr.web.app import create_app

RULES = PlateRules([r"^[A-Z]{2}\d{5}$", r"^[A-Z0-9]{2,7}$"], ["LLDDDDD"])

SEARCH_PAGE = """<html><body><div><form action="/dmr-kerne/koeretoejdetaljer/visKoeretoej?execution=e1s1" method="post">
<input type="hidden" name="dmrFormToken" value="tok-123"><input name="soegeord"></form></div></body></html>"""

RESULT_PAGE = """<html><head><script>var x = "Farve:";</script></head><body>
<div class="col"><span class="key">Stelnummer:</span><span class="value">TMBZZZAAZHD123456</span></div>
<div class="col"><span class="key">Mærke, Model, Variant:</span><span class="value">SKODA, CITIGO, 1,0 MPI</span></div>
<div class="col"><span class="key">Art:</span><span class="value">Personbil</span></div>
<div><div><label>Farve:</label></div><div><span>Sølv</span></div></div>
<div><div><label>Modelår:</label></div><div><span>2017</span></div></div>
<div><span>Første registreringsdato:</span> <span>12-05-2017</span></div>
<div><span>Status:</span><span>Registreret</span></div>
</body></html>"""


class FakeProvider:
    name = "fake"

    def __init__(self, registered):
        self.registered = registered
        self.calls = []

    def lookup(self, plate):
        self.calls.append(plate)
        if plate in self.registered:
            make, model, color = self.registered[plate]
            return VehicleInfo(plate=plate, found=True, source=self.name, make=make, model=model, color=color,
                               model_year=2019)
        return VehicleInfo(plate=plate, found=False, source=self.name)


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "db.sqlite3")
    yield s
    s.close()


def service(storage, registered, **cfg):
    provider = FakeProvider(registered)
    return RegisterService(LookupConfig(provider="fake", **cfg), storage, provider, RULES), provider


# ------------------------------------------------------------------ parsing

def test_form_token_and_labels():
    page = _TextAndForms()
    page.feed(SEARCH_PAGE)
    assert page.token == "tok-123"
    assert page.token_action.endswith("execution=e1s1")

    res = _TextAndForms()
    res.feed(RESULT_PAGE)
    pairs = parse_labels(res.texts)
    assert pairs["farve"] == "Sølv"  # the script text must be ignored
    info = MotorregisterProvider(5).parse("AB12345", pairs)
    assert (info.make, info.model, info.variant) == ("Skoda", "Citigo", "1,0 MPI")
    assert info.color == "Sølv" and info.model_year == 2017 and info.kind == "Personbil"
    assert info.status == "Registreret" and info.vin == "TMBZZZAAZHD123456"
    assert info.description() == "Skoda Citigo 1,0 MPI · sølv · 2017"


def test_motorregister_flow(monkeypatch):
    calls = []

    class Resp(io.BytesIO):
        def __init__(self, body, url):
            super().__init__(body.encode())
            self.url = url

        def geturl(self):
            return self.url

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, req, timeout):
            calls.append((req.full_url, req.data))
            if req.data is None and "customPage" not in req.full_url:
                return Resp(SEARCH_PAGE, req.full_url)
            if req.data is not None:
                return Resp(RESULT_PAGE, req.full_url + "&x=1")
            return Resp("<span>Drivkraft:</span><span>Benzin</span>", req.full_url)

    monkeypatch.setattr(reg.urllib.request, "build_opener", lambda *a: Opener())
    info = MotorregisterProvider(5).lookup("AB12345")
    assert info.found and info.make == "Skoda" and info.fuel == "Benzin"
    post_body = calls[1][1].decode()
    assert "dmrFormToken=tok-123" in post_body and "soegeord=AB12345" in post_body


def test_motorregister_not_found(monkeypatch):
    class Opener:
        def open(self, req, timeout):
            body = SEARCH_PAGE if req.data is None else "<p>Ingen køretøjer fundet.</p>"
            r = io.BytesIO(body.encode())
            r.geturl = lambda: req.full_url
            r.__enter__ = lambda *a: r
            return type("R", (), {"__enter__": lambda self: r, "__exit__": lambda self, *a: False})()

    monkeypatch.setattr(reg.urllib.request, "build_opener", lambda *a: Opener())
    assert MotorregisterProvider(5).lookup("ZZ99999").found is False


def test_motorapi_parse_and_404(monkeypatch):
    p = MotorApiProvider("key", "https://api.example", "X-AUTH-TOKEN", 5)
    info = p.parse("AB12345", {"registration_number": "AB12345", "make": "VOLKSWAGEN", "model": "GOLF",
                               "variant": "1,4 TSI", "color": "Hvid", "model_year": 2016, "fuel_type": "Benzin"})
    assert info.description() == "Volkswagen Golf 1,4 TSI · hvid · 2016"

    def raise_404(req, timeout):
        assert req.get_header("X-auth-token") == "key"
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    monkeypatch.setattr(reg.urllib.request, "urlopen", raise_404)
    assert p.lookup("ZZ99999").found is False


# ------------------------------------------------------------------ service

def test_lookup_is_cached(storage):
    svc, provider = service(storage, {"AB12345": ("Skoda", "Citigo", "Sølv")})
    assert svc.lookup("AB12345").found
    assert svc.lookup("AB12345").make == "Skoda"
    assert provider.calls == ["AB12345"]
    svc.lookup("AB12345", force=True)
    assert provider.calls == ["AB12345", "AB12345"]


def test_rate_limit(storage):
    svc, _ = service(storage, {}, max_per_hour=2)
    svc.lookup("AA11111")
    svc.lookup("AA22222")
    with pytest.raises(RateLimited):
        svc.lookup("AA33333")


def test_event_found(storage):
    svc, _ = service(storage, {"AB12345": ("Skoda", "Citigo", "Sølv")})
    ev = storage.add_event(plate="AB12345", first_seen=time.time(), last_seen=time.time())
    assert svc.process_event(ev) == "found"
    e = storage.get_event(ev)
    assert e["register"]["make"] == "Skoda" and e["register"]["description"].startswith("Skoda Citigo")


def test_misread_is_corrected_from_alternatives(storage):
    svc, provider = service(storage, {"AB12345": ("Skoda", "Citigo", "Sølv")})
    ev = storage.add_event(plate="AB12346", first_seen=time.time(), last_seen=time.time(),
                           alternatives=[("AB12345", 3), ("A812345", 1)])
    assert svc.process_event(ev) == "corrected"
    e = storage.get_event(ev)
    assert e["plate"] == "AB12345" and e["original_plate"] == "AB12346" and e["register"]["make"] == "Skoda"
    assert "A812345" not in provider.calls  # not a Danish-format candidate


def test_not_found_without_unique_alternative(storage):
    svc, _ = service(storage, {"AB12345": ("Skoda", "Citigo", "Sølv"), "AB12348": ("Kia", "Rio", "Rød")})
    ev = storage.add_event(plate="AB12346", first_seen=time.time(), last_seen=time.time(),
                           alternatives=[("AB12345", 3), ("AB12348", 2)])
    assert svc.process_event(ev) == "not_found"  # two candidates exist: don't guess
    assert storage.get_event(ev)["plate"] == "AB12346"


def test_tracker_cannot_undo_register_correction(tmp_path, storage):
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.storage.save_snapshots = False
    writer = EventWriter(cfg, storage)
    svc, _ = service(storage, {"AB12345": ("Skoda", "Citigo", "Sølv")})
    svc.on_done = writer.on_register_done
    writer.register = svc
    notified = []
    writer._notify = notified.append

    now = time.time()
    ev = writer.create_event(EventData(plate="AB12346", confidence=0.9, reads=5, alternatives=[("AB12345", 2)],
                                       valid_format=True, region=None, direction="in", parked=True,
                                       first_seen=now, last_seen=now, frame=None, bbox=None, track_id=1))
    assert notified == []  # waits for the lookup
    svc._handle(ev, 1)
    assert notified == [ev]
    writer.update_event(ev, plate="AB12346", last_seen=now + 30)  # tracker still believes its own vote
    e = storage.get_event(ev)
    assert e["plate"] == "AB12345" and e["last_seen"] == pytest.approx(now + 30)


def test_migration_adds_columns(tmp_path):
    db = tmp_path / "old.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, plate TEXT NOT NULL, confidence REAL DEFAULT 0, "
                "reads INTEGER DEFAULT 0, alternatives TEXT DEFAULT '[]', valid_format INTEGER DEFAULT 1, region TEXT, "
                "direction TEXT DEFAULT 'unknown', parked INTEGER DEFAULT 0, first_seen REAL NOT NULL, "
                "last_seen REAL NOT NULL, departed_at REAL, snapshot TEXT, crop TEXT, corrected INTEGER DEFAULT 0)")
    con.execute("INSERT INTO events (plate, first_seen, last_seen) VALUES ('AB12345', 1, 2)")
    con.commit()
    con.close()
    s = Storage(db)
    e = s.list_events()[0][0]
    assert e["register_status"] is None and e["register"] is None
    s.close()


def test_api_lookup_and_makes(tmp_path, storage):
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    client = TestClient(create_app(cfg, storage))
    assert client.post("/api/plates/AB12345/lookup").status_code == 503
    assert client.get("/api/status").json()["lookup_provider"] == "none"

    svc, _ = service(storage, {"AB12345": ("Skoda", "Citigo", "Sølv")})
    cfg.lookup.provider = "fake"
    client = TestClient(create_app(cfg, storage, register=svc))
    r = client.post("/api/plates/ab 12 345/lookup").json()
    assert r["found"] and r["description"] == "Skoda Citigo · sølv · 2019"
    storage.add_event(plate="AB12345", first_seen=time.time(), last_seen=time.time())
    assert client.get("/api/stats/makes").json() == [{"make": "Skoda", "cars": 1}]
    assert client.get("/api/plates/AB12345").json()["register"]["color"] == "Sølv"
