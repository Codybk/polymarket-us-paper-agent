"""
Deterministic market scanner.

This is the 10-minute hot path. It contains NO language-model calls. Every
decision here is arithmetic, so a scan costs an API call budget and a few CPU
seconds, not a Claude session.

Markets that clear the quantitative filters but that this code cannot value
on its own (anything non-weather) are written to state/shortlist.json for a
separate, much less frequent Claude review. They are never auto-traded.
"""
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from . import pmus_client as pm
from . import nws_client as nws
from .book import Book, simulate_market_buy, simulate_market_buy_no
from .weather_model import (parse_temperature_market, resolve_station,
                            estimate, parse_target_date)
from .kelly import size_position

WEATHER_HINTS = re.compile(r"\b(temperature|temp|degrees|deg f|high temp|low temp|weather|rainfall|snow)\b", re.I)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def classify(market: Dict) -> str:
    cat = (market.get("category") or "").strip().lower()
    text = f"{market.get('question','')} {market.get('description','')}"
    if cat == "weather" or WEATHER_HINTS.search(text):
        return "weather"
    return cat or "other"


def cluster_key(market: Dict, station: Optional[str], target_date: Optional[str]) -> str:
    """Correlated-exposure key.

    All contracts on the same station and the same calendar date share one
    underlying outcome (that day's temperature at that station) and must be
    capped together -- otherwise five thresholds on one city look like
    diversification when they are one bet.
    """
    if station and target_date:
        return f"wx:{station}:{target_date}"
    ev = market.get("eventId") or market.get("subjectId") or market.get("slug")
    return f"ev:{ev}"


# ---------------------------------------------------------------------------
# Quantitative pre-filter -- cheap, runs on every market
# ---------------------------------------------------------------------------

def prefilter(market: Dict, cfg: dict) -> Tuple[bool, str]:
    if market.get("closed") or market.get("archived") or not market.get("active", True):
        return False, "market not active"
    if market.get("hidden"):
        return False, "market hidden"

    vol = market.get("volume") or 0
    try:
        vol = float(vol)
    except (TypeError, ValueError):
        vol = 0.0
    if vol < cfg["min_market_volume"]:
        return False, f"volume {vol:.0f} below minimum {cfg['min_market_volume']}"

    end = market.get("endDate")
    if not end:
        return False, "no expiration date published"
    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return False, f"unparseable expiration {end!r}"
    if end_dt <= datetime.now(timezone.utc):
        return False, "already expired"

    return True, "passed prefilter"


def spread_ok(book: Book, cfg: dict) -> Tuple[bool, str]:
    if not book.is_tradeable():
        return False, f"book not tradeable (state={book.state})"
    sp = book.spread
    if sp is None:
        return False, "no two-sided market"
    if sp > cfg["max_spread"]:
        return False, f"spread {sp:.3f} exceeds max {cfg['max_spread']:.3f}"
    return True, f"spread {sp:.3f} acceptable"


# ---------------------------------------------------------------------------
# Weather valuation
# ---------------------------------------------------------------------------

def value_weather_market(market: Dict, stations_cfg: dict) -> Optional[Dict]:
    """Return a valuation dict, or a refusal with a stated reason.

    Refusing is the common, correct outcome. A market is only valued when the
    threshold, the station, and the target DATE are each unambiguous, and when
    the data that will actually settle it is reachable.
    """
    q = market.get("question") or market.get("title") or ""
    desc = market.get("description") or ""
    rules = market.get("rulesDisclaimer") or desc

    parsed = parse_temperature_market(q, rules)
    if not parsed:
        return {"ok": False, "reason": "resolution threshold not machine-parseable from the rules"}

    station = resolve_station(q, rules, stations_cfg["city_aliases"])
    if not station or station not in stations_cfg["stations"]:
        return {"ok": False, "reason": f"could not pin an authoritative station (got {station!r})"}

    meta = stations_cfg["stations"][station]
    tz = ZoneInfo(meta["tz"])
    now_local = datetime.now(tz)

    # The weather date comes from the CONTRACT TEXT, never from endDate --
    # endDate is the settlement moment, typically the morning after the day
    # being measured, so using it would value the wrong day entirely.
    target_date, date_note = parse_target_date(q, rules, today=now_local.date())
    if target_date is None:
        return {"ok": False, "reason": f"target date unusable: {date_note}"}

    is_target_day = (now_local.date() == target_date)
    is_past = target_date < now_local.date()

    evidence = []
    cli = None

    # ---- If the day is over, the CLI is the settlement source. Use it or skip.
    if is_past:
        cli = nws.cli_for_date(meta["wfo"], meta["cli_awips"], target_date)
        if not cli.get("matched"):
            return {"ok": False,
                    "reason": (f"target date {target_date} has passed but the settling "
                               f"NWS Daily Climate Report could not be pinned: "
                               f"{cli.get('reason')}"),
                    "evidence": [{"source": f"NWS CLI lookup, WFO {meta['wfo']} / "
                                            f"{meta['cli_awips']}",
                                  "retrieved_at": nws.utcnow_iso(),
                                  "detail": cli.get("reason")}]}
        evidence.append({
            "source": f"NWS Daily Climate Report {cli['product_id']} "
                      f"(WFO {meta['wfo']}, {meta['cli_awips']})",
            "url": cli["url"], "retrieved_at": nws.utcnow_iso(),
            "detail": f"covers {cli['covers_date']}: max {cli['max_f']}F / "
                      f"min {cli['min_f']}F -- this is the settlement source"})
        settlement_source = (f"NWS Daily Climate Report {cli['product_id']} "
                             f"(retrieved and parsed)")
    else:
        # The CLI for this date does not exist yet. Say so plainly rather than
        # citing a document we have not read.
        settlement_source = (
            f"Will settle on the NWS Daily Climate Report ({meta['cli_awips']}, "
            f"WFO {meta['wfo']}) for {target_date}, which is not published yet. "
            f"Valuation below uses NWS observations and forecast at {station}.")

    # ---- Observations at the EXACT station, for the target local day.
    start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=tz)
    try:
        obs = nws.observed_extremes_f(station, _iso(start_local))
        evidence.append({"source": f"NWS METAR observations, station {station}",
                         "url": f"https://api.weather.gov/stations/{station}/observations",
                         "retrieved_at": nws.utcnow_iso(),
                         "detail": f"max {obs.get('max_f')}F / min {obs.get('min_f')}F "
                                   f"from {obs.get('count')} observations"})
    except Exception as e:  # noqa: BLE001
        obs = {"max_f": None, "min_f": None, "count": 0}
        evidence.append({"source": f"NWS observations {station}", "error": str(e),
                         "retrieved_at": nws.utcnow_iso()})

    # ---- Forecast at the station's own coordinates, not the city's.
    fc = {"high_f": None, "low_f": None}
    if not is_past:
        try:
            fc = nws.forecast_daily_extremes_f(meta["lat"], meta["lon"],
                                               target_date.isoformat())
            evidence.append({"source": f"NWS gridpoint forecast at {station} coordinates "
                                       f"({meta['lat']},{meta['lon']})",
                             "url": f"https://api.weather.gov/points/{meta['lat']},{meta['lon']}",
                             "retrieved_at": nws.utcnow_iso(),
                             "detail": f"high {fc.get('high_f')}F / low {fc.get('low_f')}F "
                                       f"from {fc.get('hours')} hourly periods"})
        except Exception as e:  # noqa: BLE001
            evidence.append({"source": "NWS gridpoint forecast", "error": str(e),
                             "retrieved_at": nws.utcnow_iso()})

    # ---- Settled days: the CLI decides, full stop.
    if cli and cli.get("matched"):
        actual = cli["max_f"] if parsed["metric"] == "high" else cli["min_f"]
        if actual is None:
            return {"ok": False, "reason": "CLI matched but the needed extreme was absent",
                    "evidence": evidence}
        yes = (actual > parsed["threshold_f"]) if parsed["direction"] == "above" \
            else (actual < parsed["threshold_f"])
        est = {"probability": 1.0 if yes else 0.0, "prob_low": 1.0 if yes else 0.0,
               "prob_high": 1.0 if yes else 0.0, "sigma_f": 0.0,
               "expected_high_f": actual, "observed_max_f": actual,
               "threshold_f": parsed["threshold_f"], "direction": parsed["direction"],
               "method": "settled_by_cli", "determined": True,
               "notes": f"CLI {cli['product_id']} reports {actual}F for {target_date}."}
    else:
        if fc.get("high_f") is None and obs.get("max_f") is None:
            return {"ok": False,
                    "reason": "no authoritative NWS data available for this station",
                    "evidence": evidence}
        est = estimate(
            metric=parsed["metric"], direction=parsed["direction"],
            threshold_f=parsed["threshold_f"],
            forecast_high_f=fc.get("high_f"), forecast_low_f=fc.get("low_f"),
            observed_max_f=obs.get("max_f"), observed_min_f=obs.get("min_f"),
            hours_elapsed_local=now_local.hour + now_local.minute / 60.0,
            is_target_day=is_target_day,
        ).to_dict()

    return {"ok": True, "station": station, "target_date": target_date.isoformat(),
            "date_note": date_note, "parsed": parsed, "estimate": est,
            "evidence": evidence, "settlement_source": settlement_source,
            "cli": cli, "rules_text": rules[:2000]}


# ---------------------------------------------------------------------------
# Edge computation
# ---------------------------------------------------------------------------

def _edge_for_side(book: Book, side: str, prob: float, prob_conservative: float,
                   contracts_probe: float, fee_coefficient: Optional[float]) -> Dict:
    sim = simulate_market_buy if side == "YES" else simulate_market_buy_no
    fill = sim(book, contracts_probe, fee_coefficient=fee_coefficient)
    if fill.filled <= 0:
        return {"side": side, "tradeable": False, "reason": fill.reason}

    all_in = fill.net_cost / fill.filled            # $ per contract INCLUDING fee
    return {
        "side": side, "tradeable": True,
        "probability": round(prob, 6),
        "probability_conservative": round(prob_conservative, 6),
        "avg_fill_price": fill.avg_price,
        "all_in_cost_per_contract": round(all_in, 6),
        "fee": fill.fee,
        "slippage_vs_touch": fill.slippage_vs_touch,
        "gross_edge_pp": round((prob - fill.avg_price) * 100, 3),
        "net_edge_pp": round((prob - all_in) * 100, 3),
        "conservative_edge_pp": round((prob_conservative - all_in) * 100, 3),
        "fill": fill.to_dict(),
    }


def compute_edge(book: Book, prob: float, prob_low: float, prob_high: float,
                 contracts_probe: float, fee_coefficient: Optional[float]) -> Dict:
    """Evaluate BOTH sides and return the better qualifying one.

    A market can be mispriced in either direction. Only ever buying YES throws
    away half the opportunities and, worse, biases the system toward markets
    the crowd already likes.

    Both sides are priced off executable book liquidity:
      * YES is bought by lifting the offer stack.
      * NO is bought by hitting the bid stack (buying NO at q is selling YES
        at 1-q), so NO liquidity is the bid side, repriced.

    Edges are computed on ALL-IN cost per contract -- fee included -- so the
    threshold is measured against cash actually committed.

    The conservative probability is the pessimistic end of the band FOR THAT
    SIDE: prob_low for YES, and (1 - prob_high) for NO.
    """
    yes = _edge_for_side(book, "YES", prob, prob_low,
                         contracts_probe, fee_coefficient)
    no = _edge_for_side(book, "NO", 1.0 - prob, 1.0 - prob_high,
                        contracts_probe, fee_coefficient)

    candidates = [c for c in (yes, no) if c.get("tradeable")]
    if not candidates:
        return {"tradeable": False,
                "reason": "; ".join(f"{c['side']}: {c.get('reason')}" for c in (yes, no)),
                "sides": {"YES": yes, "NO": no}}

    # Rank by the CONSERVATIVE edge -- the number the trade must actually clear.
    best = max(candidates, key=lambda c: c["conservative_edge_pp"])
    out = dict(best)
    out["sides"] = {"YES": yes, "NO": no}
    return out
