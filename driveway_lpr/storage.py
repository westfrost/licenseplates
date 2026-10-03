"""SQLite storage for events and known vehicles, plus statistics queries.

All timestamps are stored as unix seconds (UTC). Statistics are bucketed in
the configured local time zone in Python, which keeps DST handling correct.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plate         TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0,
    reads         INTEGER NOT NULL DEFAULT 0,
    alternatives  TEXT NOT NULL DEFAULT '[]',
    valid_format  INTEGER NOT NULL DEFAULT 1,
    region        TEXT,
    direction     TEXT NOT NULL DEFAULT 'unknown',  -- in / out / through / unknown
    parked        INTEGER NOT NULL DEFAULT 0,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    departed_at   REAL,                             -- set on the arrival when the car leaves
    snapshot      TEXT,
    crop          TEXT,
    corrected     INTEGER NOT NULL DEFAULT 0        -- plate edited by hand in the dashboard
);
CREATE INDEX IF NOT EXISTS idx_events_first_seen ON events(first_seen);
CREATE INDEX IF NOT EXISTS idx_events_plate ON events(plate);

CREATE TABLE IF NOT EXISTS vehicles (
    plate      TEXT PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',   -- e.g. "Mors Toyota"
    owner      TEXT NOT NULL DEFAULT '',
    category   TEXT NOT NULL DEFAULT 'kendt',  -- beboer / familie / gaest / haandvaerker / levering / blokeret / kendt
    notes      TEXT NOT NULL DEFAULT '',
    notify     INTEGER NOT NULL DEFAULT 0,
    created    REAL NOT NULL
);
"""

EVENT_FIELDS = {"plate", "confidence", "reads", "valid_format", "region", "direction",
                "parked", "last_seen", "departed_at", "snapshot", "crop", "corrected", "alternatives"}
VEHICLE_FIELDS = ("label", "owner", "category", "notes", "notify")


class Storage:
    def __init__(self, db_path: str | Path, timezone: str = "Europe/Copenhagen"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.tz = ZoneInfo(timezone)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ----------------------------------------------------------------- events

    def add_event(self, *, plate: str, first_seen: float, last_seen: float, direction: str = "unknown",
                  parked: bool = False, confidence: float = 0.0, reads: int = 0,
                  alternatives: list | None = None, valid_format: bool = True, region: str | None = None,
                  snapshot: str | None = None, crop: str | None = None) -> int:
        cur = self._exec(
            """INSERT INTO events (plate, confidence, reads, alternatives, valid_format, region,
                                   direction, parked, first_seen, last_seen, snapshot, crop)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plate, confidence, reads, json.dumps(alternatives or []), int(valid_format), region,
             direction, int(parked), first_seen, last_seen, snapshot, crop),
        )
        return int(cur.lastrowid)

    def update_event(self, event_id: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in EVENT_FIELDS}
        if not fields:
            return
        if "alternatives" in fields and not isinstance(fields["alternatives"], str):
            fields["alternatives"] = json.dumps(fields["alternatives"])
        for key in ("valid_format", "parked", "corrected"):
            if key in fields:
                fields[key] = int(bool(fields[key]))
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE events SET {cols} WHERE id = ?", (*fields.values(), event_id))

    def delete_event(self, event_id: int) -> dict[str, Any] | None:
        ev = self.get_event(event_id)
        if ev:
            self._exec("DELETE FROM events WHERE id = ?", (event_id,))
        return ev

    def mark_departed(self, event_id: int, ts: float) -> None:
        self._exec("UPDATE events SET departed_at = ? WHERE id = ? AND departed_at IS NULL", (ts, event_id))

    def mark_departed_plate(self, plate: str, ts: float, max_age_s: float = 7 * 86400) -> None:
        """Close the most recent open arrival of `plate` (car left without being tracked as parked)."""
        self._exec(
            """UPDATE events SET departed_at = ? WHERE id = (
                   SELECT id FROM events WHERE plate = ? AND direction = 'in' AND departed_at IS NULL
                   AND first_seen < ? AND first_seen > ? ORDER BY first_seen DESC LIMIT 1)""",
            (ts, plate, ts, ts - max_age_s),
        )

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        rows = self._all(self._event_select() + " WHERE e.id = ?", (event_id,))
        return self._decorate(rows)[0] if rows else None

    @staticmethod
    def _event_select() -> str:
        return """SELECT e.*, v.label AS vehicle_label, v.owner AS vehicle_owner, v.category AS vehicle_category
                  FROM events e LEFT JOIN vehicles v ON v.plate = e.plate"""

    @staticmethod
    def _decorate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for r in rows:
            r["alternatives"] = json.loads(r.get("alternatives") or "[]")
            r["parked"] = bool(r["parked"])
            r["valid_format"] = bool(r["valid_format"])
            r["known"] = r.get("vehicle_category") is not None
            stay_end = r.get("departed_at") or (r["last_seen"] if r["parked"] else None)
            r["dwell_s"] = round(stay_end - r["first_seen"]) if stay_end else None
        return rows

    def list_events(self, *, limit: int = 50, offset: int = 0, plate: str | None = None,
                    direction: str | None = None, since: float | None = None, until: float | None = None,
                    known: bool | None = None) -> tuple[list[dict[str, Any]], int]:
        where, params = [], []
        if plate:
            where.append("e.plate LIKE ?")
            params.append(f"%{plate.upper().replace(' ', '')}%")
        if direction:
            where.append("e.direction = ?")
            params.append(direction)
        if since is not None:
            where.append("e.first_seen >= ?")
            params.append(since)
        if until is not None:
            where.append("e.first_seen < ?")
            params.append(until)
        if known is True:
            where.append("v.plate IS NOT NULL")
        elif known is False:
            where.append("v.plate IS NULL")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM events e LEFT JOIN vehicles v ON v.plate = e.plate" + clause, params
            ).fetchone()[0]
        rows = self._all(self._event_select() + clause + " ORDER BY e.first_seen DESC LIMIT ? OFFSET ?",
                         [*params, limit, offset])
        return self._decorate(rows), int(total)

    def snapshot_paths_before(self, ts: float) -> list[tuple[int, str | None, str | None]]:
        with self._lock:
            return [tuple(r) for r in self._conn.execute(
                "SELECT id, snapshot, crop FROM events WHERE first_seen < ? AND (snapshot IS NOT NULL OR crop IS NOT NULL)",
                (ts,)).fetchall()]

    def clear_snapshots(self, ids: list[int]) -> None:
        with self._lock:
            self._conn.executemany("UPDATE events SET snapshot = NULL, crop = NULL WHERE id = ?", [(i,) for i in ids])
            self._conn.commit()

    # --------------------------------------------------------------- vehicles

    def list_vehicles(self) -> list[dict[str, Any]]:
        return self._all(
            """SELECT v.*, COUNT(e.id) AS visits, MAX(e.first_seen) AS last_visit
               FROM vehicles v LEFT JOIN events e ON e.plate = v.plate
               GROUP BY v.plate ORDER BY v.label COLLATE NOCASE, v.plate"""
        )

    def get_vehicle(self, plate: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM vehicles WHERE plate = ?", (plate,))
        return rows[0] if rows else None

    def upsert_vehicle(self, plate: str, **fields: Any) -> dict[str, Any]:
        values = {k: fields[k] for k in VEHICLE_FIELDS if k in fields and fields[k] is not None}
        if "notify" in values:
            values["notify"] = int(bool(values["notify"]))
        existing = self.get_vehicle(plate)
        if existing is None:
            cols = ["plate", "created", *values]
            self._exec(f"INSERT INTO vehicles ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       (plate, time.time(), *values.values()))
        elif values:
            self._exec(f"UPDATE vehicles SET {', '.join(f'{k} = ?' for k in values)} WHERE plate = ?",
                       (*values.values(), plate))
        return self.get_vehicle(plate)  # type: ignore[return-value]

    def delete_vehicle(self, plate: str) -> None:
        self._exec("DELETE FROM vehicles WHERE plate = ?", (plate,))

    def rename_plate(self, old: str, new: str) -> int:
        """Correct a misread plate on all its events."""
        cur = self._exec("UPDATE events SET plate = ?, corrected = 1 WHERE plate = ?", (new, old))
        return cur.rowcount

    # ------------------------------------------------------------- statistics

    def _local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.tz)

    def _day_start(self, days_back: int = 0, now: float | None = None) -> float:
        now_local = self._local(now if now is not None else time.time())
        start = (now_local - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp()

    def _events_since(self, since: float) -> list[dict[str, Any]]:
        return self._decorate(self._all(self._event_select() + " WHERE e.first_seen >= ? ORDER BY e.first_seen",
                                        (since,)))

    def present_now(self, now: float | None = None, stale_after_s: float = 600) -> list[dict[str, Any]]:
        """Cars that arrived, parked and have not been seen leaving."""
        now = now if now is not None else time.time()
        rows = self._all(
            self._event_select() + """ WHERE e.parked = 1 AND e.departed_at IS NULL AND e.last_seen >= ?
                                       ORDER BY e.first_seen DESC""",
            (now - stale_after_s,),
        )
        return self._decorate(rows)

    def summary(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        today = self._day_start(0, now)
        yesterday = self._day_start(1, now)
        week = self._day_start(6, now)
        rows = self._events_since(self._day_start(13, now))

        def window(a: float, b: float) -> list[dict[str, Any]]:
            return [r for r in rows if a <= r["first_seen"] < b]

        t_rows, y_rows, w_rows = window(today, now + 1), window(yesterday, today), window(week, now + 1)
        prev_week = window(self._day_start(13, now), week)
        all_plates_before = {r["plate"] for r in self._all(
            "SELECT DISTINCT plate FROM events WHERE first_seen < ?", (today,))}
        arrivals = [r for r in t_rows if r["direction"] == "in"]
        return {
            "today": {
                "events": len(t_rows),
                "arrivals": len(arrivals),
                "departures": sum(1 for r in t_rows if r["direction"] == "out"),
                "unique": len({r["plate"] for r in t_rows}),
                "new_plates": len({r["plate"] for r in t_rows} - all_plates_before),
                "unknown": len({r["plate"] for r in t_rows if not r["known"]}),
                "first_arrival": arrivals[0]["first_seen"] if arrivals else None,
            },
            "yesterday": {"events": len(y_rows), "unique": len({r["plate"] for r in y_rows})},
            "week": {"events": len(w_rows), "unique": len({r["plate"] for r in w_rows})},
            "prev_week": {"events": len(prev_week)},
            "present": self.present_now(now),
            "total_events": self._all("SELECT COUNT(*) AS n FROM events")[0]["n"],
            "total_plates": self._all("SELECT COUNT(DISTINCT plate) AS n FROM events")[0]["n"],
            "last_event": (self.list_events(limit=1)[0] or [None])[0],
        }

    def daily(self, days: int = 30, now: float | None = None) -> list[dict[str, Any]]:
        start = self._day_start(days - 1, now)
        buckets: dict[str, Counter] = {}
        for i in range(days):
            day = (self._local(start) + timedelta(days=i, hours=12)).date().isoformat()
            buckets[day] = Counter()
        for r in self._events_since(start):
            day = self._local(r["first_seen"]).date().isoformat()
            if day in buckets:
                buckets[day][r["direction"]] += 1
                buckets[day]["total"] += 1
        return [{"date": d, "in": c["in"], "out": c["out"], "through": c["through"],
                 "unknown": c["unknown"], "total": c["total"]} for d, c in buckets.items()]

    def hourly(self, days: int = 30, now: float | None = None) -> list[dict[str, Any]]:
        counts: dict[int, Counter] = defaultdict(Counter)
        for r in self._events_since(self._day_start(days - 1, now)):
            counts[self._local(r["first_seen"]).hour][r["direction"]] += 1
        return [{"hour": h, "in": counts[h]["in"], "out": counts[h]["out"], "through": counts[h]["through"],
                 "unknown": counts[h]["unknown"]} for h in range(24)]

    def heatmap(self, days: int = 90, now: float | None = None) -> list[list[int]]:
        """7 x 24 matrix: weekday (Monday = 0) x hour."""
        grid = [[0] * 24 for _ in range(7)]
        for r in self._events_since(self._day_start(days - 1, now)):
            dt = self._local(r["first_seen"])
            grid[dt.weekday()][dt.hour] += 1
        return grid

    def top_plates(self, days: int = 30, limit: int = 10, now: float | None = None) -> list[dict[str, Any]]:
        rows = self._events_since(self._day_start(days - 1, now))
        stats: dict[str, dict[str, Any]] = {}
        for r in rows:
            s = stats.setdefault(r["plate"], {"plate": r["plate"], "visits": 0, "arrivals": 0,
                                              "last_seen": 0.0, "label": r["vehicle_label"],
                                              "category": r["vehicle_category"], "dwell": []})
            s["visits"] += 1
            s["arrivals"] += r["direction"] == "in"
            s["last_seen"] = max(s["last_seen"], r["first_seen"])
            if r["dwell_s"]:
                s["dwell"].append(r["dwell_s"])
        out = sorted(stats.values(), key=lambda s: (-s["visits"], -s["last_seen"]))[:limit]
        for s in out:
            dw = s.pop("dwell")
            s["avg_dwell_s"] = round(sum(dw) / len(dw)) if dw else None
        return out

    def plate_profile(self, plate: str) -> dict[str, Any]:
        rows = self._decorate(self._all(self._event_select() + " WHERE e.plate = ? ORDER BY e.first_seen",
                                        (plate,)))
        hours = Counter(self._local(r["first_seen"]).hour for r in rows)
        weekdays = Counter(self._local(r["first_seen"]).weekday() for r in rows)
        arrivals = [self._local(r["first_seen"]) for r in rows if r["direction"] == "in"]
        avg_arrival = None
        if arrivals:
            mins = sum(a.hour * 60 + a.minute for a in arrivals) / len(arrivals)
            avg_arrival = f"{int(mins // 60):02d}:{int(mins % 60):02d}"
        dwell = [r["dwell_s"] for r in rows if r["dwell_s"]]
        return {
            "plate": plate,
            "vehicle": self.get_vehicle(plate),
            "visits": len(rows),
            "first_seen": rows[0]["first_seen"] if rows else None,
            "last_seen": rows[-1]["first_seen"] if rows else None,
            "avg_arrival": avg_arrival,
            "avg_dwell_s": round(sum(dwell) / len(dwell)) if dwell else None,
            "hours": [hours[h] for h in range(24)],
            "weekdays": [weekdays[d] for d in range(7)],
            "directions": dict(Counter(r["direction"] for r in rows)),
            "events": list(reversed(rows))[:100],
        }
