"""Command line entry point.

    python -m driveway_lpr run   -c config.yaml        # camera + dashboard
    python -m driveway_lpr web   -c config.yaml        # dashboard only
    python -m driveway_lpr image foto.jpg [-c ...]     # test recognition on a still image
    python -m driveway_lpr demo  -c config.yaml        # fill the database with fake data
    python -m driveway_lpr lookup AB12345              # test the motor register lookup
"""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import threading
import time
from pathlib import Path

from .config import Config, load_config
from .storage import Storage

log = logging.getLogger("driveway_lpr")


def _serve(cfg: Config, storage: Storage, pipeline=None, register=None) -> None:
    import uvicorn

    from .web.app import create_app

    app = create_app(cfg, storage, pipeline, register)
    log.info("Dashboard: http://%s:%d", "localhost" if cfg.web.host == "0.0.0.0" else cfg.web.host, cfg.web.port)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level="warning")


def cmd_run(cfg: Config, args: argparse.Namespace) -> None:
    from .pipeline import Pipeline

    storage = Storage(cfg.db_path, cfg.timezone)
    pipeline = Pipeline(cfg, storage)
    stop = threading.Event()

    def worker() -> None:
        try:
            pipeline.run(stop)
        except Exception:  # noqa: BLE001
            log.exception("Pipeline crashed")
            # Let systemd / docker restart the whole service.
            signal.raise_signal(signal.SIGTERM)

    thread = threading.Thread(target=worker, name="pipeline", daemon=True)
    thread.start()
    try:
        if args.no_web:
            while thread.is_alive():
                thread.join(1)
        else:
            _serve(cfg, storage, pipeline)
    finally:
        stop.set()
        thread.join(timeout=10)
        storage.close()


def _register(cfg: Config, storage: Storage):
    from .plates import PlateRules
    from .register import build_register

    rules = PlateRules(cfg.detection.plate_patterns, cfg.detection.correction_templates)
    return build_register(cfg.lookup, storage, rules)


def cmd_web(cfg: Config, args: argparse.Namespace) -> None:
    storage = Storage(cfg.db_path, cfg.timezone)
    register = _register(cfg, storage)
    if register is not None:
        register.start()
    _serve(cfg, storage, register=register)


def cmd_lookup(cfg: Config, args: argparse.Namespace) -> None:
    """Test the register lookup, or check older events that were never looked up."""
    from .plates import format_plate, normalize
    from .register import RegisterError

    storage = Storage(cfg.db_path, cfg.timezone)
    register = _register(cfg, storage)
    if register is None:
        print("Sæt lookup.provider i config.yaml (motorregister eller motorapi) først.")
        return
    for plate in args.plates:
        try:
            info = register.lookup(normalize(plate), force=True)
        except RegisterError as exc:
            print(f"{format_plate(normalize(plate))}: FEJL - {exc}")
            continue
        if not info.found:
            print(f"{format_plate(info.plate)}: findes ikke i registret")
            continue
        print(f"{format_plate(info.plate)}: {info.description()}")
        for key in ("kind", "use", "fuel", "status", "first_registration"):
            if getattr(info, key):
                print(f"  {key:20s} {getattr(info, key)}")
        if args.raw:
            for key, value in info.raw.items():
                print(f"  - {key}: {value}")
    if args.backfill_days:
        ids = storage.events_without_register(time.time() - args.backfill_days * 86400)
        print(f"Slår {len(ids)} hændelser op (maks. {cfg.lookup.max_per_hour} opslag i timen) ...")
        for event_id in ids:
            try:
                status = register.process_event(event_id)
            except RegisterError as exc:
                print(f"  hændelse {event_id}: {exc} - stopper")
                break
            print(f"  hændelse {event_id}: {status}")


def cmd_image(cfg: Config, args: argparse.Namespace) -> None:
    import cv2

    from .plates import PlateRules, format_plate
    from .recognizer import build_recognizer

    rules = PlateRules(cfg.detection.plate_patterns, cfg.detection.correction_templates)
    rec = build_recognizer(cfg)
    for path in args.images:
        img = cv2.imread(path)
        if img is None:
            print(f"{path}: kunne ikke læse billedet")
            continue
        t0 = time.perf_counter()
        dets = rec.recognize(img)
        ms = (time.perf_counter() - t0) * 1000
        print(f"{path}: {len(dets)} plade(r) på {ms:.0f} ms")
        for d in dets:
            clean = rules.clean(d.text)
            print(f"  {format_plate(clean):12s} rå={d.text!r:12s} ocr={d.confidence:.2f} "
                  f"detektor={d.det_confidence:.2f} gyldig={rules.is_valid(clean)} bbox={d.bbox}")
            cv2.rectangle(img, d.bbox[:2], d.bbox[2:], (0, 220, 0), 2)
            cv2.putText(img, format_plate(clean), (d.bbox[0], max(20, d.bbox[1] - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 0), 2)
        if args.out:
            out = Path(args.out) / (Path(path).stem + "_resultat.jpg")
            out.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out), img)
            print(f"  -> {out}")


def cmd_demo(cfg: Config, args: argparse.Namespace) -> None:
    """Generate realistic-looking fake history so the dashboard can be tried out."""
    storage = Storage(cfg.db_path, cfg.timezone)
    rng = random.Random(42)
    now = time.time()
    letters = "ABCDEFGHJKLMNPRSTUVXYZ"

    def plate() -> str:
        return rng.choice(letters) + rng.choice(letters) + f"{rng.randint(10000, 99999)}"

    residents = [(plate(), "Familiebilen", "beboer"), (plate(), "Firmabil", "beboer")]
    regulars = [(plate(), "Mormor", "familie"), (plate(), "Postnord", "levering"), (plate(), "Nabo Jens", "kendt")]
    for p, label, cat in residents + regulars:
        storage.upsert_vehicle(p, label=label, category=cat)

    def trip(p: str, t_in: float, stay: float) -> None:
        ev = storage.add_event(plate=p, first_seen=t_in, last_seen=t_in + stay, direction="in", parked=True,
                               confidence=rng.uniform(0.8, 0.99), reads=rng.randint(4, 30))
        if t_in + stay < now:
            storage.mark_departed(ev, t_in + stay)
            storage.add_event(plate=p, first_seen=t_in + stay, last_seen=t_in + stay + 8, direction="out",
                              confidence=rng.uniform(0.8, 0.99), reads=rng.randint(3, 15))

    day0 = now - args.days * 86400
    count = 0
    for d in range(args.days + 1):
        base = day0 + d * 86400
        base -= (base + 3600) % 86400  # local-ish midnight
        weekday = time.localtime(base).tm_wday
        for p, _, _ in residents:
            if weekday < 5 and rng.random() < 0.9:
                leave = base + rng.gauss(7.6, 0.4) * 3600
                back = base + rng.gauss(16.8, 0.7) * 3600
                storage.add_event(plate=p, first_seen=leave, last_seen=leave + 6, direction="out",
                                  confidence=0.95, reads=12)
                trip(p, back, rng.uniform(1, 3) * 3600)
            elif rng.random() < 0.6:
                trip(p, base + rng.uniform(10, 18) * 3600, rng.uniform(0.5, 4) * 3600)
            count += 3
        if rng.random() < 0.3:
            trip(regulars[0][0], base + rng.uniform(13, 19) * 3600, rng.uniform(1, 4) * 3600)
        if weekday < 5 and rng.random() < 0.6:
            trip(regulars[1][0], base + rng.uniform(9, 15) * 3600, rng.uniform(60, 240))
        for _ in range(rng.randint(0, 3)):
            t = base + rng.uniform(8, 21) * 3600
            if rng.random() < 0.5:
                trip(plate(), t, rng.uniform(300, 7200))
            else:
                storage.add_event(plate=plate(), first_seen=t, last_seen=t + 4, direction="through",
                                  confidence=rng.uniform(0.6, 0.95), reads=rng.randint(2, 6))
    # Fake motor register data, so the dashboard shows makes and models.
    cars = [("Toyota", "Yaris", "Hvid"), ("Volkswagen", "Golf", "Grå"), ("Skoda", "Octavia", "Sort"),
            ("Tesla", "Model Y", "Hvid"), ("Peugeot", "208", "Blå"), ("Kia", "Niro", "Sølv"),
            ("Volvo", "XC60", "Sort"), ("Ford", "Transit Custom", "Hvid"), ("Toyota", "Corolla", "Rød")]
    rows, _ = storage.list_events(limit=100000)
    plates = sorted({r["plate"] for r in rows})
    for i, p in enumerate(plates):
        make, model, color = cars[i % len(cars)]
        info = {"make": make, "model": model, "color": color, "model_year": 2012 + i % 12, "source": "demo",
                "description": f"{make} {model} · {color.lower()} · {2012 + i % 12}"}
        storage.put_register(p, True, "demo", info)
    for r in rows[:40]:
        storage.update_event(r["id"], register_status="found")
    if len(rows) > 6:
        storage.update_event(rows[3]["id"], register_status="not_found")
        storage.update_event(rows[5]["id"], register_status="corrected", original_plate=rows[5]["plate"][:-1] + "8")

    # someone is home right now
    storage.add_event(plate=residents[0][0], first_seen=now - 5400, last_seen=now, direction="in", parked=True,
                      confidence=0.97, reads=40)
    total = storage.list_events(limit=1)[1]
    print(f"Demo-data skrevet til {cfg.db_path} ({total} hændelser)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="driveway_lpr", description="Nummerpladegenkendelse til indkørslen")
    parser.add_argument("-c", "--config", default=None, help="sti til config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="kør kamera-analyse + dashboard")
    p_run.add_argument("--no-web", action="store_true", help="kun analyse, intet dashboard")
    sub.add_parser("web", help="kun dashboard")
    p_img = sub.add_parser("image", help="test genkendelse på billeder")
    p_img.add_argument("images", nargs="+")
    p_img.add_argument("--out", default=None, help="mappe til billeder med markeringer")
    p_look = sub.add_parser("lookup", help="slå plader op i Motorregistret")
    p_look.add_argument("plates", nargs="*")
    p_look.add_argument("--raw", action="store_true", help="vis alle felter fra registret")
    p_look.add_argument("--backfill-days", type=int, default=0,
                        help="slå også hændelser fra de sidste N dage op, som ikke er tjekket")
    p_demo = sub.add_parser("demo", help="fyld databasen med demo-data")
    p_demo.add_argument("--days", type=int, default=60)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("open_image_models", "fast_plate_ocr", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    config_path = args.config
    if config_path is None and Path("config.yaml").exists():
        config_path = "config.yaml"
    cfg = load_config(config_path)

    {"run": cmd_run, "web": cmd_web, "image": cmd_image, "demo": cmd_demo, "lookup": cmd_lookup}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
