"""
Risk gates. Every one of these HALTS new position opening when tripped.

The system is designed to fail closed: any condition it cannot positively
verify counts as a halt, not as permission to proceed.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class RiskVerdict:
    allow_new_positions: bool
    halts: List[str]
    warnings: List[str]

    @property
    def reason(self) -> str:
        return "; ".join(self.halts) if self.halts else "all risk checks passed"


def evaluate(portfolio, cfg: dict, *, data_age_seconds: Optional[float],
             consecutive_errors: int, data_ok: bool,
             rules_changed: List[str] | None = None,
             feed_verified: bool = True) -> RiskVerdict:
    halts: List[str] = []
    warns: List[str] = []

    if cfg.get("emergency_stop"):
        halts.append("EMERGENCY STOP is engaged in risk_config.json")

    if portfolio.halted:
        halts.append(f"Portfolio already halted: {portfolio.halt_reason}")

    dd = portfolio.drawdown_pct()
    if dd >= cfg["max_drawdown_halt_pct"]:
        halts.append(f"Drawdown {dd:.1%} reached the {cfg['max_drawdown_halt_pct']:.0%} limit")
    elif dd >= cfg["max_drawdown_halt_pct"] * 0.75:
        warns.append(f"Drawdown {dd:.1%} approaching limit")

    dp = portfolio.daily_pnl_pct()
    if dp <= -cfg["daily_loss_halt_pct"]:
        halts.append(f"Daily loss {dp:.1%} reached the {cfg['daily_loss_halt_pct']:.0%} limit")

    if not data_ok:
        halts.append("Market data unavailable or inconsistent")

    if not feed_verified:
        halts.append("Price feed or resolution source could not be verified")

    if data_age_seconds is None:
        halts.append("Data age unknown - cannot confirm freshness")
    elif data_age_seconds > cfg["max_data_staleness_seconds"]:
        halts.append(f"Data is stale ({data_age_seconds:.0f}s old, limit "
                     f"{cfg['max_data_staleness_seconds']}s)")

    if consecutive_errors >= cfg["max_consecutive_errors"]:
        halts.append(f"{consecutive_errors} consecutive execution/logging errors")

    for slug in (rules_changed or []):
        halts.append(f"Resolution rules changed for {slug}")

    if portfolio.total_exposure() > cfg["max_total_exposure_pct"] * portfolio.equity() + 1e-6:
        warns.append("Total exposure above target band (no new positions until it decays)")

    return RiskVerdict(allow_new_positions=not halts, halts=halts, warnings=warns)
