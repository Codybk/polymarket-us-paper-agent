"""
Polymarket US fee model.

Source: https://docs.polymarket.us/fees  (verified 2026-08-24)

    Fee = Theta * C * p * (1 - p)

where C = number of contracts, p = trade price in dollars (0.01..0.99),
Theta = fee coefficient. Taker Theta = 0.06, Maker Theta = -0.0125 (rebate).

Fees are symmetric around p=0.50 and approach zero at the extremes.
Amounts round to the nearest cent (banker's rounding).

We deliberately model TAKER fees only. The paper engine never assumes we
earn a maker rebate, because resting an order is not guaranteed to fill and
assuming the rebate would flatter results.
"""
from decimal import Decimal, ROUND_HALF_EVEN

TAKER_THETA = Decimal("0.06")
MAKER_THETA = Decimal("-0.0125")


def _cents(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def fee(contracts: float, price: float, theta: Decimal = TAKER_THETA,
        fee_coefficient: float | None = None) -> float:
    """Fee in dollars for `contracts` at `price`.

    If the market supplies its own `feeCoefficient`, it overrides the default
    taker theta -- markets can carry bespoke coefficients and we must use the
    venue's number rather than our assumption.
    """
    if contracts <= 0:
        return 0.0
    p = Decimal(str(price))
    if p <= 0 or p >= 1:
        return 0.0
    th = Decimal(str(fee_coefficient)) if fee_coefficient is not None else theta
    c = Decimal(str(contracts))
    return float(_cents(th * c * p * (Decimal(1) - p)))


def taker_fee(contracts: float, price: float, fee_coefficient: float | None = None) -> float:
    return fee(contracts, price, TAKER_THETA, fee_coefficient)


def max_fee_per_contract(fee_coefficient: float | None = None) -> float:
    """Worst-case fee per contract (occurs at p=0.50)."""
    th = float(fee_coefficient) if fee_coefficient is not None else float(TAKER_THETA)
    return th * 0.25
