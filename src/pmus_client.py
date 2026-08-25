"""
Polymarket US public market-data client.

Base URL: https://gateway.polymarket.us  (public gateway, no credentials --
verified against docs.polymarket.us/api-reference/introduction, 2026-08-24)

This module is READ-ONLY. It contains no order-placement code whatsoever.
Live order entry lives on a different, authenticated Polymarket US host and
requires an API key. That host is deliberately never named or referenced
anywhere in this repository, and tests/test_engine.py enforces its absence.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

BASE = "https://gateway.polymarket.us"
UA = "polymarket-cowork-agent/1.0 (paper-trading research; contact via GitHub)"


class DataError(RuntimeError):
    pass


class NotFound(DataError):
    """HTTP 404. For the settlement endpoint this means UNSETTLED, not failure."""


class SchemaError(DataError):
    """A 200 response did not match its documented schema.

    Raised rather than coerced. A market payload whose shape we do not
    recognise must surface loudly: silently reading lifecycle fields off the
    wrong nesting level yields `None` for every one of them, which makes a
    settled market look merely unresolved and strands the position open with
    no alert.
    """


def _get(path: str, params: Optional[dict] = None, timeout: int = 25,
         retries: int = 3) -> Any:
    url = f"{BASE}{path}"
    if params:
        flat = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                flat.extend((k, str(i)) for i in v)
            else:
                flat.append((k, str(v)))
        url += "?" + urllib.parse.urlencode(flat)

    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Never retry a 404 -- it is a definitive answer, and for
                # /settlement it is the documented way of saying "unsettled".
                raise NotFound(f"GET {url} returned 404") from e
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise DataError(f"GET {url} failed after {retries} attempts: {last}")


def health() -> Any:
    return _get("/v1/health")


def get_markets(limit: int = 100, offset: int = 0, active: bool = True,
                closed: bool = False, categories: Optional[List[str]] = None,
                **kw) -> List[Dict]:
    payload = _get("/v1/markets", {
        "limit": limit, "offset": offset, "active": active, "closed": closed,
        "categories": categories, **kw})
    if isinstance(payload, dict):
        return payload.get("markets", []) or []
    return payload or []


def get_all_markets(page: int = 100, max_pages: int = 40, **kw) -> List[Dict]:
    """Page through every accessible active market."""
    out, offset = [], 0
    for _ in range(max_pages):
        batch = get_markets(limit=page, offset=offset, **kw)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out


def get_market_book(slug: str) -> Dict:
    return _get(f"/v1/markets/{urllib.parse.quote(slug)}/book")


def get_market_bbo(slug: str) -> Dict:
    return _get(f"/v1/markets/{urllib.parse.quote(slug)}/bbo")


def _unwrap_market(payload: Any, where: str) -> Dict:
    """Normalize gateway.market.v1.GetMarketBySlugResponse -> the Market object.

    DOCUMENTED CONTRACT
    -------------------
    The endpoint returns an ENVELOPE, not a bare market:

        {"market": { ...gateway.market.v1.Market... }}

    (docs.polymarket.us/api-reference/markets/get-market-by-slug --
     `gateway.market.v1.GetMarketBySlugResponse` has exactly one property,
     `market`, referencing `gateway.market.v1.Market`.)

    Callers of get_market_by_slug() therefore ALWAYS receive the inner Market
    object, never the envelope. That normalization happens here, once.

    Ambiguous shapes are refused rather than accepted. Reading `closed`,
    `active`, or `ep3Status` off the envelope silently yields None for each,
    so a settled market reads as unresolved and its position is stranded open
    with no alert -- a failure that is invisible precisely because nothing
    errors.
    """
    if not isinstance(payload, dict):
        raise SchemaError(f"{where}: expected a JSON object, got "
                          f"{type(payload).__name__}")
    if "market" not in payload:
        raise SchemaError(
            f"{where}: response has no 'market' key. The documented schema is "
            f"{{'market': {{...}}}}; got keys {sorted(payload)[:8]}. Refusing to "
            "guess at an undocumented shape.")
    inner = payload["market"]
    if not isinstance(inner, dict):
        raise SchemaError(f"{where}: 'market' must be an object, got "
                          f"{type(inner).__name__}")
    return inner


def get_market_by_slug(slug: str) -> Optional[Dict]:
    """GET /v1/market/slug/{slug}  (note: singular `market` in this path).

    Returns the INNER `gateway.market.v1.Market` object, already unwrapped from
    its `{"market": ...}` envelope -- see _unwrap_market for the contract.

    Returns None on 404 so a delisted market does not look like an outage.
    Raises SchemaError if the envelope is missing or malformed.

    docs.polymarket.us/api-reference/markets/get-market-by-slug
    """
    try:
        payload = _get(f"/v1/market/slug/{urllib.parse.quote(slug)}")
    except NotFound:
        return None
    return _unwrap_market(payload, f"GET /v1/market/slug/{slug}")


def get_market_settlement(slug: str) -> Optional[Dict]:
    """GET /v1/markets/{slug}/settlement -> {"slug": str, "settlement": decimal}

    THE authoritative resolution source. Per the documented schema, 404 means
    "Market not found or unsettled", so it is returned as None -- an ordinary
    not-yet-resolved answer, never an error.

    docs.polymarket.us/api-reference/markets/get-market-settlement
    """
    try:
        return _get(f"/v1/markets/{urllib.parse.quote(slug)}/settlement")
    except NotFound:
        return None


def market_url(slug: str) -> str:
    return f"https://polymarket.us/market/{slug}"
