"""
Market discovery.

WHY THIS MODULE EXISTS
----------------------
The first live scan reported exactly 4,000 markets, every one of them sports,
and not a single weather market. 4,000 is 40 pages x 100 per page -- precisely
the `max_pages=40` cap in the old `get_all_markets()`. The scanner was walking
an unfiltered, undated listing dominated by sports and hitting its ceiling long
before any weather market appeared.

The failure is worth naming precisely: nothing errored. The dashboard showed a
healthy scan of 4,000 markets. The weather strategy simply never saw its
markets, and a cap silently decided the strategy's entire universe.

The fix has two halves:

  1. WEATHER IS DISCOVERED EXPLICITLY AND EXHAUSTIVELY, using the documented
     category and tag filters, before any broad scan. A targeted query returns
     tens of markets, not thousands, so it genuinely runs to completion.

  2. THE BROAD SCAN IS HONEST ABOUT ITS LIMITS. It paginates until the API
     stops returning rows, and if it ever hits its safety ceiling it reports
     `exhausted=False` and the dashboard says so, rather than presenting a
     truncated slice as "all markets".

Discovery never decides whether a market is tradeable -- it only decides what
gets looked at. Every filter, threshold and risk limit downstream is unchanged.

DOCUMENTED ENDPOINTS (verified 2026-08-25)
------------------------------------------
GET /v1/markets            limit, offset, active, closed, categories[], tagIds[]
                           https://docs.polymarket.us/api-reference/markets/get-markets
GET /v2/tags               limit, offset, slug[], ids[], parentSlug, query
                           (query = case-insensitive substring on tag label)
                           https://docs.polymarket.us/api-reference/tags/get-tags
GET /v1/search             query, limit, page -> {events: [{markets: [...]}]}
                           https://docs.polymarket.us/api-reference/search/search
"""
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Tuple

from . import pmus_client as pm

# Category labels seen on temperature contracts. Queried case-variantly because
# the docs do not pin the canonical casing.
WEATHER_CATEGORY_LABELS = ("weather", "Weather", "WEATHER")

# Substring used against tag labels via GET /v2/tags?query=
WEATHER_TAG_QUERIES = ("weather", "temperature")

# Free-text search terms. Deliberately generic: the per-market parser decides
# what is actually a temperature contract, so over-fetching here is harmless
# while under-fetching is exactly the bug being fixed.
WEATHER_SEARCH_TERMS = ("temperature", "high temperature", "weather")


@dataclass
class PagedResult:
    markets: List[Dict] = field(default_factory=list)
    pages_fetched: int = 0
    exhausted: bool = False        # True only if the API ran out of rows
    source: str = ""
    error: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["market_count"] = len(self.markets)
        d.pop("markets", None)
        return d


@dataclass
class DiscoveryResult:
    weather: List[Dict] = field(default_factory=list)
    other: List[Dict] = field(default_factory=list)
    weather_sources: List[Dict] = field(default_factory=list)
    broad: Optional[PagedResult] = None
    weather_complete: bool = False   # every weather strategy ran to exhaustion

    @property
    def universe(self) -> List[Dict]:
        """Weather FIRST, so a capped broad scan can never crowd it out."""
        return self.weather + self.other

    def to_dict(self) -> Dict:
        return {
            "weather_markets_found": len(self.weather),
            "other_markets_found": len(self.other),
            "weather_discovery_complete": self.weather_complete,
            "weather_sources": self.weather_sources,
            "broad_scan": self.broad.to_dict() if self.broad else None,
        }


def _slug_of(m: Dict) -> Optional[str]:
    return m.get("slug") or (str(m.get("id")) if m.get("id") is not None else None)


def paginate(fetch_page: Callable[[int, int], List[Dict]], *, page_size: int = 100,
             max_pages: int = 200, source: str = "") -> PagedResult:
    """Page until the API stops returning rows, or the safety ceiling is hit.

    `exhausted` is True ONLY when the API itself ran out of rows -- a short or
    empty page. Hitting `max_pages` sets it False, and callers must surface
    that rather than pretending the listing was complete.
    """
    out: List[Dict] = []
    seen = set()
    offset = 0
    for page in range(1, max_pages + 1):
        try:
            batch = fetch_page(page_size, offset) or []
        except Exception as e:  # noqa: BLE001
            return PagedResult(out, page - 1, False, source, f"{type(e).__name__}: {e}")

        new = 0
        for m in batch:
            s = _slug_of(m)
            if s and s not in seen:
                seen.add(s)
                out.append(m)
                new += 1

        if len(batch) < page_size:
            return PagedResult(out, page, True, source)      # API ran out
        if new == 0:
            # Offset paging is not advancing (repeated rows). Stop rather than
            # spin, and report it as NOT exhausted so it is not mistaken for
            # a complete listing.
            return PagedResult(out, page, False, source,
                               "pagination stopped advancing (duplicate page)")
        offset += page_size

    return PagedResult(out, max_pages, False, source,
                       f"hit the {max_pages}-page safety ceiling; listing is TRUNCATED")


# ---------------------------------------------------------------------------
# Weather discovery -- explicit, targeted, exhaustive
# ---------------------------------------------------------------------------

def _by_category(label: str, page_size: int, max_pages: int) -> PagedResult:
    return paginate(
        lambda lim, off: pm.get_markets(limit=lim, offset=off, active=True,
                                        closed=False, categories=[label]),
        page_size=page_size, max_pages=max_pages,
        source=f"GET /v1/markets?categories={label}")


def _weather_tag_ids() -> Tuple[List[int], str]:
    ids, notes = [], []
    for q in WEATHER_TAG_QUERIES:
        try:
            tags = pm.get_tags(query=q, limit=100)
        except Exception as e:  # noqa: BLE001
            notes.append(f"tag query {q!r} failed: {e}")
            continue
        for t in tags:
            label = str(t.get("label") or "")
            slug = str(t.get("slug") or "")
            if "weather" in (label + slug).lower() or "temperature" in (label + slug).lower():
                tid = t.get("id")
                if tid is not None:
                    try:
                        ids.append(int(tid))
                    except (TypeError, ValueError):
                        notes.append(f"non-numeric tag id {tid!r}")
    return sorted(set(ids)), "; ".join(notes)


def _by_tag(tag_id: int, page_size: int, max_pages: int) -> PagedResult:
    return paginate(
        lambda lim, off: pm.get_markets(limit=lim, offset=off, active=True,
                                        closed=False, tagIds=[tag_id]),
        page_size=page_size, max_pages=max_pages,
        source=f"GET /v1/markets?tagIds={tag_id}")


def _by_search(term: str, limit: int = 100) -> PagedResult:
    try:
        markets = pm.search_markets(term, limit=limit)
    except Exception as e:  # noqa: BLE001
        return PagedResult([], 0, False, f"GET /v1/search?query={term}",
                           f"{type(e).__name__}: {e}")
    return PagedResult(markets, 1, True, f"GET /v1/search?query={term}")


def discover_weather_markets(*, page_size: int = 100, max_pages: int = 50,
                             use_search: bool = True) -> Tuple[List[Dict], List[Dict], bool]:
    """Find every accessible weather market. Returns (markets, sources, complete).

    Several independent strategies are run and unioned. They overlap heavily by
    design: if the category label is not what we guessed, or the tag is absent,
    another route still finds the markets. `complete` is True only when every
    strategy that returned anything ran to exhaustion.
    """
    found: Dict[str, Dict] = {}
    sources: List[Dict] = []
    complete = True
    had_error = False

    def _note(res: PagedResult) -> None:
        """Record a strategy's result. Any error, or any listing that did not
        run to exhaustion, means discovery cannot be called complete."""
        nonlocal complete, had_error
        if res.error:
            had_error = True
            complete = False
        if res.markets and not res.exhausted:
            complete = False
        for m in res.markets:
            s_ = _slug_of(m)
            if s_:
                found.setdefault(s_, m)
        sources.append(res.to_dict())

    for label in WEATHER_CATEGORY_LABELS:
        res = _by_category(label, page_size, max_pages)
        _note(res)
        if res.markets:
            break          # the canonical label worked; no need for variants

    tag_ids, tag_note = _weather_tag_ids()
    if tag_note:
        had_error = True
        complete = False
        sources.append({"source": "GET /v2/tags", "error": tag_note,
                        "market_count": 0, "exhausted": False, "pages_fetched": 0})
    for tid in tag_ids:
        _note(_by_tag(tid, page_size, max_pages))

    if use_search:
        for term in WEATHER_SEARCH_TERMS:
            _note(_by_search(term))

    # An empty result is reported as-is; run_scan raises it as an error, since
    # "no weather markets anywhere" and "our filters are wrong" look identical
    # from here and both mean the strategy has nothing to trade.
    return list(found.values()), sources, complete


def discover(*, page_size: int = 100, weather_max_pages: int = 50,
             broad_max_pages: int = 200, include_broad: bool = True,
             use_search: bool = True) -> DiscoveryResult:
    """Weather first and in full, then everything else."""
    weather, sources, complete = discover_weather_markets(
        page_size=page_size, max_pages=weather_max_pages, use_search=use_search)
    weather_slugs = {_slug_of(m) for m in weather}

    broad = None
    other: List[Dict] = []
    if include_broad:
        broad = paginate(
            lambda lim, off: pm.get_markets(limit=lim, offset=off, active=True,
                                            closed=False),
            page_size=page_size, max_pages=broad_max_pages,
            source="GET /v1/markets (all active)")
        other = [m for m in broad.markets if _slug_of(m) not in weather_slugs]

    return DiscoveryResult(weather=weather, other=other, weather_sources=sources,
                           broad=broad, weather_complete=complete)
