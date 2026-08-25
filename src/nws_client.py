"""
NOAA / National Weather Service client.

Authoritative settlement source for Polymarket US temperature contracts is the
NWS Daily Climate Report (CLI) issued by the local Weather Forecast Office
(docs.polymarket.us/faqs/weather-faqs, verified 2026-08-24).

We therefore read, in order of authority:
  1. CLI product text  -- the settlement source itself
  2. METAR observations at the exact station -- intraday max/min so far
  3. Gridpoint forecast at the station's coordinates -- forward-looking

We never substitute a citywide forecast for the station. Each contract's
station is pinned in config/stations.json.

api.weather.gov requires a descriptive User-Agent and is rate-limited; be polite.
"""
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional

BASE = "https://api.weather.gov"
UA = "polymarket-cowork-agent/1.0 (paper-trading research; contact via GitHub)"


class WeatherError(RuntimeError):
    pass


def _get(path: str, timeout: int = 25, retries: int = 3, raw: bool = False):
    url = path if path.startswith("http") else f"{BASE}{path}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/geo+json, application/json, text/plain"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return body if raw else json.loads(body)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
    raise WeatherError(f"GET {url} failed after {retries} attempts: {last}")


def latest_observation(station: str) -> Dict:
    """Most recent METAR observation for an exact station id (e.g. KNYC)."""
    return _get(f"/stations/{station}/observations/latest?require_qc=false")


def observations_since(station: str, start_iso: str) -> List[Dict]:
    data = _get(f"/stations/{station}/observations?start={start_iso}&limit=500")
    return data.get("features", []) or []


def c_to_f(c: Optional[float]) -> Optional[float]:
    return None if c is None else c * 9.0 / 5.0 + 32.0


def observed_extremes_f(station: str, start_iso: str) -> Dict:
    """Max/min temperature actually observed at the station since start_iso.

    This is the single most valuable input late in a trading day: once the
    day's high is already in the books, the outcome is partly determined and
    the market sometimes lags that fact.
    """
    feats = observations_since(station, start_iso)
    temps = []
    for f in feats:
        p = f.get("properties", {}) or {}
        t = (p.get("temperature") or {}).get("value")
        ts = p.get("timestamp")
        if t is not None:
            temps.append((c_to_f(t), ts))
    if not temps:
        return {"station": station, "count": 0, "max_f": None, "min_f": None,
                "latest_f": None, "latest_ts": None}
    mx = max(temps, key=lambda x: x[0])
    mn = min(temps, key=lambda x: x[0])
    latest = max(temps, key=lambda x: x[1] or "")
    return {"station": station, "count": len(temps),
            "max_f": round(mx[0], 1), "max_ts": mx[1],
            "min_f": round(mn[0], 1), "min_ts": mn[1],
            "latest_f": round(latest[0], 1), "latest_ts": latest[1]}


def gridpoint_forecast(lat: float, lon: float) -> Dict:
    """Hourly forecast at the station's own coordinates -- not the city's."""
    pt = _get(f"/points/{lat:.4f},{lon:.4f}")
    url = (pt.get("properties", {}) or {}).get("forecastHourly")
    if not url:
        raise WeatherError(f"no forecastHourly for {lat},{lon}")
    return _get(url)


def forecast_daily_extremes_f(lat: float, lon: float, date_str: str) -> Dict:
    """Forecast high/low in F for a local calendar date at these coordinates."""
    fc = gridpoint_forecast(lat, lon)
    periods = (fc.get("properties", {}) or {}).get("periods", []) or []
    vals = []
    for p in periods:
        st = p.get("startTime", "")
        if not st.startswith(date_str):
            continue
        t = p.get("temperature")
        unit = (p.get("temperatureUnit") or "F").upper()
        if t is None:
            continue
        vals.append(float(t) if unit == "F" else c_to_f(float(t)))
    if not vals:
        return {"date": date_str, "hours": 0, "high_f": None, "low_f": None}
    return {"date": date_str, "hours": len(vals),
            "high_f": round(max(vals), 1), "low_f": round(min(vals), 1),
            "generated_at": (fc.get("properties", {}) or {}).get("generatedAt")}


def cli_report(office: str, limit: int = 5) -> List[Dict]:
    """Daily Climate Report products from a Weather Forecast Office."""
    data = _get(f"/products/types/CLI/locations/{office}?limit={limit}")
    return data.get("@graph", []) or data.get("graph", []) or []


def cli_text(product_id: str) -> str:
    data = _get(f"/products/{product_id}")
    return data.get("productText", "") or ""


_MAX_RE = re.compile(r"^\s*MAXIMUM\s+(-?\d+)", re.M)
_MIN_RE = re.compile(r"^\s*MINIMUM\s+(-?\d+)", re.M)


def parse_cli_extremes(text: str) -> Dict:
    """Pull MAXIMUM / MINIMUM temperature out of a CLI product.

    Returns None values when the format is not recognised. We never guess --
    an unparsed CLI means the market is marked unresolvable, not estimated.
    """
    mx = _MAX_RE.search(text or "")
    mn = _MIN_RE.search(text or "")
    return {"max_f": int(mx.group(1)) if mx else None,
            "min_f": int(mn.group(1)) if mn else None,
            "parsed": bool(mx or mn)}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Daily Climate Report (CLI) -- the actual settlement source
# ---------------------------------------------------------------------------
# Polymarket US settles temperature contracts on the NWS Daily Climate Report
# for the named station. We therefore only ever CLAIM the CLI as a source when
# we have actually retrieved a product and matched BOTH:
#   * the AWIPS product id (e.g. CLINYC) -> the station, and
#   * the "CLIMATE SUMMARY FOR <month> <day> <year>" line -> the date.
# A near-miss is not a match. If we cannot pin the exact report, the caller
# treats the market as unresolvable and skips it, rather than quietly
# substituting observations and calling them the CLI.

_CLI_DATE_RE = re.compile(
    r"CLIMATE\s+SUMMARY\s+FOR\s+"
    r"([A-Z]+)\s+(\d{1,2})\s+(\d{4})", re.I)

_MONTH_NAMES = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], start=1)}


def parse_cli_date(text: str):
    """Date the CLI product actually covers, or None if not stated."""
    from datetime import date as _date
    m = _CLI_DATE_RE.search(text or "")
    if not m:
        return None
    mon = _MONTH_NAMES.get(m.group(1).upper())
    if not mon:
        return None
    try:
        return _date(int(m.group(3)), mon, int(m.group(2)))
    except ValueError:
        return None


def cli_for_date(office: str, awips_id: str, target_date, limit: int = 12):
    """Fetch the CLI product for an exact station and date.

    Returns a dict with max_f/min_f and provenance, or None when no product
    unambiguously matches. Never approximates.
    """
    try:
        products = cli_report(office, limit=limit)
    except WeatherError as e:
        return {"matched": False, "reason": f"CLI listing unavailable: {e}"}

    checked = []
    for p in products:
        pid = p.get("id")
        awips = (p.get("productCode") or p.get("awipsId") or "").upper()
        if not pid:
            continue
        # The listing may not expose the AWIPS id; fall back to the text.
        try:
            text = cli_text(pid)
        except WeatherError:
            continue
        if awips_id.upper() not in text.upper() and awips_id.upper() not in awips:
            continue
        d = parse_cli_date(text)
        checked.append({"id": pid, "date": d.isoformat() if d else None})
        if d != target_date:
            continue
        ext = parse_cli_extremes(text)
        if not ext["parsed"] or ext["max_f"] is None:
            return {"matched": False,
                    "reason": f"CLI {pid} matched {target_date} but its "
                              "MAXIMUM/MINIMUM lines could not be parsed",
                    "product_id": pid}
        return {"matched": True, "max_f": ext["max_f"], "min_f": ext["min_f"],
                "product_id": pid, "issued": p.get("issuanceTime"),
                "office": office, "awips_id": awips_id,
                "covers_date": target_date.isoformat(),
                "url": f"https://api.weather.gov/products/{pid}"}

    return {"matched": False,
            "reason": (f"no CLI product from WFO {office} matching {awips_id} "
                       f"covers {target_date}"),
            "products_checked": checked[:6]}
