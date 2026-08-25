"""
Settlement adapter: Polymarket US resolution payloads -> one canonical outcome.

WHY THIS MODULE EXISTS
----------------------
Settlement used to be inferred inline with `float(settle.get("settlementValue"))`,
guarded by a bare `except`, and only attempted when the order book had already
been fetched successfully AND reported one of two exact states. Every one of
those assumptions could fail on a genuinely resolved market:

  * a resolved market may no longer serve an order book at all,
  * it may report a terminal state this code did not enumerate,
  * the resolution field may not be the assumed name or a plain number.

In each case the exception was swallowed and the paper position stayed open
forever, quietly holding a stale mark that the final report would then count as
if it were a live result.

This module makes the parse explicit, total, and testable. It NEVER raises for
bad input -- it returns a classification, and the caller decides. The only
classification that may close a position is RESOLVED.

DOCUMENTED SOURCES (verified 2026-08-25)
----------------------------------------
Settlement endpoint
    GET https://gateway.polymarket.us/v1/markets/{slug}/settlement
    -> gateway.market.v1.GetMarketSettlementResponse
       { "slug": string, "settlement": decimal }
    200 = settled. 404 = "Market not found or unsettled" -- NOT an error.
    https://docs.polymarket.us/api-reference/markets/get-market-settlement

Market lookup
    GET https://gateway.polymarket.us/v1/market/slug/{slug}
    Lifecycle fields: active, closed, archived, hidden, ep3Status
    https://docs.polymarket.us/api-reference/markets/get-market-by-slug

Payout semantics
    "every contract settles at either $1.00 (YES won) or $0.00 (NO won)"
    https://docs.polymarket.us/concepts/market-data
    Note: "some markets include predefined settlement terms that differ from
    the standard $1/$0 structure" -- so a value strictly between 0 and 1 is
    legitimate but is NOT a binary outcome. This system only trades binary
    weather thresholds, so a non-binary settlement is classified UNSUPPORTED
    and left for a human rather than guessed at.
    https://docs.polymarket.us/learn/markets/contract-settlement

Book states
    MARKET_STATE_OPEN | PREOPEN | SUSPENDED | EXPIRED | TERMINATED | HALTED
    | MATCH_AND_CLOSE_AUCTION
    https://docs.polymarket.us/api-reference/markets/get-market-book
"""
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

# --- canonical outcomes ----------------------------------------------------
RESOLVED = "RESOLVED"        # authoritative binary outcome available
UNRESOLVED = "UNRESOLVED"    # not settled yet; keep holding, keep marking
UNSUPPORTED = "UNSUPPORTED"  # settled, but not at $1/$0 -- needs a human
MALFORMED = "MALFORMED"      # terminal-looking but unparseable -- needs a human

# Book/market states that mean "this will not trade again".
TERMINAL_BOOK_STATES = {
    "MARKET_STATE_EXPIRED", "MARKET_STATE_TERMINATED",
    "MARKET_STATE_MATCH_AND_CLOSE_AUCTION",
    "EXPIRED", "TERMINATED", "MATCH_AND_CLOSE_AUCTION",
}
# States that are merely paused. A halted market is NOT resolved.
PAUSED_BOOK_STATES = {
    "MARKET_STATE_SUSPENDED", "MARKET_STATE_HALTED", "MARKET_STATE_PREOPEN",
    "SUSPENDED", "HALTED", "PREOPEN",
}

# Fields that have carried the settlement price across payload shapes. The
# documented name is `settlement`; the others are accepted defensively because
# reading one extra key is free, whereas missing the real one strands a
# position open forever.
_SETTLEMENT_KEYS = ("settlement", "settlementValue", "settlementPrice",
                    "settledPrice", "resolutionValue")

_BINARY_TOL = 1e-6


@dataclass
class SettlementResult:
    status: str                       # RESOLVED | UNRESOLVED | UNSUPPORTED | MALFORMED
    yes_won: Optional[bool] = None    # only meaningful when RESOLVED
    settlement_value: Optional[float] = None
    source: str = ""                  # which endpoint/field decided this
    detail: str = ""

    @property
    def is_resolved(self) -> bool:
        return self.status == RESOLVED

    @property
    def needs_attention(self) -> bool:
        return self.status in (UNSUPPORTED, MALFORMED)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def payout_per_contract(self, side: str) -> Optional[float]:
        """$1.00 if this side won, $0.00 if it lost. None unless RESOLVED."""
        if not self.is_resolved or self.yes_won is None:
            return None
        side_won = self.yes_won if side == "YES" else (not self.yes_won)
        return 1.0 if side_won else 0.0


def _coerce_number(raw: Any) -> Optional[float]:
    """Accept int, float, or numeric string. Reject bool, None, and junk.

    `bool` is excluded deliberately: in Python `isinstance(True, int)` is True,
    and a payload carrying `settlement: true` must not silently become 1.0.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


def parse_settlement(payload: Optional[Dict[str, Any]],
                     *, source: str = "settlement endpoint") -> SettlementResult:
    """Parse a settlement payload into a canonical outcome. Never raises."""
    if payload is None:
        return SettlementResult(UNRESOLVED, source=source,
                                detail="no settlement payload (404 = unsettled)")
    if not isinstance(payload, dict):
        return SettlementResult(MALFORMED, source=source,
                                detail=f"expected an object, got {type(payload).__name__}")

    raw, key = None, None
    for k in _SETTLEMENT_KEYS:
        if k in payload:
            raw, key = payload[k], k
            break

    if key is None:
        return SettlementResult(UNRESOLVED, source=source,
                                detail=f"no settlement field present; keys="
                                       f"{sorted(payload)[:8]}")

    if raw is None:
        return SettlementResult(UNRESOLVED, source=f"{source}.{key}",
                                detail="settlement field present but null")

    val = _coerce_number(raw)
    if val is None:
        return SettlementResult(MALFORMED, source=f"{source}.{key}",
                                detail=f"settlement value {raw!r} is not numeric")

    if abs(val - 1.0) <= _BINARY_TOL:
        return SettlementResult(RESOLVED, yes_won=True, settlement_value=val,
                                source=f"{source}.{key}",
                                detail="settled at $1.00 -- YES won")
    if abs(val) <= _BINARY_TOL:
        return SettlementResult(RESOLVED, yes_won=False, settlement_value=val,
                                source=f"{source}.{key}",
                                detail="settled at $0.00 -- NO won")

    if 0.0 < val < 1.0:
        return SettlementResult(UNSUPPORTED, settlement_value=val,
                                source=f"{source}.{key}",
                                detail=(f"settled at ${val:.4f}, which is neither $1.00 "
                                        "nor $0.00. Polymarket US allows non-standard "
                                        "settlement terms; this system only models "
                                        "binary outcomes, so a human must resolve it."))

    return SettlementResult(MALFORMED, settlement_value=val, source=f"{source}.{key}",
                            detail=f"settlement value {val} is outside [0, 1]")


def looks_like_envelope(market: Optional[Dict[str, Any]]) -> bool:
    """True if this is an un-normalized {"market": {...}} response envelope.

    Callers must pass the INNER Market object (pmus_client.get_market_by_slug
    already unwraps it). Detecting the envelope here is a second line of
    defence: reading lifecycle fields off it returns None for every one, so a
    settled market would read as unresolved and strand its position silently.
    We refuse rather than unwrap, so the mistake surfaces instead of hiding.
    """
    return (isinstance(market, dict) and isinstance(market.get("market"), dict)
            and not any(k in market for k in ("closed", "active", "ep3Status")))


def market_looks_terminal(market: Optional[Dict[str, Any]],
                          book_state: Optional[str] = None) -> bool:
    """Does anything authoritative suggest this market will not trade again?

    Used ONLY to decide whether an unparseable outcome deserves an alert. It
    never closes a position on its own -- a market can be closed, archived, or
    expired and still not have published a settlement price.
    """
    if book_state and str(book_state).upper() in TERMINAL_BOOK_STATES:
        return True
    if isinstance(market, dict):
        if looks_like_envelope(market):
            raise ValueError(
                "market_looks_terminal received an un-normalized "
                '{"market": {...}} envelope. Pass the inner Market object; '
                "pmus_client.get_market_by_slug() already unwraps it.")
        if market.get("closed") is True:
            return True
        if market.get("active") is False:
            return True
        if str(market.get("ep3Status") or "").upper() in {"SETTLED", "RESOLVED", "FINAL"}:
            return True
    return False


def classify(settlement_payload: Optional[Dict[str, Any]],
             market_payload: Optional[Dict[str, Any]] = None,
             book_state: Optional[str] = None) -> SettlementResult:
    """Full classification from every authoritative signal available.

    The settlement endpoint is the authority. The market payload and book state
    only escalate an *unresolved* answer to MALFORMED when the market plainly
    will not trade again -- i.e. a position that can never be marked or exited
    and would otherwise sit open forever without anyone noticing.
    """
    res = parse_settlement(settlement_payload)

    # classify() must never raise -- it is the safety net. An envelope reaching
    # here is a wiring bug, so report it as needing attention rather than
    # letting it masquerade as "not settled yet".
    if looks_like_envelope(market_payload):
        return SettlementResult(
            MALFORMED, source="market payload",
            detail=('received an un-normalized {"market": {...}} envelope '
                    "instead of the inner Market object; lifecycle fields "
                    "could not be read, so terminality is unknown"))

    if res.status == UNRESOLVED and market_looks_terminal(market_payload, book_state):
        # Last resort: some payloads carry the price on the market object.
        fallback = parse_settlement(market_payload, source="market payload")
        if fallback.is_resolved or fallback.needs_attention:
            return fallback
        return SettlementResult(
            MALFORMED, source="terminal-state check",
            detail=("market appears terminal (book state "
                    f"{book_state!r}, closed={_get(market_payload, 'closed')}, "
                    f"active={_get(market_payload, 'active')}) but no settlement "
                    "price could be parsed from either endpoint"))
    return res


def _get(d: Optional[Dict[str, Any]], k: str) -> Any:
    return d.get(k) if isinstance(d, dict) else None
