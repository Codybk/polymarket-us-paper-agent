"""
Fractional Kelly sizing with hard portfolio caps.

Binary contract mechanics on Polymarket US:
  Buy 1 contract at price p. It settles at $1.00 (win) or $0.00 (loss).
  Stake per contract = p. Net profit if win = (1 - p) - fee. Loss if lose = p + fee.

Kelly for a binary bet with net odds b = (1-p)/p and win prob q:
      f* = (q*b - (1-q)) / b   ==   (q - p) / (1 - p)

We apply:
  * one-quarter Kelly at most
  * a conservative probability (lower edge of the confidence band), so sizing
    is driven by the pessimistic estimate, not the point estimate
  * per-position cap, total-exposure cap, correlated-cluster cap
  * refusal if the venue minimum order exceeds the permitted size
"""
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class SizingResult:
    contracts: float
    stake: float
    full_kelly_fraction: float
    used_kelly_fraction: float
    binding_constraint: str
    permitted_stake: float
    approved: bool
    reason: str

    def to_dict(self):
        return asdict(self)


def full_kelly_fraction(prob: float, price: float) -> float:
    """f* for buying at `price` with true probability `prob`. Negative = no bet."""
    if not (0.0 < price < 1.0):
        return 0.0
    return (prob - price) / (1.0 - price)


def size_position(
    *,
    prob_conservative: float,
    price: float,
    bankroll: float,
    cfg: dict,
    current_total_exposure: float,
    current_cluster_exposure: float,
    venue_min_contracts: float = 1.0,
    tick: float = 0.01,
) -> SizingResult:
    """Return the permitted paper position, or a refusal with the reason."""
    kf = full_kelly_fraction(prob_conservative, price)
    if kf <= 0:
        return SizingResult(0, 0, kf, 0, "no_edge_at_conservative_prob", 0, False,
                            "Conservative probability does not exceed the ask; no Kelly edge.")

    frac = min(kf * cfg["kelly_fraction"], cfg["kelly_fraction"])
    kelly_stake = frac * bankroll

    per_pos_cap = cfg["max_position_pct"] * bankroll
    total_room = max(0.0, cfg["max_total_exposure_pct"] * bankroll - current_total_exposure)
    cluster_room = max(0.0, cfg["max_correlated_pct"] * bankroll - current_cluster_exposure)

    caps = {
        "quarter_kelly": kelly_stake,
        "per_position_cap": per_pos_cap,
        "total_exposure_cap": total_room,
        "correlated_exposure_cap": cluster_room,
    }
    binding = min(caps, key=caps.get)
    permitted = caps[binding]

    if permitted <= 0:
        return SizingResult(0, 0, kf, frac, binding, 0, False,
                            f"No capacity remaining under {binding}.")

    contracts = permitted / price
    # Contracts trade in whole units on the venue's minimum increment.
    contracts = float(int(contracts))

    if contracts < venue_min_contracts:
        return SizingResult(
            0, 0, kf, frac, binding, round(permitted, 4), False,
            f"Permitted stake ${permitted:.2f} buys {permitted/price:.2f} contracts, "
            f"below the venue minimum of {venue_min_contracts}. Skipping per policy.")

    stake = contracts * price
    return SizingResult(
        contracts=contracts,
        stake=round(stake, 4),
        full_kelly_fraction=round(kf, 6),
        used_kelly_fraction=round(frac, 6),
        binding_constraint=binding,
        permitted_stake=round(permitted, 4),
        approved=True,
        reason=f"Sized by {binding}.",
    )


def fit_fill_to_caps(fill, *, bankroll: float, cfg: dict,
                     current_total_exposure: float,
                     current_cluster_exposure: float):
    """Verify an ACTUAL fill respects the hard caps, on all-in cost.

    Sizing is computed from a probe fill, but the real fill can differ -- a
    smaller order may clear at a better average, a larger one at worse, and
    the fee is a function of the achieved price. The binding number for a risk
    cap is the cash actually committed, `fill.net_cost`, which INCLUDES the
    fee. Checking the pre-fee stake would let fees push real exposure past a
    cap that the sizer believed it had respected.

    Returns (ok, permitted_ceiling, reason).
    """
    ceiling = min(
        cfg["max_position_pct"] * bankroll,
        max(0.0, cfg["max_total_exposure_pct"] * bankroll - current_total_exposure),
        max(0.0, cfg["max_correlated_pct"] * bankroll - current_cluster_exposure),
    )
    if fill.net_cost <= ceiling + 1e-9:
        return True, round(ceiling, 6), ""
    return False, round(ceiling, 6), (
        f"all-in cost ${fill.net_cost:.4f} (incl. ${fill.fee:.4f} fee) exceeds the "
        f"permitted ${ceiling:.4f}")


def largest_fill_within_caps(simulate, book, max_contracts: float, *,
                             bankroll: float, cfg: dict,
                             current_total_exposure: float,
                             current_cluster_exposure: float,
                             fee_coefficient=None,
                             venue_min_contracts: float = 1.0):
    """Largest whole-contract fill whose ALL-IN cost fits every cap.

    Walks down from the requested size rather than trusting the probe estimate,
    so the caps hold on realised cost including fees.
    """
    n = int(max_contracts)
    while n >= venue_min_contracts and n >= 1:
        fill = simulate(book, n, fee_coefficient=fee_coefficient)
        if fill.filled >= n - 1e-9:
            ok, ceiling, _ = fit_fill_to_caps(
                fill, bankroll=bankroll, cfg=cfg,
                current_total_exposure=current_total_exposure,
                current_cluster_exposure=current_cluster_exposure)
            if ok:
                return fill, n
        n -= 1
    return None, 0
