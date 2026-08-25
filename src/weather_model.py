"""
Fair-probability estimation for Polymarket US temperature contracts.

The honest thesis
-----------------
There is no clever meteorology here and we should not pretend otherwise. The
only edge this model plausibly has is *bookkeeping*: as a trading day
progresses, part of the outcome becomes a matter of record rather than
forecast. If Central Park has already observed 91F today, then "high above
88F" is settled in all but name, and any price meaningfully below ~0.97 is
mispriced. Conversely a threshold far above the day's remaining potential is
near-dead. The model's job is to quantify how much is already determined and
to be honest that early-in-the-day estimates are mostly just the NWS forecast
with wide error bars -- which is exactly where we should NOT be trading.

Uncertainty model
-----------------
NWS max-temperature forecast error is roughly normal. Published verification
puts day-1 mean absolute error near 2.5-3.0F, implying sigma near 3.5-4.0F.
We use sigma that shrinks with elapsed day and with observational anchoring,
and we never let sigma go below a floor that accounts for station data
revision and CLI/METAR disagreement (Polymarket US explicitly allows for that
disagreement in its settlement rules).
"""
import math
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, Dict, Tuple

# Sigma (F) on the day's remaining high, by hours of daylight still to come.
SIGMA_FLOOR = 0.8           # data revision / CLI-vs-METAR disagreement

# A forecast-based model must never assert certainty. Beyond the meteorology
# there is always residual risk it cannot see: a station outage, a revised
# observation, a CLI that disagrees with METAR, an unforeseen rule
# clarification. Clamping keeps that honest and, incidentally, stops Kelly
# from computing an unbounded bet off a probability of exactly 1.
PROB_CLAMP = 0.99
SIGMA_DAY_AHEAD = 4.0       # forecast issued ~24h out
SIGMA_SAME_DAY_MORNING = 3.0

# Diurnal anchors (local solar-ish time). Daily maxima cluster in the
# mid-afternoon and minima around sunrise. After the relevant peak, the
# extreme is very nearly locked in, and an observation that disagrees with
# the morning forecast should dominate that forecast -- not the other way
# round. Getting this backwards is how a model talks itself into buying a
# threshold the station has already failed to reach.
PEAK_HIGH_HOUR = 15.5
PEAK_LOW_HOUR = 6.5
UNCONSTRAINED = 40.0        # effectively "observation tells us nothing yet"


def remaining_rise_f(hours_elapsed: float) -> float:
    """How much more the daily HIGH can plausibly climb from here."""
    h = hours_elapsed
    if h <= 10.0:
        return UNCONSTRAINED                     # morning: forecast governs
    if h < PEAK_HIGH_HOUR:
        span = PEAK_HIGH_HOUR - 10.0
        return 12.0 * (PEAK_HIGH_HOUR - h) / span
    return max(0.3, 1.2 - 0.4 * (h - PEAK_HIGH_HOUR))


def remaining_fall_f(hours_elapsed: float) -> float:
    """How much more the daily LOW can plausibly fall from here."""
    h = hours_elapsed
    if h <= 2.0:
        return UNCONSTRAINED
    if h < PEAK_LOW_HOUR:
        return 8.0 * (PEAK_LOW_HOUR - h) / (PEAK_LOW_HOUR - 2.0)
    if h < 20.0:
        return 0.3                               # daytime: today's low is set
    return UNCONSTRAINED                          # late evening cooling


@dataclass
class WeatherEstimate:
    probability: float
    prob_low: float
    prob_high: float
    sigma_f: float
    expected_high_f: Optional[float]
    observed_max_f: Optional[float]
    threshold_f: float
    direction: str
    method: str
    determined: bool
    notes: str

    def to_dict(self):
        return asdict(self)


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Market question parsing -- deterministic, refuses anything ambiguous.
# ---------------------------------------------------------------------------

_PATTERNS = [
    # "Will the high temperature in NYC be above 88F on August 24?"
    (re.compile(r"\bhigh(?:est)?\s+temp\w*\b.*?\b(above|below|over|under|at least|greater than|less than)\b\s*(-?\d{1,3})\s*(?:°|deg\w*)?\s*F?", re.I), "high"),
    (re.compile(r"\blow(?:est)?\s+temp\w*\b.*?\b(above|below|over|under|at least|greater than|less than)\b\s*(-?\d{1,3})\s*(?:°|deg\w*)?\s*F?", re.I), "low"),
    # "NYC high temp on Aug 24: 88F or above"
    (re.compile(r"\bhigh\b.*?(-?\d{1,3})\s*(?:°|deg\w*)?\s*F\b.*?\b(or above|or higher|or more|or below|or lower|or less)\b", re.I), "high_suffix"),
    # Shorthand titles: "NYC high above 88F", "Chicago low below 30F".
    # An explicit F unit is REQUIRED here -- without it this pattern would
    # happily read "high above 500" out of a market about share prices.
    (re.compile(r"\bhigh\b(?:\W+\w+){0,4}?\W+\b(above|below|over|under|at least|greater than|less than)\b\s*(-?\d{1,3})\s*(?:°|deg\w*)?\s*F\b", re.I), "high"),
    (re.compile(r"\blow\b(?:\W+\w+){0,4}?\W+\b(above|below|over|under|at least|greater than|less than)\b\s*(-?\d{1,3})\s*(?:°|deg\w*)?\s*F\b", re.I), "low"),
]

_ABOVE_WORDS = {"above", "over", "at least", "greater than", "or above", "or higher", "or more"}
_BELOW_WORDS = {"below", "under", "less than", "or below", "or lower", "or less"}


def parse_temperature_market(question: str, description: str = "") -> Optional[Dict]:
    """Extract (metric, direction, threshold_f) or None if not cleanly parseable.

    Returning None is a feature: an unparsed market is skipped, never guessed.
    """
    text = f"{question} {description}"
    for rx, kind in _PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        if kind == "high_suffix":
            thr = float(m.group(1))
            word = m.group(2).lower()
            metric = "high"
        else:
            word = m.group(1).lower()
            thr = float(m.group(2))
            metric = kind
        if word in _ABOVE_WORDS:
            direction = "above"
        elif word in _BELOW_WORDS:
            direction = "below"
        else:
            return None
        if not (-60 <= thr <= 140):
            return None
        return {"metric": metric, "direction": direction, "threshold_f": thr}
    return None


def resolve_station(question: str, description: str, aliases: Dict[str, str]) -> Optional[str]:
    """Map a market to its EXACT settlement station.

    We first look for an explicit station id in the rules text, because the
    rules are authoritative. Only if absent do we fall back to a city alias,
    and a city that maps to no configured station returns None rather than a
    guess -- a citywide forecast is never a substitute for the station.
    """
    text = f"{question} {description}"
    m = re.search(r"\b(K[A-Z]{3})\b", text)
    if m:
        return m.group(1)
    low = text.lower()
    # longest alias first so "new york city" beats "new york"
    for alias in sorted(aliases, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return aliases[alias]
    return None


# ---------------------------------------------------------------------------
# Probability estimation
# ---------------------------------------------------------------------------

def estimate(
    *,
    metric: str,
    direction: str,
    threshold_f: float,
    forecast_high_f: Optional[float],
    forecast_low_f: Optional[float],
    observed_max_f: Optional[float],
    observed_min_f: Optional[float],
    hours_elapsed_local: float,
    is_target_day: bool,
) -> WeatherEstimate:
    """Estimate P(contract resolves YES) with a confidence band."""

    if metric == "high":
        forecast = forecast_high_f
        observed = observed_max_f
    else:
        forecast = forecast_low_f
        observed = observed_min_f

    notes = []
    determined = False

    # --- Case 1: the outcome is already a matter of record -----------------
    # A day's high can only rise. If it has already cleared the threshold,
    # "above" is effectively settled. Same logic mirrored for lows.
    if is_target_day and observed is not None:
        if metric == "high" and observed > threshold_f:
            determined = True
            notes.append(f"Observed max {observed}F already exceeds {threshold_f}F; "
                         "a daily high cannot decrease.")
        if metric == "low" and observed < threshold_f:
            determined = True
            notes.append(f"Observed min {observed}F is already below {threshold_f}F; "
                         "a daily low cannot increase.")

    if determined:
        p_above = 0.985  # residual: CLI vs METAR disagreement, data revision
        prob = p_above if direction == "above" else 1.0 - p_above
        return WeatherEstimate(
            probability=prob, prob_low=max(0.0, prob - 0.02),
            prob_high=min(1.0, prob + 0.01), sigma_f=SIGMA_FLOOR,
            expected_high_f=forecast, observed_max_f=observed,
            threshold_f=threshold_f, direction=direction,
            method="determined_by_observation", determined=True,
            notes=" ".join(notes))

    # --- Case 2: forecast-driven estimate -----------------------------------
    if forecast is None:
        return WeatherEstimate(0.5, 0.0, 1.0, 99.0, None, observed, threshold_f,
                               direction, "no_forecast_available", False,
                               "No station forecast available; unusable.")

    # Sigma shrinks through the day.
    if not is_target_day:
        sigma = SIGMA_DAY_AHEAD
        notes.append("Day-ahead forecast; wide error band.")
    elif hours_elapsed_local < PEAK_HIGH_HOUR:
        sigma = SIGMA_SAME_DAY_MORNING
        notes.append(f"Same-day but pre-peak ({hours_elapsed_local:.1f}h local); "
                     "the day's extreme is not yet determined.")
    else:
        # Past peak heating: the extreme is largely a matter of record.
        shrink = min(1.0, (hours_elapsed_local - PEAK_HIGH_HOUR) / 3.0)
        sigma = SIGMA_SAME_DAY_MORNING * (1 - shrink) + SIGMA_FLOOR * shrink
        notes.append(f"Post-peak window ({hours_elapsed_local:.1f}h local); "
                     "error band tightened.")

    # Anchor the central estimate on observation. The observation constrains
    # the outcome in BOTH directions: it is a hard floor (a daily high cannot
    # fall) and, after peak heating, also a near-ceiling (it has little room
    # left to climb). Trusting a morning forecast over an afternoon
    # observation is the single most dangerous error available here.
    center = forecast
    if is_target_day and observed is not None:
        if metric == "high":
            headroom = remaining_rise_f(hours_elapsed_local)
            center = max(observed, min(forecast, observed + headroom))
            if headroom < UNCONSTRAINED:
                if center < forecast - 0.05:
                    notes.append(
                        f"Station has reached only {observed}F with about {headroom:.1f}F "
                        f"of climb left at {hours_elapsed_local:.1f}h local; the {forecast}F "
                        f"forecast is no longer attainable, so the estimate is capped at "
                        f"{center:.1f}F.")
                elif observed > forecast:
                    notes.append(f"Observed max {observed}F already exceeds the {forecast}F "
                                 "forecast; observation governs.")
        else:
            headroom = remaining_fall_f(hours_elapsed_local)
            center = min(observed, max(forecast, observed - headroom))
            if headroom < UNCONSTRAINED and center > forecast + 0.05:
                notes.append(
                    f"Station has fallen only to {observed}F with about {headroom:.1f}F "
                    f"of fall left; estimate floored at {center:.1f}F.")

    sigma = max(sigma, SIGMA_FLOOR)
    z = (center - threshold_f) / sigma
    p_above = _phi(z)

    # Confidence band: propagate a +/-25% uncertainty on sigma itself, since
    # our sigma is an assumption rather than a measurement.
    z_wide = (center - threshold_f) / (sigma * 1.25)
    z_tight = (center - threshold_f) / (sigma * 0.75)
    band = sorted([_phi(z_wide), _phi(z_tight)])

    if direction == "above":
        prob, lo, hi = p_above, band[0], band[1]
    else:
        prob, lo, hi = 1 - p_above, 1 - band[1], 1 - band[0]

    def _clamp(x):
        return min(PROB_CLAMP, max(1.0 - PROB_CLAMP, x))

    prob, lo, hi = _clamp(prob), _clamp(lo), _clamp(hi)
    if prob >= PROB_CLAMP or prob <= 1.0 - PROB_CLAMP:
        notes.append(f"Probability clamped to +/-{PROB_CLAMP:.0%}; the model does "
                     "not assert certainty from a forecast.")

    return WeatherEstimate(
        probability=round(prob, 4), prob_low=round(lo, 4), prob_high=round(hi, 4),
        sigma_f=round(sigma, 2), expected_high_f=center, observed_max_f=observed,
        threshold_f=threshold_f, direction=direction,
        method="normal_around_station_forecast", determined=False,
        notes=" ".join(notes))


# ---------------------------------------------------------------------------
# Target-date parsing
# ---------------------------------------------------------------------------
# The contract's weather date comes from the QUESTION or RESOLUTION RULES,
# never from the market's endDate. Those are different things: endDate is when
# the contract stops trading or settles, which for a temperature contract is
# typically the MORNING AFTER the day being measured (settlement is 8:00 AM ET
# the following day, per the Polymarket US weather rules). Deriving the weather
# date from endDate therefore lands a day late and values the wrong day.
#
# If no unambiguous date can be read from the text, the market is skipped.

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

_DATE_PATTERNS = [
    # 2026-08-24
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    # August 24, 2026  /  Aug 24 2026
    (re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(\d{{4}})\b", re.I), "mdy"),
    # 24 August 2026
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\.?\s*,?\s*(\d{{4}})\b", re.I), "dmy"),
    # August 24  (no year)
    (re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*,?\s*\d{{4}})", re.I), "md"),
]


def parse_target_date(question: str, rules: str = "", *, today=None):
    """Return (date, note) or (None, reason).

    Refuses rather than guesses: no date, or two different dates in the text,
    both return None.
    """
    from datetime import date as _date, timedelta as _td
    today = today or _date.today()
    text = f"{question} {rules}"

    found = []
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "ymd":
                    d = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "mdy":
                    d = _date(int(m.group(3)), _MONTHS[m.group(1).lower().rstrip(".")],
                              int(m.group(2)))
                elif kind == "dmy":
                    d = _date(int(m.group(3)), _MONTHS[m.group(2).lower().rstrip(".")],
                              int(m.group(1)))
                else:  # md -- infer the nearest plausible year
                    mo = _MONTHS[m.group(1).lower().rstrip(".")]
                    dy = int(m.group(2))
                    cands = []
                    for yr in (today.year - 1, today.year, today.year + 1):
                        try:
                            cands.append(_date(yr, mo, dy))
                        except ValueError:
                            pass
                    if not cands:
                        continue
                    d = min(cands, key=lambda c: abs((c - today).days))
                    if abs((d - today).days) > 180:
                        continue
            except (ValueError, KeyError):
                continue
            found.append(d)
        if found:
            break   # most specific pattern that matched wins

    uniq = sorted(set(found))
    if not uniq:
        return None, "no weather date found in the question or resolution rules"
    if len(uniq) > 1:
        return None, (f"ambiguous date: text contains {len(uniq)} distinct dates "
                      f"({', '.join(d.isoformat() for d in uniq)})")
    d = uniq[0]
    if abs((d - today).days) > 370:
        return None, f"parsed date {d.isoformat()} is implausibly far from today"
    return d, f"target date {d.isoformat()} parsed from the contract text"
