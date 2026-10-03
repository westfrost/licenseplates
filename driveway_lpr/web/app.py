"""FastAPI dashboard + JSON API."""

from __future__ import annotations

import asyncio
import csv
import io
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Config
from ..plates import format_plate, normalize
from ..register import RateLimited, RegisterError
from ..storage import Storage

HERE = Path(__file__).parent


class EventPatch(BaseModel):
    plate: str | None = None
    direction: str | None = None
    apply_to_all: bool = False  # rename this plate on every event


class VehicleIn(BaseModel):
    label: str = ""
    owner: str = ""
    category: str = "kendt"
    notes: str = ""
    notify: bool = False


def create_app(cfg: Config, storage: Storage, pipeline: Any | None = None, register: Any | None = None) -> FastAPI:
    if register is None and pipeline is not None:
        register = pipeline.register
    security = HTTPBasic(auto_error=False)

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not (cfg.web.username and cfg.web.password):
            return
        ok = creds is not None and secrets.compare_digest(creds.username, cfg.web.username) \
            and secrets.compare_digest(creds.password, cfg.web.password)
        if not ok:
            raise HTTPException(401, "Login påkrævet", headers={"WWW-Authenticate": "Basic"})

    app = FastAPI(title="Indkørsel - nummerplader", dependencies=[Depends(auth)], docs_url="/api/docs",
                  redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    cfg.snapshot_path.mkdir(parents=True, exist_ok=True)
    app.mount("/snapshots", StaticFiles(directory=cfg.snapshot_path), name="snapshots")
    tz = ZoneInfo(cfg.timezone)

    def with_display(ev: dict[str, Any] | None) -> dict[str, Any] | None:
        if ev is not None:
            ev["plate_display"] = format_plate(ev["plate"])
        return ev

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (HERE / "templates" / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        base = {"pipeline": pipeline is not None, "timezone": cfg.timezone, "now": time.time(),
                "lookup_provider": cfg.lookup.provider if register is not None else "none",
                "lookup_link": cfg.lookup.link_template}
        if pipeline is not None:
            base.update(pipeline.status())
        return base

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        s = storage.summary()
        s["present"] = [with_display(e) for e in s["present"]]
        with_display(s["last_event"])
        return s

    @app.get("/api/events")
    def events(limit: int = Query(50, le=500), offset: int = 0, plate: str | None = None,
               direction: str | None = None, days: int | None = None, known: bool | None = None) -> dict[str, Any]:
        since = time.time() - days * 86400 if days else None
        rows, total = storage.list_events(limit=limit, offset=offset, plate=plate, direction=direction,
                                          since=since, known=known)
        return {"total": total, "events": [with_display(r) for r in rows]}

    @app.get("/api/events/{event_id}")
    def event(event_id: int) -> dict[str, Any]:
        ev = storage.get_event(event_id)
        if ev is None:
            raise HTTPException(404, "Hændelse findes ikke")
        return with_display(ev)  # type: ignore[return-value]

    @app.patch("/api/events/{event_id}")
    def patch_event(event_id: int, body: EventPatch) -> dict[str, Any]:
        ev = storage.get_event(event_id)
        if ev is None:
            raise HTTPException(404, "Hændelse findes ikke")
        if body.plate:
            new = normalize(body.plate)
            if not new:
                raise HTTPException(400, "Ugyldig nummerplade")
            if body.apply_to_all:
                storage.rename_plate(ev["plate"], new)
            else:
                storage.update_event(event_id, plate=new, corrected=True)
            if new != ev["plate"]:
                storage.update_event(event_id, register_status=None, original_plate=None)
                if register is not None:
                    register.submit(event_id)
        if body.direction:
            if body.direction not in ("in", "out", "through", "unknown"):
                raise HTTPException(400, "Ugyldig retning")
            storage.update_event(event_id, direction=body.direction, corrected=True)
        return with_display(storage.get_event(event_id))  # type: ignore[return-value]

    @app.delete("/api/events/{event_id}")
    def delete_event(event_id: int) -> dict[str, Any]:
        ev = storage.delete_event(event_id)
        if ev is None:
            raise HTTPException(404, "Hændelse findes ikke")
        for rel in (ev["snapshot"], ev["crop"]):
            if rel:
                (cfg.snapshot_path / rel).unlink(missing_ok=True)
        return {"deleted": event_id}

    @app.get("/api/stats/daily")
    def daily(days: int = Query(30, ge=1, le=366)) -> list[dict[str, Any]]:
        return storage.daily(days)

    @app.get("/api/stats/hourly")
    def hourly(days: int = Query(30, ge=1, le=3650)) -> list[dict[str, Any]]:
        return storage.hourly(days)

    @app.get("/api/stats/heatmap")
    def heatmap(days: int = Query(90, ge=1, le=3650)) -> list[list[int]]:
        return storage.heatmap(days)

    @app.get("/api/stats/top")
    def top(days: int = Query(30, ge=1, le=3650), limit: int = Query(10, le=100)) -> list[dict[str, Any]]:
        rows = storage.top_plates(days, limit)
        for r in rows:
            r["plate_display"] = format_plate(r["plate"])
        return rows

    @app.get("/api/stats/makes")
    def makes(days: int = Query(90, ge=1, le=3650)) -> list[dict[str, Any]]:
        return storage.top_makes(days)

    @app.post("/api/plates/{plate}/lookup")
    async def lookup_plate(plate: str) -> dict[str, Any]:
        """Look the plate up in the motor register now (ignores the cache)."""
        if register is None:
            raise HTTPException(503, "Opslag i Motorregistret er ikke slået til (lookup.provider)")
        try:
            info = await asyncio.to_thread(register.lookup, normalize(plate), True)
        except RateLimited as exc:
            raise HTTPException(429, str(exc)) from exc
        except RegisterError as exc:
            raise HTTPException(502, str(exc)) from exc
        return info.to_dict()

    @app.get("/api/plates/{plate}")
    def plate(plate: str) -> dict[str, Any]:
        p = storage.plate_profile(normalize(plate))
        p["plate_display"] = format_plate(p["plate"])
        p["events"] = [with_display(e) for e in p["events"]]
        return p

    @app.get("/api/vehicles")
    def vehicles() -> list[dict[str, Any]]:
        rows = storage.list_vehicles()
        for r in rows:
            r["plate_display"] = format_plate(r["plate"])
        return rows

    @app.put("/api/vehicles/{plate}")
    def put_vehicle(plate: str, body: VehicleIn) -> dict[str, Any]:
        key = normalize(plate)
        if not key:
            raise HTTPException(400, "Ugyldig nummerplade")
        return storage.upsert_vehicle(key, **body.model_dump())

    @app.delete("/api/vehicles/{plate}")
    def delete_vehicle(plate: str) -> dict[str, str]:
        storage.delete_vehicle(normalize(plate))
        return {"deleted": plate}

    @app.get("/api/export.csv")
    def export_csv(days: int | None = None) -> Response:
        since = time.time() - days * 86400 if days else None
        rows, _ = storage.list_events(limit=1_000_000, since=since)
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        writer.writerow(["id", "tidspunkt", "nummerplade", "retning", "parkeret", "varighed_s", "sikkerhed",
                         "aflæsninger", "kendt_som", "kategori"])
        for r in rows:
            writer.writerow([r["id"], datetime.fromtimestamp(r["first_seen"], tz).isoformat(timespec="seconds"),
                             format_plate(r["plate"]), r["direction"], int(r["parked"]), r["dwell_s"] or "",
                             f"{r['confidence']:.2f}", r["reads"], r["vehicle_label"] or "",
                             r["vehicle_category"] or ""])
        return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=nummerplader.csv"})

    @app.get("/api/live.jpg")
    async def live_jpg() -> Response:
        if pipeline is None:
            raise HTTPException(503, "Kameraet kører ikke i denne proces")
        _, jpeg = await asyncio.to_thread(pipeline.live_frame, -1, 3.0)
        if jpeg is None:
            raise HTTPException(503, "Intet billede endnu")
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/live.mjpg")
    async def live_mjpg(request: Request) -> StreamingResponse:
        if pipeline is None:
            raise HTTPException(503, "Kameraet kører ikke i denne proces")

        async def frames():
            seq = -1
            while not await request.is_disconnected():
                seq, jpeg = await asyncio.to_thread(pipeline.live_frame, seq, 5.0)
                if jpeg is None:
                    continue
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    return app
