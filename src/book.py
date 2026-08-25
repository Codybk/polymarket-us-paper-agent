"""
Order-book handling and realistic paper fill simulation.

Design rule: a simulated fill NEVER uses the midpoint. It walks the actual
resting depth on the far side of the book at the moment of the decision.
If depth is insufficient, the fill is partial or refused -- we do not invent
liquidity that was not there.

Book shape from GET https://gateway.polymarket.us/v1/markets/{slug}/book
    marketData.bids   -> [{px, qty}, ...]   (buyers; best = highest px)
    marketData.offers -> [{px, qty}, ...]   (sellers; best = lowest px)
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional
from .fees import taker_fee


@dataclass
class Level:
    px: float
    qty: float


@dataclass
class Book:
    slug: str
    bids: List[Level] = field(default_factory=list)
    offers: List[Level] = field(default_factory=list)
    state: str = "UNKNOWN"
    transact_time: Optional[str] = None

    @classmethod
    def from_api(cls, payload: dict) -> "Book":
        md = payload.get("marketData", payload) or {}
        def levels(raw):
            out = []
            for e in raw or []:
                try:
                    px = float(e.get("px"))
                    qty = float(e.get("qty"))
                except (TypeError, ValueError):
                    continue
                if qty > 0 and 0.0 < px < 1.0:
                    out.append(Level(px, qty))
            return out
        b = sorted(levels(md.get("bids")), key=lambda l: -l.px)
        o = sorted(levels(md.get("offers")), key=lambda l: l.px)
        return cls(slug=md.get("marketSlug") or payload.get("slug", ""),
                   bids=b, offers=o,
                   state=md.get("state", "UNKNOWN"),
                   transact_time=md.get("transactTime"))

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].px if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.offers[0].px if self.offers else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round(self.best_ask - self.best_bid, 6)

    @property
    def mid(self) -> Optional[float]:
        """Reference price only. Never used as a fill price."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    def depth_within(self, side: str, limit_px: float) -> float:
        """Contracts available at or better than limit_px."""
        if side == "BUY":
            return sum(l.qty for l in self.offers if l.px <= limit_px + 1e-12)
        return sum(l.qty for l in self.bids if l.px >= limit_px - 1e-12)

    def is_tradeable(self) -> bool:
        return (self.state in ("MARKET_STATE_OPEN", "OPEN")
                and self.best_bid is not None and self.best_ask is not None)


@dataclass
class Fill:
    filled: float
    avg_price: float
    gross_cost: float
    fee: float
    net_cost: float
    slippage_vs_touch: float
    levels_consumed: list
    complete: bool
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def simulate_market_buy(book: Book, contracts: float, max_price: float = 0.99,
                        fee_coefficient: float | None = None) -> Fill:
    """Walk the offer side. Buying YES (or NO) as a taker."""
    return _walk(book.offers, contracts, "BUY", max_price, book.best_ask, fee_coefficient)


def simulate_market_sell(book: Book, contracts: float, min_price: float = 0.01,
                         fee_coefficient: float | None = None) -> Fill:
    """Walk the bid side. Exiting a position as a taker."""
    return _walk(book.bids, contracts, "SELL", min_price, book.best_bid, fee_coefficient)


def _walk(levels: List[Level], want: float, side: str, limit: float,
          touch: Optional[float], fee_coefficient) -> Fill:
    if touch is None or not levels:
        return Fill(0, 0, 0, 0, 0, 0, [], False, "no resting liquidity")

    remaining = want
    filled = 0.0
    gross = 0.0
    consumed = []

    for lvl in levels:
        if remaining <= 1e-9:
            break
        if side == "BUY" and lvl.px > limit + 1e-12:
            break
        if side == "SELL" and lvl.px < limit - 1e-12:
            break
        take = min(remaining, lvl.qty)
        filled += take
        gross += take * lvl.px
        consumed.append({"px": lvl.px, "qty": take})
        remaining -= take

    if filled <= 1e-9:
        return Fill(0, 0, 0, 0, 0, 0, [], False,
                    f"no liquidity within limit {limit}")

    avg = gross / filled
    f = taker_fee(filled, avg, fee_coefficient)
    # Buying costs money (+fee). Selling returns money (-fee).
    net = gross + f if side == "BUY" else gross - f
    slip = (avg - touch) if side == "BUY" else (touch - avg)

    return Fill(
        filled=round(filled, 4),
        avg_price=round(avg, 6),
        gross_cost=round(gross, 4),
        fee=round(f, 4),
        net_cost=round(net, 4),
        slippage_vs_touch=round(slip, 6),
        levels_consumed=consumed,
        complete=(remaining <= 1e-9),
        reason="" if remaining <= 1e-9 else f"partial: {round(remaining,2)} contracts unavailable",
    )


def simulate_market_buy_no(book: Book, contracts: float, max_price: float = 0.99,
                           fee_coefficient: float | None = None) -> Fill:
    """Buy the NO side as a taker.

    On a binary market, buying NO at price q is economically the same as
    selling YES at (1 - q): you pay q now and receive $1 if the event does NOT
    happen. So NO liquidity is the YES *bid* stack, repriced.

    The highest YES bid is the CHEAPEST NO, so walking bids in their natural
    descending-price order already walks NO from best to worst.

    The fee formula is symmetric about 0.50, so a NO contract at q costs the
    same fee as a YES contract at (1 - q). We still compute it at the NO price
    to keep the audit record faithful to what was actually bought.
    """
    if not book.bids:
        return Fill(0, 0, 0, 0, 0, 0, [], False, "no resting bid liquidity for NO")

    no_levels = [Level(px=round(1.0 - l.px, 6), qty=l.qty) for l in book.bids]
    touch = no_levels[0].px
    return _walk(no_levels, contracts, "BUY", max_price, touch, fee_coefficient)


def best_no_ask(book: Book) -> Optional[float]:
    return round(1.0 - book.best_bid, 6) if book.best_bid is not None else None


def no_depth_within(book: Book, limit_px: float) -> float:
    """NO contracts available at or better than limit_px."""
    return sum(l.qty for l in book.bids if (1.0 - l.px) <= limit_px + 1e-12)


def simulate_market_sell_no(book: Book, contracts: float, min_price: float = 0.01,
                            fee_coefficient: float | None = None) -> Fill:
    """Exit a NO position as a taker.

    Selling NO at q is the same as buying YES at (1 - q), so the counterparties
    are the resting YES *offers*. Proceeds per NO contract are (1 - offer_px),
    and the LOWEST YES offer yields the MOST for our NO, so walking offers in
    their natural ascending order walks our exit from best to worst.

    Marking a NO position off the YES bid -- which is what a side-agnostic exit
    does -- values it at roughly (1 - its true worth) and inflates equity
    dramatically. Paper results computed that way are meaningless.
    """
    if not book.offers:
        return Fill(0, 0, 0, 0, 0, 0, [], False, "no resting offer liquidity to exit NO")
    no_bids = [Level(px=round(1.0 - l.px, 6), qty=l.qty) for l in book.offers]
    touch = no_bids[0].px
    return _walk(no_bids, contracts, "SELL", min_price, touch, fee_coefficient)


def exit_fill(book: Book, side: str, contracts: float,
              fee_coefficient: float | None = None) -> Fill:
    """Side-aware exit simulation. Always use this to mark or close."""
    if side == "NO":
        return simulate_market_sell_no(book, contracts, fee_coefficient=fee_coefficient)
    return simulate_market_sell(book, contracts, fee_coefficient=fee_coefficient)
