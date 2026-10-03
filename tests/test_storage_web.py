import time

import pytest
from fastapi.testclient import TestClient

from driveway_lpr.config import Config
from driveway_lpr.storage import Storage
from driveway_lpr.web.app import create_app


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.storage.data_dir = str(tmp_path / "data")
    return c


@pytest.fixture
def storage(cfg):
    s = Storage(cfg.db_path, cfg.timezone)
    yield s
    s.close()


def test_events_and_stats(storage):
    now = time.time()
    a = storage.add_event(plate="AB12345", first_seen=now - 3600, last_seen=now - 60, direction="in", parked=True)
    storage.add_event(plate="AB12345", first_seen=now - 7200, last_seen=now - 7190, direction="out")
    storage.add_event(plate="CD11111", first_seen=now - 100, last_seen=now - 95, direction="through")
    storage.upsert_vehicle("AB12345", label="Familiebilen", category="beboer")

    rows, total = storage.list_events()
    assert total == 3
    assert rows[0]["plate"] == "CD11111"
    assert storage.get_event(a)["vehicle_label"] == "Familiebilen"

    present = storage.present_now(now)
    assert [p["id"] for p in present] == [a]

    storage.mark_departed(a, now - 30)
    assert storage.present_now(now) == []
    assert storage.get_event(a)["dwell_s"] == 3570

    assert sum(sum(r) for r in storage.heatmap(7)) == 3
    assert sum(d["total"] for d in storage.daily(7)) == 3
    top = storage.top_plates(7)
    assert top[0]["plate"] == "AB12345" and top[0]["visits"] == 2
    prof = storage.plate_profile("AB12345")
    assert prof["visits"] == 2 and prof["vehicle"]["label"] == "Familiebilen"


def test_mark_departed_plate_closes_latest_open_arrival(storage):
    now = time.time()
    a = storage.add_event(plate="AB12345", first_seen=now - 500, last_seen=now - 400, direction="in")
    storage.mark_departed_plate("AB12345", now - 10)
    assert storage.get_event(a)["departed_at"] == pytest.approx(now - 10)


def test_rename_plate(storage):
    now = time.time()
    for i in range(3):
        storage.add_event(plate="A812345", first_seen=now - i, last_seen=now - i)
    assert storage.rename_plate("A812345", "AB12345") == 3
    assert storage.list_events(plate="AB12345")[1] == 3


def test_api(cfg, storage):
    now = time.time()
    ev = storage.add_event(plate="AB12345", first_seen=now - 60, last_seen=now - 50, direction="in")
    client = TestClient(create_app(cfg, storage))

    assert client.get("/").status_code == 200
    s = client.get("/api/summary").json()
    assert s["today"]["events"] >= 0 and s["total_events"] == 1

    events = client.get("/api/events").json()
    assert events["total"] == 1 and events["events"][0]["plate_display"] == "AB 12 345"

    r = client.patch(f"/api/events/{ev}", json={"plate": "ab 12 346", "direction": "out"})
    assert r.json()["plate"] == "AB12346" and r.json()["direction"] == "out" and r.json()["corrected"] == 1

    r = client.put("/api/vehicles/AB 12 346", json={"label": "Bilen", "category": "beboer", "notify": True})
    assert r.json()["plate"] == "AB12346"
    assert client.get("/api/vehicles").json()[0]["visits"] == 1
    assert client.get("/api/plates/AB12346").json()["visits"] == 1

    assert len(client.get("/api/stats/daily?days=7").json()) == 7
    assert len(client.get("/api/stats/hourly").json()) == 24
    assert len(client.get("/api/stats/heatmap").json()) == 7
    csv = client.get("/api/export.csv")
    assert "AB 12 346" in csv.content.decode("utf-8-sig")

    assert client.get("/api/live.jpg").status_code == 503
    assert client.delete(f"/api/events/{ev}").status_code == 200
    assert client.get(f"/api/events/{ev}").status_code == 404


def test_basic_auth(cfg, storage):
    cfg.web.username, cfg.web.password = "admin", "hemmelig"
    client = TestClient(create_app(cfg, storage))
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", auth=("admin", "forkert")).status_code == 401
    assert client.get("/api/summary", auth=("admin", "hemmelig")).status_code == 200


def test_config_env_override(tmp_path):
    from driveway_lpr.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text("camera:\n  source: rtsp://cam/stream\ndetection:\n  roi: [0.1, 0.2, 0.9, 1.0]\n")
    c = load_config(p, environ={"LPR_WEB__PORT": "9000", "LPR_DIRECTION__APPROACHING_IS_IN": "false"})
    assert c.camera.source == "rtsp://cam/stream"
    assert c.detection.roi == [0.1, 0.2, 0.9, 1.0]
    assert c.web.port == 9000 and c.direction.approaching_is_in is False
    p.write_text("camera:\n  bogus: 1\n")
    with pytest.raises(ValueError):
        load_config(p, environ={})
