"""
Entry point. One scan cycle.

Order of operations:
  1. load state, roll the trading day, verify audit chain
  2. mark open positions to market and settle anything resolved
  3. evaluate risk gates -- fail closed
  4. fetch markets, prefilter, value, size, and (paper) trade
  5. record every opportunity considered, with reasons
  6. persist, rebuild the dashboard, notify
"""
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Dict, List

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src import pmus_client as pm            # noqa: E402
from src import nws_client as nws            # noqa: E402
from src import scanner                      # noqa: E402
from src import settlement                   # noqa: E402
from src import discovery                    # noqa: E402
from src.book import best_no_ask, no_depth_within  # noqa: E402
from src.audit import AuditLog               # noqa: E402
from src.book import (Book, simulate_market_buy, simulate_market_buy_no,  # noqa: E402
                      simulate_market_sell, exit_fill)
from src.kelly import size_position, largest_fill_within_caps  # noqa: E402
from src.portfolio import Portfolio, Position  # noqa: E402
from src.risk import evaluate                # noqa: E402
from src import notify                       # noqa: E402
from src import dashboard                    # noqa: E402
from src import evaluation                   # noqa: E402
from src import final_report                 # noqa: E402

CFG_PATH = os.path.join(HERE, "config", "risk_config.json")
STATIONS_PATH = os.path.join(HERE, "config", "stations.json")
STATE_DIR = os.path.join(HERE, "state")
PORTFOLIO_PATH = os.path.join(STATE_DIR, "portfolio.json")
AUDIT_PATH = os.path.join(STATE_DIR, "audit.jsonl")
STATUS_PATH = os.path.join(STATE_DIR, "status.json")
SHORTLIST_PATH = os.path.join(STATE_DIR, "shortlist.json")
OPPS_PATH = os.path.join(STATE_DIR, "opportunities.json")
EVAL_PATH = os.path.join(STATE_DIR, "evaluation.json")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_cfg() -> Dict:
    with open(CFG_PATH) as fh:
        return json.load(fh)


def main() -> int:
    cfg = load_cfg()
    stations_cfg = json.load(open(STATIONS_PATH))
    os.makedirs(STATE_DIR, exist_ok=True)

    log = AuditLog(AUDIT_PATH)
    chain = log.verify()
    pf = Portfolio(PORTFOLIO_PATH, cfg["starting_bankroll"])

    status = {
        "scan_started_at": now(), "mode": cfg["mode"],
        "live_trading_enabled": cfg["live_trading_enabled"],
        "audit_chain": chain, "errors": [], "halts": [], "warnings": [],
    }
    opportunities: List[Dict] = []
    shortlist: List[Dict] = []
    errors: List[str] = []

    # ---- hard stop before anything else --------------------------------
    if cfg["live_trading_enabled"]:
        msg = ("live_trading_enabled is true but no live execution code exists "
               "in this repository. Refusing to run.")
        log.append("fatal", {"reason": msg})
        print(msg, file=sys.stderr)
        return 2

    # ---- evaluation window ---------------------------------------------
    # State lives in state/evaluation.json because the workflow commits
    # state/. Writing it to config/ would be lost on the next clean checkout
    # and the window would silently restart forever.
    evaluation.assert_config_immutable(cfg)
    ev_state, stamped = evaluation.stamp_start_if_needed(EVAL_PATH, cfg)
    if stamped:
        log.append("evaluation_started", {
            "start": ev_state["started_at"], "hours": ev_state["hours"],
            "state_file": "state/evaluation.json"})
        notify.send(cfg, f"Evaluation started. Running ~every 10 minutes for "
                         f"{ev_state['hours']}h, then auto-stopping.")
    win = evaluation.window(ev_state)
    status["evaluation"] = win

    if win["complete"]:
        status["evaluation_complete"] = True
        json.dump(status, open(STATUS_PATH, "w"), indent=2, default=str)
        print("Evaluation already complete; nothing to do.")
        return 0

    if win["expired"]:
        # Mark complete FIRST so the final report -- which reads the committed
        # evaluation state -- records the window as closed rather than
        # snapshotting it a moment before it was.
        evaluation.mark_complete(EVAL_PATH, cfg)
        rep = final_report.build(HERE)
        log.append("evaluation_complete", {
            "elapsed_hours": win["elapsed_hours"],
            "runs_observed": win["runs_observed"],
            "scans": rep["scans_completed"],
            "positions_opened": rep["positions_opened"],
            "positions_resolved": rep["positions_resolved"],
            "net_pnl": rep["net_pnl"], "verdict": rep["verdict"]})
        notify.send(cfg, "48-hour evaluation complete. Scanner disabling itself.\n"
                         f"{rep['verdict']}")
        status["evaluation_complete"] = True
        status["evaluation"] = evaluation.window(evaluation.load(EVAL_PATH, cfg))
        pf.save()
        json.dump(status, open(STATUS_PATH, "w"), indent=2, default=str)
        dashboard.build(HERE)
        print("EVALUATION_COMPLETE")
        return 0

    if pf.roll_day_if_needed():
        log.append("day_rolled", {"day_start_equity": pf.day_start_equity})

    log.append("scan_started", {"equity": pf.equity(), "open": len(pf.open_positions),
                                "audit_chain_ok": chain["ok"]})

    # ---- 1. refresh marks and settle -----------------------------------
    data_ok = True
    newest_data_ts = None
    marks_verified = True          # every open position re-marked cleanly?
    settlement_alerts = []

    for pos in list(pf.open_positions):
        # -------------------------------------------------------------------
        # SETTLEMENT DETECTION IS INDEPENDENT OF THE ORDER BOOK.
        # A resolved market may stop serving a book entirely, or report a
        # terminal state this code does not enumerate. Gating the resolution
        # lookup behind a successful book fetch left resolved positions open
        # forever, holding a stale mark the final report counted as a result.
        # So: look up settlement FIRST, for every open position, always.
        # -------------------------------------------------------------------
        book_state = None
        bk = None
        try:
            bk = Book.from_api(pm.get_market_book(pos.slug))
            book_state = bk.state
            newest_data_ts = bk.transact_time or newest_data_ts
        except Exception as e:  # noqa: BLE001
            marks_verified = False
            errors.append(f"book {pos.slug}: {e}")

        settle_payload = market_payload = None
        lookup_failed = False
        try:
            settle_payload = pm.get_market_settlement(pos.slug)
        except Exception as e:  # noqa: BLE001
            lookup_failed = True
            errors.append(f"settlement lookup {pos.slug}: {e}")
        try:
            market_payload = pm.get_market_by_slug(pos.slug)
        except Exception as e:  # noqa: BLE001
            lookup_failed = True
            errors.append(f"market lookup {pos.slug}: {e}")

        verdict_s = settlement.classify(settle_payload, market_payload, book_state)

        if verdict_s.is_resolved:
            # A position is closed ONLY on an explicit authoritative outcome.
            payout = verdict_s.payout_per_contract(pos.side)
            pf.close_position(pos, payout, 0.0,
                              outcome=1 if verdict_s.yes_won else 0,
                              reason=verdict_s.detail)
            pos.settlement_pending = False
            pos.resolution_error = ""
            pos.settlement_detail = verdict_s.to_dict()
            log.append("position_resolved", {
                "decision_id": pos.decision_id, "slug": pos.slug,
                "side": pos.side, "yes_won": verdict_s.yes_won,
                "payout_per_contract": payout,
                "settlement_value": verdict_s.settlement_value,
                "settlement_source": verdict_s.source,
                "realized_pnl": pos.realized_pnl,
                "predicted_prob_at_entry": pos.predicted_prob},
                decision_id=pos.decision_id)
            notify.send(cfg, f"Position resolved: {pos.question}\n"
                             f"{pos.side} {'won' if payout else 'lost'}, "
                             f"P&L ${pos.realized_pnl:+.2f}")
            continue

        if verdict_s.needs_attention:
            # Terminal-looking but unparseable. PRESERVE the position, flag it,
            # and make sure the final report does not count its stale mark.
            pos.settlement_pending = True
            pos.resolution_error = f"{verdict_s.status}: {verdict_s.detail}"
            pos.settlement_detail = verdict_s.to_dict()
            marks_verified = False
            msg = (f"{pos.slug}: {verdict_s.status} -- {verdict_s.detail}")
            settlement_alerts.append(msg)
            log.append("settlement_error", {
                "decision_id": pos.decision_id, "slug": pos.slug,
                "side": pos.side, "status": verdict_s.status,
                "detail": verdict_s.detail, "source": verdict_s.source,
                "book_state": book_state,
                "action": "position preserved; not closed; excluded from results"},
                decision_id=pos.decision_id)
            notify.send(cfg, "SETTLEMENT NEEDS ATTENTION\n" + msg)
            continue

        # Unresolved: clear any previous flag and mark to market if we can.
        if pos.settlement_pending and not lookup_failed:
            pos.settlement_pending = False
            pos.resolution_error = ""
        if bk is None:
            continue
        try:
            ex = exit_fill(bk, pos.side, pos.contracts,
                           fee_coefficient=pos.fee_coefficient)
            if ex.filled > 0:
                pos.mark_price = ex.net_cost / ex.filled
                pos.mark_is_fallback = False
                pos.mark_source = (f"executable {pos.side} exit, "
                                   f"{ex.filled:.0f} contracts, fee ${ex.fee:.4f}")
            else:
                touch = bk.best_bid if pos.side == "YES" else (
                    (1.0 - bk.best_ask) if bk.best_ask is not None else None)
                pos.mark_price = touch if touch is not None else pos.avg_price
                pos.mark_is_fallback = True
                pos.mark_source = ("FALLBACK: no executable exit liquidity; "
                                   + ("touch price" if touch is not None
                                      else "entry price"))
                marks_verified = False
        except Exception as e:  # noqa: BLE001
            marks_verified = False
            errors.append(f"mark {pos.slug}: {e}")

    # The high-water mark moves only on a fully verified, side-aware,
    # fee-inclusive mark-to-market pass.
    if pf.open_positions:
        raised = pf.update_high_water_mark(marks_verified and data_ok)
        if raised:
            log.append("high_water_mark_raised", {"peak_bankroll": pf.peak_bankroll})
    status["marks_verified"] = marks_verified
    status["settlement_alerts"] = settlement_alerts
    status["settlement_pending"] = [
        {"slug": p.slug, "error": p.resolution_error} for p in pf.open_positions
        if p.settlement_pending]

    # ---- 2. risk gates --------------------------------------------------
    prev_status = {}
    if os.path.exists(STATUS_PATH):
        try:
            prev_status = json.load(open(STATUS_PATH))
        except Exception:  # noqa: BLE001
            prev_status = {}
    consecutive_errors = prev_status.get("consecutive_errors", 0)

    # ---- 3. discover the market universe --------------------------------
    # Weather is discovered EXPLICITLY and EXHAUSTIVELY first, using the
    # documented category/tag filters. The broad listing runs afterwards and is
    # additive only. The first live scan walked 4,000 unfiltered markets -- all
    # sports, zero weather -- because 40 pages x 100 was the cap; a targeted
    # query returns tens of markets and genuinely completes.
    markets: List[Dict] = []
    disc = None
    try:
        disc = discovery.discover(
            page_size=cfg.get("market_page_size", 100),
            weather_max_pages=cfg.get("weather_discovery_max_pages", 50),
            broad_max_pages=cfg.get("broad_scan_max_pages", 200),
            include_broad=cfg.get("broad_scan_enabled", True),
            use_search=cfg.get("weather_search_enabled", True),
        )
        markets = disc.universe
        status["discovery"] = disc.to_dict()
        data_age = 0.0

        if not disc.weather:
            errors.append("weather discovery returned NO markets -- the weather "
                          "strategy has nothing to evaluate")
        if not disc.weather_complete:
            status["warnings"].append(
                "weather discovery did not run to exhaustion; some weather "
                "markets may be unseen")
        if disc.broad is None:
            status["scan_scope"] = "WEATHER_ONLY"
        elif not disc.broad.exhausted:
            status["warnings"].append(
                f"broad market scan is TRUNCATED, not complete "
                f"({len(disc.broad.markets)} markets over "
                f"{disc.broad.pages_fetched} pages): "
                f"{disc.broad.error or 'safety ceiling reached'}")
        log.append("discovery", disc.to_dict())
    except Exception as e:  # noqa: BLE001
        data_ok = False
        data_age = None
        errors.append(f"market discovery: {e}")

    verdict = evaluate(pf, cfg,
                       data_age_seconds=(0.0 if data_ok else None),
                       consecutive_errors=consecutive_errors + (1 if errors else 0),
                       data_ok=data_ok, feed_verified=data_ok)
    status["halts"] = verdict.halts
    status["warnings"] = list(status.get("warnings", [])) + list(verdict.warnings)

    if verdict.halts:
        pf.halted = True
        pf.halt_reason = verdict.reason
        log.append("risk_halt", {"halts": verdict.halts})
        notify.send(cfg, "RISK HALT — no new positions.\n" + verdict.reason)

    # ---- 4. evaluate every market ---------------------------------------
    for m in markets:
        slug = m.get("slug")
        if not slug:
            continue
        question = m.get("question") or m.get("title") or slug
        category = scanner.classify(m)
        rec = {"slug": slug, "question": question, "category": category,
               "url": pm.market_url(slug), "considered_at": now(),
               "traded": False, "reason": "", "expiration": m.get("endDate"),
               "volume": m.get("volume")}

        ok, why = scanner.prefilter(m, cfg)
        if not ok:
            rec["reason"] = f"Skipped: {why}"
            opportunities.append(rec)
            continue

        # Non-weather markets: we have no defensible automated fair value.
        if category != "weather":
            rec["reason"] = ("Passed quantitative filters but this system has no "
                             "defensible automated fair-value model for non-weather "
                             "markets. Shortlisted for human/Claude review; never auto-traded.")
            shortlist.append(rec)
            opportunities.append(rec)
            continue

        val = scanner.value_weather_market(m, stations_cfg)
        if not val or not val.get("ok"):
            rec["reason"] = f"Skipped: {val.get('reason') if val else 'valuation failed'}"
            rec["evidence"] = (val or {}).get("evidence", [])
            opportunities.append(rec)
            continue

        try:
            bk = Book.from_api(pm.get_market_book(slug))
        except Exception as e:  # noqa: BLE001
            rec["reason"] = f"Skipped: order book unavailable ({e})"
            errors.append(f"book {slug}: {e}")
            opportunities.append(rec)
            continue

        sok, swhy = scanner.spread_ok(bk, cfg)
        rec.update({"best_bid": bk.best_bid, "best_ask": bk.best_ask,
                    "spread": bk.spread, "book_state": bk.state,
                    "depth_at_ask": bk.depth_within("BUY", (bk.best_ask or 0) + 0.02),
                    "station": val["station"], "target_date": val["target_date"],
                    "settlement_source": val["settlement_source"],
                    "resolution_rules": val["rules_text"][:600],
                    "evidence": val["evidence"], "estimate": val["estimate"]})

        if not sok:
            rec["reason"] = f"Skipped: {swhy}"
            opportunities.append(rec)
            continue

        est = val["estimate"]
        prob = est["probability"]
        rec["fair_probability"] = prob
        rec["confidence_range"] = [est["prob_low"], est["prob_high"]]

        depth_yes = bk.depth_within("BUY", (bk.best_ask or 0) + 0.02)
        depth_no = no_depth_within(bk, (best_no_ask(bk) or 0) + 0.02)
        if max(depth_yes, depth_no) < cfg["min_book_depth_contracts"]:
            rec["reason"] = (f"Skipped: best-side depth is only "
                             f"{max(depth_yes, depth_no):.0f} contracts near the touch, "
                             f"below the {cfg['min_book_depth_contracts']} minimum")
            opportunities.append(rec)
            continue

        probe = max(cfg["min_book_depth_contracts"],
                    cfg["max_position_pct"] * pf.equity() / 0.5)
        edge = scanner.compute_edge(bk, prob, est["prob_low"], est["prob_high"],
                                    probe, m.get("feeCoefficient"))
        rec["edge"] = edge
        if not edge.get("tradeable"):
            rec["reason"] = f"Skipped: {edge.get('reason')}"
            opportunities.append(rec)
            continue

        side = edge["side"]
        rec["recommended_side"] = side
        rec["net_edge_pp"] = edge["net_edge_pp"]
        rec["conservative_edge_pp"] = edge["conservative_edge_pp"]
        rec["estimated_fees"] = edge["fee"]
        rec["estimated_slippage"] = edge["slippage_vs_touch"]

        if edge["net_edge_pp"] < cfg["min_net_edge_pp"]:
            rec["reason"] = (f"Skipped: best side ({side}) nets "
                             f"{edge['net_edge_pp']:.1f}pp after fees and slippage, "
                             f"below the {cfg['min_net_edge_pp']}pp threshold")
            opportunities.append(rec)
            continue

        if edge["conservative_edge_pp"] < cfg["min_net_edge_pp"]:
            rec["reason"] = (f"Skipped: {side} edge survives at the point estimate "
                             f"({edge['net_edge_pp']:.1f}pp) but collapses to "
                             f"{edge['conservative_edge_pp']:.1f}pp at the conservative "
                             f"end of the confidence range")
            opportunities.append(rec)
            continue

        cluster = scanner.cluster_key(m, val["station"], val["target_date"])
        rec["cluster"] = cluster

        if any(p.slug == slug for p in pf.open_positions):
            rec["reason"] = "Skipped: already holding this market (never averaging in)"
            opportunities.append(rec)
            continue

        if not verdict.allow_new_positions:
            rec["reason"] = f"Skipped: risk halt in effect — {verdict.reason}"
            opportunities.append(rec)
            continue

        sizing = size_position(
            prob_conservative=edge["probability_conservative"],
            price=edge["all_in_cost_per_contract"],
            bankroll=pf.equity(), cfg=cfg,
            current_total_exposure=pf.total_exposure(),
            current_cluster_exposure=pf.cluster_exposure(cluster),
            venue_min_contracts=float(m.get("minimumTradeQty") or cfg["venue_min_contracts"]),
        )
        rec["sizing"] = sizing.to_dict()

        if not sizing.approved:
            rec["reason"] = f"Skipped: {sizing.reason}"
            opportunities.append(rec)
            continue

        # Enforce the caps on the ACTUAL fill, whose cost includes the fee.
        # The sizer worked from a probe; the realised fill can differ, and a
        # fee must never be what pushes real exposure past a hard cap.
        sim = simulate_market_buy if side == "YES" else simulate_market_buy_no
        fill, n = largest_fill_within_caps(
            sim, bk, sizing.contracts, bankroll=pf.equity(), cfg=cfg,
            current_total_exposure=pf.total_exposure(),
            current_cluster_exposure=pf.cluster_exposure(cluster),
            fee_coefficient=m.get("feeCoefficient"),
            venue_min_contracts=float(m.get("minimumTradeQty") or cfg["venue_min_contracts"]),
        )
        if fill is None or fill.filled <= 0:
            rec["reason"] = ("Skipped: no whole-contract size fits every exposure cap "
                             "once the fee is included in the all-in cost")
            opportunities.append(rec)
            continue
        rec["contracts_after_cap_fit"] = n

        decision = log.append("paper_trade_opened", {
            "slug": slug, "question": question, "url": pm.market_url(slug),
            "side": side, "contracts": fill.filled, "avg_price": fill.avg_price,
            "all_in_cost": fill.net_cost, "fee": fill.fee,
            "slippage_vs_touch": fill.slippage_vs_touch,
            "best_bid_at_decision": bk.best_bid, "best_ask_at_decision": bk.best_ask,
            "spread_at_decision": bk.spread, "book_transact_time": bk.transact_time,
            "fair_probability": prob, "confidence_range": [est["prob_low"], est["prob_high"]],
            "side_probability": edge["probability"],
            "side_probability_conservative": edge["probability_conservative"],
            "sides_evaluated": {k: {kk: vv for kk, vv in v.items() if kk != "fill"}
                                for k, v in edge.get("sides", {}).items()},
            "target_date": val["target_date"], "date_note": val.get("date_note"),
            "cli": val.get("cli"),
            "net_edge_pp": edge["net_edge_pp"],
            "conservative_edge_pp": edge["conservative_edge_pp"],
            "method": est["method"], "station": val["station"],
            "settlement_source": val["settlement_source"],
            "resolution_rules": val["rules_text"][:2000],
            "evidence": val["evidence"], "sizing": sizing.to_dict(),
            "levels_consumed": fill.levels_consumed,
        })

        pos = Position(
            decision_id=decision["id"], slug=slug, question=question,
            url=pm.market_url(slug), side=side, contracts=fill.filled,
            avg_price=fill.avg_price, stake=fill.net_cost, entry_fee=fill.fee,
            opened_at=now(), category="weather", cluster=cluster,
            predicted_prob=edge["probability"],
            predicted_prob_low=edge["probability_conservative"],
            predicted_prob_high=est["prob_high"] if side == "YES" else 1.0 - est["prob_low"], edge_pp_at_entry=edge["net_edge_pp"],
            evidence=val["evidence"], resolution_rules=val["rules_text"][:2000],
            expiration=m.get("endDate"),
            fee_coefficient=m.get("feeCoefficient"),
        )
        # Initial mark uses the SAME executable, side-aware, fee-inclusive exit
        # path as every later mark. Using a raw bid here valued a fresh NO
        # position at the YES bid -- roughly (1 - its true worth) -- which
        # inflated first-scan equity above the starting bankroll purely as a
        # bookkeeping artefact.
        entry_ex = exit_fill(bk, side, fill.filled,
                             fee_coefficient=m.get("feeCoefficient"))
        if entry_ex.filled > 0:
            pos.mark_price = entry_ex.net_cost / entry_ex.filled
            pos.mark_is_fallback = False
            pos.mark_source = (f"executable {side} exit at entry, "
                               f"{entry_ex.filled:.0f} contracts, "
                               f"fee ${entry_ex.fee:.4f}")
        else:
            touch = bk.best_bid if side == "YES" else (
                (1.0 - bk.best_ask) if bk.best_ask is not None else None)
            pos.mark_price = touch if touch is not None else fill.avg_price
            pos.mark_is_fallback = True
            pos.mark_source = ("FALLBACK at entry: no executable exit liquidity; "
                               + ("touch price" if touch is not None else "entry price"))
        try:
            pf.open_position(pos)
            rec["traded"] = True
            rec["reason"] = (f"TRADED {side}: net edge {edge['net_edge_pp']:.1f}pp "
                             f"(conservative {edge['conservative_edge_pp']:.1f}pp), "
                             f"{fill.filled:.0f} contracts at avg {fill.avg_price:.3f}, "
                             f"all-in ${fill.net_cost:.2f} incl. ${fill.fee:.2f} fee, "
                             f"sized by {sizing.binding_constraint}")
            notify.send(cfg, f"PAPER TRADE\n{question}\n"
                             f"{fill.filled:.0f} {side} @ {fill.avg_price:.3f} "
                             f"(${fill.net_cost:.2f})\n"
                             f"Fair {prob:.1%}, net edge {edge['net_edge_pp']:.1f}pp\n"
                             f"{pm.market_url(slug)}")
        except ValueError as e:
            rec["reason"] = f"Skipped: {e}"

        opportunities.append(rec)

    # ---- 5. persist ------------------------------------------------------
    pf.save()
    json.dump(opportunities, open(OPPS_PATH, "w"), indent=2, default=str)
    json.dump(shortlist, open(SHORTLIST_PATH, "w"), indent=2, default=str)

    status.update({
        "scan_finished_at": now(),
        "markets_scanned": len(markets),
        "weather_markets_discovered": len(disc.weather) if disc else 0,
        "weather_discovery_complete": bool(disc.weather_complete) if disc else False,
        "broad_scan_exhausted": bool(disc.broad.exhausted) if (disc and disc.broad) else None,
        "broad_scan_enabled": bool(cfg.get("broad_scan_enabled", False)),
        "scan_scope": ("WEATHER_ONLY" if not cfg.get("broad_scan_enabled", False)
                       else "WEATHER_PLUS_BROAD"),
        "opportunities_considered": len(opportunities),
        "traded_this_scan": sum(1 for o in opportunities if o.get("traded")),
        "shortlisted_for_review": len(shortlist),
        "errors": errors,
        "consecutive_errors": (consecutive_errors + 1) if errors else 0,
        "equity": pf.equity(), "cash": pf.cash,
        "realized_pnl": pf.realized_pnl(), "unrealized_pnl": pf.unrealized_pnl(),
        "drawdown_pct": pf.drawdown_pct(), "daily_pnl_pct": pf.daily_pnl_pct(),
        "open_positions": len(pf.open_positions),
        "halted": pf.halted, "halt_reason": pf.halt_reason,
        "last_successful_scan": now() if data_ok else prev_status.get("last_successful_scan"),
    })
    json.dump(status, open(STATUS_PATH, "w"), indent=2, default=str)
    log.append("scan_finished", {k: status[k] for k in
                                 ("markets_scanned", "opportunities_considered",
                                  "traded_this_scan", "equity", "errors")})

    if errors:
        notify.send(cfg, "Scan completed with errors:\n" + "\n".join(errors[:5]))

    dashboard.build(HERE)
    print(json.dumps({k: status[k] for k in
                      ("markets_scanned", "opportunities_considered",
                       "traded_this_scan", "equity", "halted")}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        try:
            AuditLog(AUDIT_PATH).append("fatal_error", {"traceback": traceback.format_exc()[:4000]})
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
