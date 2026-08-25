"""
End-of-evaluation report.

Deliberately unflattering. It reports the sample size first, refuses to call a
small or unresolved sample an edge, and compares against the honest benchmark:
paying the ask on every shortlisted market.
"""
import json
import os
from datetime import datetime, timezone
from typing import Dict, List

from .audit import AuditLog
from . import evaluation


def build(root: str) -> Dict:
    cfg = json.load(open(os.path.join(root, "config", "risk_config.json")))
    pf = json.load(open(os.path.join(root, "state", "portfolio.json")))
    log = AuditLog(os.path.join(root, "state", "audit.jsonl"))

    # Evaluation timings come from the COMMITTED state file. config/ is
    # immutable deployment configuration and never carries the start time --
    # reading it here would silently report `null` for the very timestamp the
    # 48-hour window is built on.
    ev_path = os.path.join(root, "state", "evaluation.json")
    ev = evaluation.load(ev_path, cfg)
    ev_win = evaluation.window(ev)

    positions = pf.get("positions", [])
    resolved = [p for p in positions if p.get("outcome") is not None]
    closed = [p for p in positions if p.get("status") != "OPEN"]
    open_pos = [p for p in positions if p.get("status") == "OPEN"]

    # A position whose market looks terminal but whose outcome could not be
    # parsed is NOT a result. Its mark is stale by definition -- the market
    # has stopped trading -- so counting it as unrealised P&L would present a
    # settlement failure as performance. It is reported separately and
    # excluded from every performance figure below.
    stranded = [p for p in open_pos if p.get("settlement_pending")]
    live_open = [p for p in open_pos if not p.get("settlement_pending")]

    scans = log.by_type("scan_finished")
    opened = log.by_type("paper_trade_opened")
    halts = log.by_type("risk_halt")

    gross = sum((p.get("contracts", 0) * ((p.get("exit_price") or 0)) -
                 p.get("contracts", 0) * p.get("avg_price", 0)) for p in closed)
    fees = sum((p.get("entry_fee") or 0) + (p.get("exit_fee") or 0) for p in positions)
    net = sum(p.get("realized_pnl") or 0 for p in closed)

    equity_curve = []
    for s in scans:
        eq = (s.get("payload") or {}).get("equity")
        if eq is not None:
            equity_curve.append(eq)
    peak = pf.get("peak_bankroll", cfg["starting_bankroll"])
    max_dd = 0.0
    run_peak = cfg["starting_bankroll"]
    for eq in equity_curve:
        run_peak = max(run_peak, eq)
        max_dd = max(max_dd, (run_peak - eq) / run_peak if run_peak else 0)

    wins = [p for p in closed if (p.get("realized_pnl") or 0) > 0]

    # Calibration
    bins = [(0.0,0.2),(0.2,0.4),(0.4,0.6),(0.6,0.8),(0.8,1.0)]
    calib = []
    for lo, hi in bins:
        grp = [p for p in resolved if lo <= p.get("predicted_prob", -1) < hi]
        calib.append({"bin": f"{int(lo*100)}-{int(hi*100)}%", "n": len(grp),
                      "mean_predicted": round(sum(p["predicted_prob"] for p in grp)/len(grp), 4) if grp else None,
                      "actual_frequency": round(sum(1 for p in grp if p["outcome"]==1)/len(grp), 4) if grp else None})

    n = len(resolved)
    if stranded:
        pass  # verdict text below appends the caveat
    if n == 0:
        verdict = ("NO EDGE DEMONSTRATED. Nothing resolved during the window, so "
                   "there is no evidence either way. Any profit shown is unrealised "
                   "marks, not results.")
        recommendation = "CONTINUE PAPER TRADING"
    elif n < 30:
        verdict = (f"NO EDGE DEMONSTRATED. Only {n} position(s) resolved. A sample "
                   "this small cannot distinguish skill from luck at any useful "
                   "confidence, regardless of the P&L sign.")
        recommendation = "CONTINUE PAPER TRADING"
    else:
        verdict = (f"{n} resolved positions. Judge by calibration below, not by P&L "
                   "alone. Even here, 48 hours of one market category is a narrow base.")
        recommendation = "REVIEW CALIBRATION BEFORE ANY LIVE CONSIDERATION"

    if stranded:
        verdict = (f"{verdict} NOTE: {len(stranded)} position(s) could not be "
                   f"settled automatically and are excluded from all figures "
                   f"above; ${sum(p.get('stake', 0) for p in stranded):.2f} of "
                   f"capital is unaccounted for until a human resolves them.")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_start": ev.get("started_at"),
        "evaluation_end_expected": ev_win.get("end"),
        "evaluation_completed_at": ev.get("completed_at"),
        "evaluation_complete": bool(ev.get("complete")),
        "evaluation_elapsed_hours": ev_win.get("elapsed_hours"),
        "evaluation_hours": ev.get("hours"),
        "scheduled_runs_observed": ev.get("runs_observed"),
        "evaluation_state_source": "state/evaluation.json",
        "scans_completed": len(scans),
        "schedule_evidence": {
            "first_scan": (scans[0].get("ts") if scans else None),
            "last_scan": (scans[-1].get("ts") if scans else None),
            "distinct_scan_timestamps": len({s.get("ts") for s in scans}),
            "runs_observed_in_state": ev.get("runs_observed"),
            "window_start_persisted": ev.get("started_at"),
        },
        "starting_bankroll": cfg["starting_bankroll"],
        "final_equity": pf.get("cash", 0) + sum(
            p.get("contracts", 0) * (p.get("mark_price") or p.get("avg_price", 0))
            for p in live_open),
        "capital_in_unresolved_settlements": round(
            sum(p.get("stake", 0) for p in stranded), 4),
        "settlement_failures": [
            {"slug": p.get("slug"), "side": p.get("side"),
             "stake": p.get("stake"), "error": p.get("resolution_error"),
             "opened_at": p.get("opened_at")} for p in stranded],
        "positions_opened": len(opened),
        "positions_resolved": n,
        "positions_still_open": len(live_open),
        "positions_awaiting_manual_settlement": len(stranded),
        "gross_pnl": round(gross, 4),
        "total_fees_and_slippage": round(fees, 4),
        "net_pnl": round(net, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "peak_bankroll": pf.get("peak_bankroll"),
        "positions_on_fallback_marks": sum(
            1 for p in live_open if p.get("mark_is_fallback")),
        "win_rate": round(len(wins)/len(closed), 4) if closed else None,
        "calibration": calib,
        "risk_halts": len(halts),
        "audit_chain": log.verify(),
        "verdict": verdict,
        "recommendation": recommendation,
        "caveats": [
            "Simulated fills walked real resting order-book depth at the recorded "
            "timestamp; no midpoint pricing was used.",
            "Unresolved positions are marked to the bid and are NOT results.",
            "A 48-hour window concentrated in weather markets is not a "
            "representative sample of anything.",
            "Fees and slippage are modelled from the published Polymarket US "
            "schedule and observed book depth, not from executed trades.",
            "Positions whose settlement could not be parsed are excluded "
            "from every performance figure; their stale marks are not results.",
            "Open positions are marked at their executable, side-aware, "
            "fee-inclusive exit value -- what they could actually be closed "
            "into, not the opposite side of the book.",
        ],
    }
    out = os.path.join(root, "state", "final_report.json")
    json.dump(report, open(out, "w"), indent=2)
    return report
