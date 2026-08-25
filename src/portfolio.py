"""
Paper portfolio: positions, mark-to-market, realized/unrealized P&L, calibration.

State lives in state/portfolio.json. Every mutation is also written to the
append-only audit log by the caller, so the JSON is a convenience cache and
the log is the record of truth.
"""
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Position:
    decision_id: str
    slug: str
    question: str
    url: str
    side: str                 # "YES" or "NO"
    contracts: float
    avg_price: float
    stake: float              # cash committed incl. entry fee
    entry_fee: float
    opened_at: str
    category: str
    cluster: str              # correlated-exposure key
    predicted_prob: float
    predicted_prob_low: float
    predicted_prob_high: float
    edge_pp_at_entry: float
    evidence: List[Dict] = field(default_factory=list)
    resolution_rules: str = ""
    expiration: Optional[str] = None
    fee_coefficient: Optional[float] = None   # the market's own theta, if it publishes one
    mark_price: Optional[float] = None
    mark_is_fallback: bool = False            # True when no executable exit existed
    mark_source: str = ""                     # how mark_price was derived
    settlement_pending: bool = False          # terminal-looking, outcome unparseable
    resolution_error: str = ""                # why settlement could not be applied
    settlement_detail: Optional[Dict] = None  # the adapter's verdict, for audit
    status: str = "OPEN"      # OPEN | CLOSED | RESOLVED
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    exit_fee: float = 0.0
    realized_pnl: Optional[float] = None
    outcome: Optional[int] = None   # 1 = YES resolved true

    def to_dict(self):
        return asdict(self)

    @property
    def cost_basis(self) -> float:
        return self.stake

    def unrealized(self) -> float:
        if self.status != "OPEN" or self.mark_price is None:
            return 0.0
        # Mark at the bid (what we could actually get out at), fee-inclusive
        # handled by the caller supplying a realistic mark.
        return round(self.contracts * self.mark_price - self.stake, 4)


class Portfolio:
    def __init__(self, path: str, starting_bankroll: float):
        self.path = path
        self.starting_bankroll = starting_bankroll
        self.cash = starting_bankroll
        self.peak_bankroll = starting_bankroll
        self.positions: List[Position] = []
        self.day_start_equity = starting_bankroll
        self.day_start_date = datetime.now(timezone.utc).date().isoformat()
        self.halted = False
        self.halt_reason = ""
        if os.path.exists(path):
            self.load()

    # ---------------- persistence ----------------
    def load(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        self.cash = d["cash"]
        self.starting_bankroll = d.get("starting_bankroll", self.starting_bankroll)
        self.peak_bankroll = d.get("peak_bankroll", self.starting_bankroll)
        self.day_start_equity = d.get("day_start_equity", self.starting_bankroll)
        self.day_start_date = d.get("day_start_date", self.day_start_date)
        self.halted = d.get("halted", False)
        self.halt_reason = d.get("halt_reason", "")
        self.positions = [Position(**p) for p in d.get("positions", [])]

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({
                "cash": round(self.cash, 4),
                "starting_bankroll": self.starting_bankroll,
                "peak_bankroll": round(self.peak_bankroll, 4),
                "day_start_equity": round(self.day_start_equity, 4),
                "day_start_date": self.day_start_date,
                "halted": self.halted,
                "halt_reason": self.halt_reason,
                "updated_at": _now(),
                "positions": [p.to_dict() for p in self.positions],
            }, fh, indent=2)

    # ---------------- views ----------------
    @property
    def open_positions(self) -> List[Position]:
        return [p for p in self.positions if p.status == "OPEN"]

    @property
    def closed_positions(self) -> List[Position]:
        return [p for p in self.positions if p.status != "OPEN"]

    def total_exposure(self) -> float:
        return round(sum(p.stake for p in self.open_positions), 4)

    def cluster_exposure(self, cluster: str) -> float:
        return round(sum(p.stake for p in self.open_positions if p.cluster == cluster), 4)

    def market_value(self) -> float:
        v = 0.0
        for p in self.open_positions:
            v += p.contracts * (p.mark_price if p.mark_price is not None else p.avg_price)
        return round(v, 4)

    def equity(self) -> float:
        return round(self.cash + self.market_value(), 4)

    def realized_pnl(self) -> float:
        return round(sum(p.realized_pnl or 0.0 for p in self.closed_positions), 4)

    def unrealized_pnl(self) -> float:
        return round(sum(p.unrealized() for p in self.open_positions), 4)

    def drawdown_pct(self) -> float:
        if self.peak_bankroll <= 0:
            return 0.0
        return round(max(0.0, (self.peak_bankroll - self.equity()) / self.peak_bankroll), 6)

    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return round((self.equity() - self.day_start_equity) / self.day_start_equity, 6)

    def total_fees(self) -> float:
        return round(sum(p.entry_fee + p.exit_fee for p in self.positions), 4)

    # ---------------- mutations ----------------
    def open_position(self, pos: Position):
        if pos.stake > self.cash + 1e-9:
            raise ValueError("insufficient paper cash")
        self.cash = round(self.cash - pos.stake, 6)
        self.positions.append(pos)
        # Deliberately NOT touching the high-water mark here. Opening a
        # position only converts cash into an asset worth slightly less
        # (spread + fee), so it can never be a genuine new high. Touching the
        # peak here once let a mis-marked NO position write an inflated
        # peak_bankroll, which then corrupted drawdown and the risk halts that
        # depend on it. The peak is updated only by
        # update_high_water_mark(), after a verified mark-to-market pass.

    def close_position(self, pos: Position, exit_price: float, exit_fee: float,
                       outcome: Optional[int] = None, reason: str = ""):
        proceeds = pos.contracts * exit_price - exit_fee
        pos.exit_price = exit_price
        pos.exit_fee = exit_fee
        pos.realized_pnl = round(proceeds - pos.stake, 4)
        pos.status = "RESOLVED" if outcome is not None else "CLOSED"
        pos.outcome = outcome
        pos.closed_at = _now()
        self.cash = round(self.cash + proceeds, 6)
        self._touch_peak()

    def _touch_peak(self):
        eq = self.equity()
        if eq > self.peak_bankroll:
            self.peak_bankroll = eq

    def update_high_water_mark(self, marks_verified: bool) -> bool:
        """Raise the peak only from a verified, side-aware, fee-inclusive pass.

        `marks_verified` must be True only when EVERY open position was
        successfully re-marked this scan. A partial pass leaves stale marks in
        the equity figure, and a peak written from stale or wrong marks
        understates every subsequent drawdown -- which is the direction that
        makes a risk halt less likely to fire, not more.
        """
        if not marks_verified:
            return False
        eq = self.equity()
        if eq > self.peak_bankroll:
            self.peak_bankroll = eq
            return True
        return False

    def roll_day_if_needed(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self.day_start_date:
            self.day_start_date = today
            self.day_start_equity = self.equity()
            return True
        return False

    # ---------------- analytics ----------------
    def calibration(self, bins=((0.0,0.2),(0.2,0.4),(0.4,0.6),(0.6,0.8),(0.8,1.0))) -> List[Dict]:
        out = []
        resolved = [p for p in self.positions if p.outcome is not None]
        for lo, hi in bins:
            grp = [p for p in resolved if lo <= p.predicted_prob < hi or (hi == 1.0 and p.predicted_prob == 1.0)]
            if not grp:
                out.append({"bin": f"{int(lo*100)}-{int(hi*100)}%", "n": 0,
                            "predicted": None, "actual": None})
                continue
            pred = sum(p.predicted_prob for p in grp) / len(grp)
            act = sum(1 for p in grp if p.outcome == 1) / len(grp)
            out.append({"bin": f"{int(lo*100)}-{int(hi*100)}%", "n": len(grp),
                        "predicted": round(pred, 4), "actual": round(act, 4)})
        return out

    def win_rate(self) -> Optional[float]:
        done = [p for p in self.closed_positions if p.realized_pnl is not None]
        if not done:
            return None
        return round(sum(1 for p in done if p.realized_pnl > 0) / len(done), 4)

    def pnl_by_category(self) -> Dict[str, float]:
        agg: Dict[str, float] = {}
        for p in self.closed_positions:
            agg[p.category] = round(agg.get(p.category, 0.0) + (p.realized_pnl or 0.0), 4)
        return agg
