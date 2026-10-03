"""Cross-check plates against the Danish motor register (DMR).

Two providers:

* ``motorregister`` - the official, free public lookup at motorregister.skat.dk
  (the same source nummerplade.net and similar sites build on). It is a web
  form, not an API, so lookups take a few seconds and may break if Skat
  changes the page. Labels are matched by text rather than page layout to make
  that less likely.
* ``motorapi`` - motorapi.dk, a JSON API (API key, 100 free lookups per day).

Lookups run in a background thread, are cached in SQLite and rate limited.
The register only holds vehicle data (make, model, colour, ...), never the
owner - that is not public in Denmark.

When a read plate does not exist in the register, the service tries the
runner-up reads from the tracker's vote; if exactly one of them exists, the
event is corrected to that plate.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Protocol

from .config import LookupConfig
from .plates import PlateRules

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) driveway-lpr/0.1"


class RegisterError(Exception):
    """The lookup could not be done (network, site changed, quota)."""


class RateLimited(RegisterError):
    pass


@dataclass
class VehicleInfo:
    plate: str
    found: bool
    source: str = ""
    make: str | None = None
    model: str | None = None
    variant: str | None = None
    color: str | None = None
    model_year: int | None = None
    fuel: str | None = None
    kind: str | None = None  # "Personbil", "Varebil", ...
    use: str | None = None
    status: str | None = None  # "Registreret", "Afmeldt", ...
    first_registration: str | None = None
    vin: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def description(self) -> str:
        name = " ".join(p for p in (self.make, self.model, self.variant) if p)
        extra = [p for p in (self.color.lower() if self.color else None,
                             str(self.model_year) if self.model_year else None) if p]
        return " · ".join([name, *extra]) if name else ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["description"] = self.description()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VehicleInfo":
        known = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


def _pretty(name: str | None) -> str | None:
    """'SKODA' -> 'Skoda', 'MERCEDES-BENZ' -> 'Mercedes-Benz', 'BMW' stays."""
    if not name:
        return None
    name = name.strip()
    if name.isupper() and len(name) > 3:
        return name.title()
    return name


def _year(value: Any) -> int | None:
    m = re.search(r"(19|20)\d\d", str(value or ""))
    return int(m.group(0)) if m else None


class Provider(Protocol):
    name: str

    def lookup(self, plate: str) -> VehicleInfo: ...


# ---------------------------------------------------------------- motorapi


class MotorApiProvider:
    name = "motorapi"

    def __init__(self, api_key: str, api_url: str, header: str, timeout_s: float):
        if not api_key:
            raise ValueError("lookup.api_key skal sættes for provider: motorapi")
        self.api_key, self.api_url, self.header, self.timeout_s = api_key, api_url.rstrip("/"), header, timeout_s

    def lookup(self, plate: str) -> VehicleInfo:
        url = f"{self.api_url}/vehicles/{urllib.parse.quote(plate)}"
        req = urllib.request.Request(url, headers={self.header: self.api_key, "Accept": "application/json",
                                                   "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return VehicleInfo(plate=plate, found=False, source=self.name)
            if exc.code == 429:
                raise RateLimited("MotorAPI: dagens kvote er brugt") from exc
            raise RegisterError(f"MotorAPI svarede {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RegisterError(f"MotorAPI: {exc}") from exc
        if isinstance(data, list):
            data = data[0] if data else {}
        if not data:
            return VehicleInfo(plate=plate, found=False, source=self.name)
        return self.parse(plate, data)

    def parse(self, plate: str, d: dict[str, Any]) -> VehicleInfo:
        def first(*keys: str) -> Any:
            for k in keys:
                if d.get(k) not in (None, ""):
                    return d[k]
            return None

        return VehicleInfo(
            plate=plate, found=True, source=self.name,
            make=_pretty(first("make", "brand")),
            model=_pretty(first("model")),
            variant=first("variant"),
            color=first("color", "colour"),
            model_year=_year(first("model_year", "modelYear")),
            fuel=first("fuel_type", "fuel", "propellant"),
            kind=first("type", "kind", "vehicle_type"),
            use=first("use", "usage"),
            status=first("status"),
            first_registration=first("first_registration", "firstRegistration"),
            vin=first("vin", "chassis_number"),
            raw=d,
        )


# ----------------------------------------------------------- motorregister


class _TextAndForms(HTMLParser):
    """Collects visible text nodes and the DMR search form token."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self.token: str | None = None
        self.token_action: str | None = None
        self._form_action: str | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "form":
            self._form_action = a.get("action")
        elif tag == "input" and a.get("name") == "dmrFormToken" and self.token is None:
            self.token, self.token_action = a.get("value"), self._form_action

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            text = " ".join(data.split())
            if text:
                self.texts.append(text)


def parse_labels(texts: list[str]) -> dict[str, str]:
    """Turn ["Farve:", "Sølv", "Modelår:", "2017", ...] into {"farve": "Sølv", ...}."""
    pairs: dict[str, str] = {}
    for i, text in enumerate(texts[:-1]):
        if text.endswith(":") and len(text) < 60:
            value = texts[i + 1]
            if value.endswith(":"):
                continue
            key = text[:-1].strip().lower()
            pairs.setdefault(key, value)
    return pairs


def _pick(pairs: dict[str, str], *prefixes: str) -> str | None:
    for prefix in prefixes:
        for key, value in pairs.items():
            if key.startswith(prefix):
                if value.startswith("-") or value.lower() in ("ukendt", "n/a"):
                    return None
                return value
    return None


class MotorregisterProvider:
    name = "motorregister"
    BASE = "https://motorregister.skat.dk"
    START = BASE + "/dmr-kerne/koeretoejdetaljer/visKoeretoej"

    def __init__(self, timeout_s: float):
        self.timeout_s = timeout_s

    def _open(self, opener, url: str, data: dict[str, str] | None = None, referer: str | None = None) -> tuple[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "da-DK,da;q=0.9"}
        if referer:
            headers["Referer"] = referer
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers)
        with opener.open(req, timeout=self.timeout_s) as resp:
            return resp.read().decode("utf-8", errors="replace"), resp.geturl()

    def lookup(self, plate: str) -> VehicleInfo:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            html, _ = self._open(opener, self.START)
            form = _TextAndForms()
            form.feed(html)
            if not form.token or not form.token_action:
                raise RegisterError("Motorregistret: kunne ikke finde søgeformularen (siden er måske ændret)")
            action = urllib.parse.urljoin(self.BASE, form.token_action)
            html, page_url = self._open(opener, action, {
                "dmrFormToken": form.token,
                "soegeord": plate,
                "soegekriterie": "REGISTRERINGSNUMMER",
                form.token_action: "Søg",
            }, referer=action)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RegisterError(f"Motorregistret: {exc}") from exc

        if "Ingen køretøjer fundet" in html:
            return VehicleInfo(plate=plate, found=False, source=self.name)

        page = _TextAndForms()
        page.feed(html)
        pairs = parse_labels(page.texts)
        if not pairs:
            raise RegisterError("Motorregistret: uventet svar (siden er måske ændret)")

        # Page 2 ("Tekniske oplysninger") holds the fuel type. Best effort only.
        try:
            html2, _ = self._open(opener, page_url + "&_eventId=customPage&_pageIndex=1", referer=page_url)
            tech = _TextAndForms()
            tech.feed(html2)
            for k, v in parse_labels(tech.texts).items():
                pairs.setdefault(k, v)
        except Exception:  # noqa: BLE001
            log.debug("Could not fetch technical page for %s", plate)
        return self.parse(plate, pairs)

    def parse(self, plate: str, pairs: dict[str, str]) -> VehicleInfo:
        mmv = _pick(pairs, "mærke, model, variant", "mærke")
        make = model = variant = None
        if mmv:
            parts = [p.strip() for p in mmv.split(",", 2)]
            make, model, variant = (parts + [None, None, None])[:3]
        return VehicleInfo(
            plate=plate, found=True, source=self.name,
            make=_pretty(make), model=_pretty(model), variant=variant or None,
            color=_pick(pairs, "farve"),
            model_year=_year(_pick(pairs, "modelår", "model år", "model-år")),
            fuel=_pick(pairs, "drivkraft", "drivmiddel"),
            kind=_pick(pairs, "art"),
            use=_pick(pairs, "anvendelse"),
            status=_pick(pairs, "status", "registreringsstatus"),
            first_registration=_pick(pairs, "første registrering"),
            vin=_pick(pairs, "stelnummer", "stelnr"),
            raw=pairs,
        )


# ------------------------------------------------------------------ service


class RegisterService:
    def __init__(self, cfg: LookupConfig, storage, provider: Provider, rules: PlateRules,
                 on_done: Callable[[int], None] | None = None):
        self.cfg = cfg
        self.storage = storage
        self.provider = provider
        self.rules = rules
        self.on_done = on_done
        self._queue: queue.Queue[int] = queue.Queue()
        self._retry: list[tuple[float, int, int]] = []  # (due, event_id, attempt)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error = ""

    # ---------------------------------------------------------------- public

    def start(self) -> "RegisterService":
        self._thread = threading.Thread(target=self._run, name="register", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def submit(self, event_id: int) -> None:
        self._queue.put(event_id)

    def lookup(self, plate: str, force: bool = False, wait: bool = False) -> VehicleInfo:
        """Cached lookup. Raises RegisterError / RateLimited."""
        if not force:
            cached = self.storage.get_register(plate)
            if cached is not None and not cached.get("error"):
                max_age = (self.cfg.cache_days if cached["found"] else self.cfg.not_found_cache_days) * 86400
                if time.time() - cached["fetched_at"] < max_age:
                    return VehicleInfo.from_dict({**cached["data"], "plate": plate, "found": cached["found"]})
        self._take_slot(wait)
        try:
            info = self.provider.lookup(plate)
        except RegisterError as exc:
            self.last_error = str(exc)
            raise
        self.last_error = ""
        data = info.to_dict()
        data.pop("plate", None)
        data.pop("found", None)
        self.storage.put_register(plate, info.found, info.source, data)
        log.info("Register %s: %s", plate, info.description() if info.found else "findes ikke")
        return info

    # --------------------------------------------------------------- worker

    def _take_slot(self, wait: bool) -> None:
        while True:
            with self._lock:
                now = time.time()
                while self._calls and now - self._calls[0] > 3600:
                    self._calls.popleft()
                if len(self._calls) < self.cfg.max_per_hour:
                    self._calls.append(now)
                    return
                retry_in = 3600 - (now - self._calls[0]) + 1
            if not wait or self._stop.is_set():
                raise RateLimited(f"Maks. {self.cfg.max_per_hour} opslag i timen er nået")
            self._stop.wait(min(retry_in, 60))

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            due = [r for r in self._retry if r[0] <= now]
            self._retry = [r for r in self._retry if r[0] > now]
            for _, event_id, attempt in due:
                self._handle(event_id, attempt)
            try:
                event_id = self._queue.get(timeout=5)
            except queue.Empty:
                continue
            self._handle(event_id, 1)

    def _handle(self, event_id: int, attempt: int) -> None:
        try:
            self.process_event(event_id)
        except RateLimited as exc:
            log.warning("Register lookup postponed: %s", exc)
            self.storage.update_event(event_id, register_status="pending")
            self._retry.append((time.time() + 900, event_id, attempt))
            if self.on_done:
                self.on_done(event_id)  # notify now, without vehicle data
            return
        except RegisterError as exc:
            log.warning("Register lookup failed (%d/3): %s", attempt, exc)
            if attempt < 3:
                self._retry.append((time.time() + 120 * attempt, event_id, attempt + 1))
                if self.on_done:
                    self.on_done(event_id)
                return
            self.storage.update_event(event_id, register_status="error")
        except Exception:  # noqa: BLE001
            log.exception("Register lookup crashed for event %s", event_id)
            self.storage.update_event(event_id, register_status="error")
        if self.on_done:
            self.on_done(event_id)

    def process_event(self, event_id: int) -> str | None:
        ev = self.storage.get_event(event_id)
        if ev is None:
            return None
        plate = ev["plate"]
        if not self.rules.is_valid(plate):
            status = "skipped"
        elif self.lookup(plate, wait=True).found:
            status = "found"
        elif ev["corrected"] or not self.cfg.auto_correct:
            status = "not_found"
        else:
            candidates = [t for t, _ in ev["alternatives"] if t != plate and self.rules.is_strict(t)]
            hits = [c for c in candidates[: self.cfg.max_alternatives] if self.lookup(c, wait=True).found]
            if len(hits) == 1:
                log.info("Event %d: %s is not registered, corrected to %s", event_id, plate, hits[0])
                self.storage.update_event(event_id, plate=hits[0], original_plate=plate)
                status = "corrected"
            else:
                status = "not_found"
        self.storage.update_event(event_id, register_status=status)
        return status


def build_register(cfg: LookupConfig, storage, rules: PlateRules,
                   on_done: Callable[[int], None] | None = None) -> RegisterService | None:
    provider: Provider
    if cfg.provider in ("", "none", None):
        return None
    if cfg.provider == "motorapi":
        provider = MotorApiProvider(cfg.api_key, cfg.api_url, cfg.api_header, cfg.timeout_s)
    elif cfg.provider == "motorregister":
        provider = MotorregisterProvider(cfg.timeout_s)
    else:
        raise ValueError(f"Ukendt lookup.provider: {cfg.provider} (brug none, motorregister eller motorapi)")
    return RegisterService(cfg, storage, provider, rules, on_done)
