"""
End-to-end scan with the network mocked out.

Feeds a realistic market universe (one clearly mispriced weather market, one
fairly priced, one unparseable, one illiquid, one non-weather) through the
real run_scan.main() and asserts the engine behaves.
"""
import json, os, sys, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import pmus_client, nws_client

from datetime import datetime as _dt, timedelta as _td
from zoneinfo import ZoneInfo as _ZI

# Each station's calendar date IN ITS OWN TIMEZONE. The model reasons in local
# time (a daily high belongs to a local day), so fixtures pinned to UTC dates
# flip meaning as the UTC clock crosses midnight and make this test flaky.
_TZ = {"KNYC": "America/New_York", "KMDW": "America/Chicago",
       "KMIA": "America/New_York", "KSFO": "America/Los_Angeles",
       "KLAX": "America/Los_Angeles"}


def _local_today(station):
    return _dt.now(_ZI(_TZ[station])).date()


def _human(station):
    return _local_today(station).strftime("%B %-d, %Y")


def _settles(station):
    """Polymarket US temperature contracts settle 8:00 AM ET the day AFTER the
    measured date, so endDate deliberately disagrees with the weather date --
    forcing the engine to read the date from the contract text."""
    return (_local_today(station) + _td(days=1)).isoformat() + "T12:00:00Z"


MARKETS = [
    {   # Day's high already observed above threshold, priced at 0.62 -> big edge
        "slug": "nyc-high-above-88", "question":
        f"Will the high temperature in NYC be above 88F on {_human('KNYC')}?",
        "description": "Resolves per the NWS Daily Climate Report for KNYC (Central Park).",
        "rulesDisclaimer": "Settlement per NWS CLI issued by WFO OKX for station KNYC.",
        "category": "weather", "active": True, "closed": False, "volume": 42000,
        "endDate": _settles("KNYC"), "minimumTradeQty": 1, "feeCoefficient": 0.06},
    {   # Fairly priced -> should be skipped for insufficient edge
        "slug": "chi-high-above-84", "question":
        f"Will the high temperature in Chicago be above 84F on {_human('KMDW')}?",
        "description": "Resolves per NWS CLI for KMDW.", "rulesDisclaimer": "Station KMDW.",
        "category": "weather", "active": True, "closed": False, "volume": 15000,
        "endDate": _settles("KMDW"), "minimumTradeQty": 1},
    {   # Unparseable rules -> must be skipped, never guessed
        "slug": "miami-hot-day", "question": f"Will Miami have a hot day on {_human('KMIA')}?",
        "description": "Subjective.", "category": "weather", "active": True,
        "closed": False, "volume": 9000, "endDate": _settles("KMIA")},
    {   # Wide spread / thin book -> skipped
        "slug": "sf-high-above-70", "question":
        f"Will the high temperature in San Francisco be above 70F on {_human('KSFO')}?",
        "description": "NWS CLI KSFO.", "category": "weather", "active": True,
        "closed": False, "volume": 3000, "endDate": _settles("KSFO")},
    {   # Non-weather -> shortlist only, never auto-traded
        "slug": "fed-cut-september", "question":
        "Will the Fed cut rates in September 2026?", "description": "FOMC statement.",
        "category": "economics", "active": True, "closed": False, "volume": 800000,
        "endDate": "2026-12-18T23:59:00Z"},
    {   # Grossly OVERPRICED: station is nowhere near 105F, yet YES trades at 0.78.
        # The engine must buy NO, not skip.
        "slug": "la-high-above-105", "question":
        f"Will the high temperature in Los Angeles be above 105F on {_human('KLAX')}?",
        "description": "NWS CLI KLAX.", "rulesDisclaimer": "Station KLAX.",
        "category": "weather", "active": True, "closed": False, "volume": 22000,
        "endDate": _settles("KLAX"), "minimumTradeQty": 1},
    {   # No date anywhere in the text -> must be skipped, not guessed from endDate
        "slug": "nyc-high-undated", "question":
        "Will the high temperature in NYC be above 85F?",
        "description": "Station KNYC.", "category": "weather", "active": True,
        "closed": False, "volume": 30000, "endDate": _settles("KNYC")},
    {   # Below volume floor -> prefiltered
        "slug": "tiny-market", "question": f"Will the high temperature in NYC be above 100F on {_human('KNYC')}?",
        "description": "KNYC.", "category": "weather", "active": True, "closed": False,
        "volume": 12, "endDate": _settles("KNYC")},
]

BOOKS = {
    "nyc-high-above-88": {"marketData": {"marketSlug": "nyc-high-above-88",
        "state": "MARKET_STATE_OPEN", "transactTime": "2026-08-24T19:00:00Z",
        "bids": [{"px": 0.61, "qty": 300}, {"px": 0.60, "qty": 500}],
        "offers": [{"px": 0.62, "qty": 250}, {"px": 0.63, "qty": 400}]}},
    "chi-high-above-84": {"marketData": {"marketSlug": "chi-high-above-84",
        "state": "MARKET_STATE_OPEN", "transactTime": "2026-08-24T19:00:00Z",
        "bids": [{"px": 0.70, "qty": 200}], "offers": [{"px": 0.72, "qty": 200}]}},
    "sf-high-above-70": {"marketData": {"marketSlug": "sf-high-above-70",
        "state": "MARKET_STATE_OPEN", "transactTime": "2026-08-24T19:00:00Z",
        "bids": [{"px": 0.30, "qty": 5}], "offers": [{"px": 0.55, "qty": 5}]}},
    "miami-hot-day": {"marketData": {"marketSlug": "miami-hot-day", "state": "MARKET_STATE_OPEN",
        "transactTime": "2026-08-24T19:00:00Z", "bids": [{"px": 0.4, "qty": 100}],
        "offers": [{"px": 0.42, "qty": 100}]}},
    "la-high-above-105": {"marketData": {"marketSlug": "la-high-above-105",
        "state": "MARKET_STATE_OPEN", "transactTime": "2026-08-24T19:00:00Z",
        "bids": [{"px": 0.78, "qty": 400}, {"px": 0.77, "qty": 600}],
        "offers": [{"px": 0.80, "qty": 400}]}},
    "nyc-high-undated": {"marketData": {"marketSlug": "nyc-high-undated",
        "state": "MARKET_STATE_OPEN", "transactTime": "2026-08-24T19:00:00Z",
        "bids": [{"px": 0.50, "qty": 400}], "offers": [{"px": 0.52, "qty": 400}]}},
    "fed-cut-september": {"marketData": {"marketSlug": "fed-cut-september", "state": "MARKET_STATE_OPEN",
        "transactTime": "2026-08-24T19:00:00Z", "bids": [{"px": 0.55, "qty": 5000}],
        "offers": [{"px": 0.56, "qty": 5000}]}},
}

# Station truth for the mock: NYC already hit 92F today (threshold 88 -> settled).
OBS = {
    "KNYC": {"station": "KNYC", "count": 30, "max_f": 92.0, "min_f": 74.0,
             "latest_f": 90.0, "max_ts": "2026-08-24T18:51:00Z"},
    # Deliberately unambiguous at ANY hour of the local day: nowhere near 84F.
    # (The subtle "stale forecast vs. afternoon observation" case is covered
    #  precisely by test_afternoon_observation_overrides_stale_forecast.)
    "KMDW": {"station": "KMDW", "count": 30, "max_f": 77.0, "min_f": 64.0,
             "latest_f": 76.0, "max_ts": "2026-08-24T18:51:00Z"},
    "KSFO": {"station": "KSFO", "count": 30, "max_f": 68.0, "min_f": 55.0,
             "latest_f": 67.0, "max_ts": "2026-08-24T18:51:00Z"},
}
OBS["KLAX"] = {"station": "KLAX", "count": 30, "max_f": 84.0, "min_f": 66.0,
               "latest_f": 83.0, "max_ts": "2026-08-24T18:51:00Z"}
FCST = {"KNYC": {"high_f": 91.0, "low_f": 74.0, "hours": 24},
        "KMDW": {"high_f": 78.0, "low_f": 64.0, "hours": 24},
        "KSFO": {"high_f": 69.0, "low_f": 55.0, "hours": 24},
        "KLAX": {"high_f": 85.0, "low_f": 66.0, "hours": 24}}
COORD_TO_STATION = {(40.779, -73.9693): "KNYC", (41.7842, -87.7553): "KMDW",
                    (37.6197, -122.3647): "KSFO", (25.7906, -80.3164): "KMIA",
                    (33.9381, -118.3889): "KLAX"}


# Slug -> settlement payload. Absent means unsettled (the client returns None
# on the documented 404).
SETTLEMENTS = {}
SETTLED_MARKETS = {}


def install_mocks():
    pmus_client.get_all_markets = lambda **kw: MARKETS
    pmus_client.get_markets = lambda **kw: MARKETS
    pmus_client.get_market_book = lambda slug: BOOKS.get(slug) or (_ for _ in ()).throw(
        pmus_client.DataError(f"no book for {slug}"))
    # Settlement lookups are now independent of the book and run on every
    # scan, so they must be mocked explicitly. None == HTTP 404 == unsettled.
    pmus_client.get_market_by_slug = lambda slug: SETTLED_MARKETS.get(slug)
    pmus_client.get_market_settlement = lambda slug: SETTLEMENTS.get(slug)
    nws_client.observed_extremes_f = lambda station, start: OBS.get(
        station, {"max_f": None, "min_f": None, "count": 0})
    def _fc(lat, lon, date_str):
        st = COORD_TO_STATION.get((round(lat,4), round(lon,4)))
        d = FCST.get(st, {"high_f": None, "low_f": None, "hours": 0})
        return {"date": date_str, **d}
    nws_client.forecast_daily_extremes_f = _fc


def main():
    install_mocks()
    state = os.path.join(ROOT, "state")
    if os.path.exists(state):
        shutil.rmtree(state)
    os.makedirs(state, exist_ok=True)

    from src import run_scan
    # keep scanner's module-level references pointing at the mocks
    run_scan.pm = pmus_client
    from src import scanner
    scanner.pm = pmus_client
    scanner.nws = nws_client

    rc = run_scan.main()
    print(f"\nexit={rc}")

    opps = json.load(open(os.path.join(state, "opportunities.json")))
    pf = json.load(open(os.path.join(state, "portfolio.json")))
    sl = json.load(open(os.path.join(state, "shortlist.json")))

    print("\n--- DECISIONS " + "-"*60)
    for o in opps:
        flag = "TRADE " if o.get("traded") else "skip  "
        side = o.get("recommended_side") or "-"
        print(f"{flag} {side:3s} {o['slug'][:34]:34s} {o.get('reason','')[:88]}")

    open_now = [x for x in pf["positions"] if x["status"] == "OPEN"]
    equity_now = pf["cash"] + sum(x["contracts"] * x["mark_price"] for x in open_now)
    print("\n--- PORTFOLIO " + "-"*60)
    print(f"cash ${pf['cash']:.2f}  marked equity ${equity_now:.2f}  "
          f"peak ${pf['peak_bankroll']:.2f}  positions={len(pf['positions'])}")
    for p in pf["positions"]:
        print(f"  {p['contracts']:.0f} {p['side']} @ {p['avg_price']:.4f} "
              f"stake ${p['stake']:.2f} fee ${p['entry_fee']:.2f} "
              f"mark {p['mark_price']:.4f} "
              f"pred {p['predicted_prob']:.1%} edge {p['edge_pp_at_entry']:.1f}pp")
    print(f"shortlisted (non-weather, never auto-traded): {[s['slug'] for s in sl]}")

    from src.audit import AuditLog
    print("\naudit:", AuditLog(os.path.join(state, "audit.jsonl")).verify())

    # ---- assertions ----
    traded = [o for o in opps if o.get("traded")]
    assert any(t["slug"].startswith("nyc-high-above-88") and t["recommended_side"] == "YES"
               for t in traded), \
        f"expected a YES trade on the underpriced NYC market, got {[t['slug'] for t in traded]}"
    # Chicago: the station is nowhere near 84F, yet YES trades at 0.72. YES is a
    # bad buy -- but NO at ~0.30 is a good one. Before both-sides evaluation
    # existed, this opportunity was simply discarded.
    chi = [o for o in opps if o["slug"].startswith("chi-")][0]
    assert chi["fair_probability"] < 0.35, chi["fair_probability"]
    assert chi["traded"] and chi["recommended_side"] == "NO", \
        f"expected a NO trade on the overpriced Chicago market: {chi.get('reason')}"

    # No position may be recorded at an asserted certainty.
    for p_ in pf["positions"]:
        assert p_["predicted_prob"] < 1.0, "the model must never claim certainty"
    assert any("not machine-parseable" in o.get("reason","") for o in opps if o["slug"]=="miami-hot-day")
    assert any("spread" in o.get("reason","") for o in opps if o["slug"].startswith("sf-"))
    assert any(o["slug"]=="fed-cut-september" and not o["traded"] for o in opps)
    assert any("volume" in o.get("reason","") for o in opps if o["slug"]=="tiny-market")

    # An overpriced market must be traded from the NO side.
    la = [o for o in opps if o["slug"] == "la-high-above-105"][0]
    assert la["traded"] and la["recommended_side"] == "NO", \
        f"expected a NO trade on the overpriced LA market: {la.get('reason')}"

    # A market with no date in its text must be skipped, never dated from endDate.
    und = [o for o in opps if o["slug"] == "nyc-high-undated"][0]
    assert not und["traded"] and "target date unusable" in und["reason"], und["reason"]

    # Hard caps must hold on ALL-IN cost, fee included.
    import json as _j
    cfgj = _j.load(open(os.path.join(ROOT, "config", "risk_config.json")))
    cap = cfgj["max_position_pct"] * 50
    for p_ in pf["positions"]:
        assert p_["stake"] <= cap + 1e-6, \
            f"position stake ${p_['stake']} (incl. fee) breached the ${cap} cap"
    assert sum(p_["stake"] for p_ in pf["positions"]) <= cfgj["max_total_exposure_pct"] * 50 + 1e-6

    # FIRST-SCAN equity must never exceed the starting bankroll from bookkeeping.
    assert equity_now <= 50.0 + 1e-6, (
        f"first-scan equity ${equity_now:.2f} exceeds the $50.00 bankroll -- "
        "a position is being marked off the wrong side of the book")
    assert pf["peak_bankroll"] <= 50.0 + 1e-6, (
        f"peak_bankroll ${pf['peak_bankroll']:.2f} inflated on the first scan")
    for p_ in open_now:
        assert not p_["mark_is_fallback"], f"{p_['slug']} fell back to a touch mark"
        assert p_["mark_price"] < 1.0
    print(f"  first-scan equity ${equity_now:.2f} and peak "
          f"${pf['peak_bankroll']:.2f} both within the $50.00 bankroll")

    # The evaluation window must be stamped into state/, not config/.
    assert os.path.exists(os.path.join(state, "evaluation.json")), \
        "evaluation state must be written under state/ so the workflow commits it"
    cfg_raw = open(os.path.join(ROOT, "config", "risk_config.json")).read()
    assert "evaluation_start_utc" not in cfg_raw, \
        "config must stay immutable deployment configuration"
    pos = pf["positions"][0]
    assert pos["stake"] <= 0.06*50 + 1e-6, "per-position cap breached"
    assert pos["avg_price"] > 0.615, "fill must not use midpoint"
    print("\nALL INTEGRATION ASSERTIONS PASSED")





def settlement_test():
    """Resolve the open positions on a later scan and check the payouts."""
    import shutil
    from src import run_scan, scanner, pmus_client, nws_client
    from tests.fixtures import polymarket_us_payloads as FIX

    install_mocks()
    run_scan.pm = pmus_client; scanner.pm = pmus_client; scanner.nws = nws_client
    state = os.path.join(ROOT, "state")
    shutil.rmtree(state, ignore_errors=True); os.makedirs(state, exist_ok=True)

    SETTLEMENTS.clear(); SETTLED_MARKETS.clear()
    run_scan.main()                                   # opens positions
    pf = json.load(open(os.path.join(state, "portfolio.json")))
    opened = [p for p in pf["positions"] if p["status"] == "OPEN"]
    assert opened, "scan 1 must open positions"

    print("\n=== SETTLEMENT ===")
    # Settle each open position: YES wins, NO markets resolve NO.
    for p in opened:
        yes_won = (p["side"] == "YES")
        SETTLEMENTS[p["slug"]] = {"slug": p["slug"],
                                  "settlement": 1 if yes_won else 0}
        SETTLED_MARKETS[p["slug"]] = {**FIX.MARKET_CLOSED_SETTLED, "slug": p["slug"]}
    # One market stops serving a book entirely -- resolution must still work.
    dead = opened[0]["slug"]
    _orig_book = pmus_client.get_market_book
    def _book(slug):
        if slug == dead:
            raise pmus_client.DataError("no book for a resolved market")
        return _orig_book(slug)
    pmus_client.get_market_book = _book

    run_scan.main()
    pf = json.load(open(os.path.join(state, "portfolio.json")))
    for p in pf["positions"]:
        if p["status"] == "RESOLVED":
            print(f"  {p['contracts']:.0f} {p['side']:3s} {p['slug'][:26]:26s} "
                  f"outcome={p['outcome']} exit=${p['exit_price']:.2f} "
                  f"P&L ${p['realized_pnl']:+.2f}")
    resolved = [p for p in pf["positions"] if p["status"] == "RESOLVED"]
    assert len(resolved) == len(opened), (
        f"all {len(opened)} positions must resolve, got {len(resolved)}")
    for p in resolved:
        expect = 1.0 if ((p["outcome"] == 1) == (p["side"] == "YES")) else 0.0
        assert p["exit_price"] == expect, (
            f"{p['slug']} {p['side']} outcome={p['outcome']} paid {p['exit_price']}")
    assert any(p["slug"] == dead for p in resolved), \
        "the market with no order book must still have resolved"
    print(f"  -> all {len(resolved)} positions resolved, including one with no book")

    # Now a malformed payload on a fresh position must NOT close it.
    pmus_client.get_market_book = _orig_book
    SETTLEMENTS.clear(); SETTLED_MARKETS.clear()
    shutil.rmtree(state, ignore_errors=True); os.makedirs(state, exist_ok=True)
    run_scan.main()
    pf = json.load(open(os.path.join(state, "portfolio.json")))
    victim = [p for p in pf["positions"] if p["status"] == "OPEN"][0]["slug"]
    SETTLEMENTS[victim] = FIX.SETTLEMENT_NON_NUMERIC
    SETTLED_MARKETS[victim] = {**FIX.MARKET_CLOSED_NO_PRICE, "slug": victim}
    run_scan.main()
    pf = json.load(open(os.path.join(state, "portfolio.json")))
    v = [p for p in pf["positions"] if p["slug"] == victim][0]
    assert v["status"] == "OPEN" and v["settlement_pending"] and v["realized_pnl"] is None
    print(f"  -> malformed payload preserved {victim} "
          f"({v['resolution_error'][:48]}...) and fabricated no P&L")

    from src import final_report
    rep = final_report.build(ROOT)
    assert rep["positions_awaiting_manual_settlement"] >= 1
    assert "unaccounted for" in rep["verdict"]
    print(f"  -> final report excludes it: "
          f"${rep['capital_in_unresolved_settlements']:.2f} flagged as stranded")
    print("SETTLEMENT TESTS PASSED")


def checkout_persistence_test():
    """Simulate three separate GitHub Actions runs, each a CLEAN CHECKOUT.

    A clean checkout keeps only what was committed. The workflow commits
    `state/` and `docs/`, so between runs we discard everything else -- exactly
    what Actions does. Run 1 stamps the window; runs 2 and 3 must reuse that
    same timestamp. Then we backdate and confirm the window closes and stays
    closed.

    This is the regression for the bug where the start was written to
    config/risk_config.json, which is never committed: every run re-stamped
    "now", so the 48-hour auto-shutoff could never fire.
    """
    import shutil, tempfile
    from src import run_scan, scanner, pmus_client, nws_client
    from src.evaluation import load as ev_load, save as ev_save, window as ev_window

    install_mocks()
    run_scan.pm = pmus_client; scanner.pm = pmus_client; scanner.nws = nws_client
    cfg = json.load(open(os.path.join(ROOT, "config", "risk_config.json")))
    state = os.path.join(ROOT, "state")
    eval_path = os.path.join(state, "evaluation.json")

    print("\n=== CLEAN-CHECKOUT PERSISTENCE ===")
    if os.path.exists(state):
        shutil.rmtree(state)
    os.makedirs(state, exist_ok=True)

    stamps = []
    for run in (1, 2, 3):
        # --- simulate a clean checkout: preserve only committed dirs ---------
        keep = tempfile.mkdtemp()
        if os.path.exists(state):
            shutil.copytree(state, os.path.join(keep, "state"))
        shutil.rmtree(state, ignore_errors=True)
        src_state = os.path.join(keep, "state")
        if os.path.exists(src_state):
            shutil.copytree(src_state, state)
        else:
            os.makedirs(state, exist_ok=True)
        shutil.rmtree(keep, ignore_errors=True)

        run_scan.main()
        st = ev_load(eval_path, cfg)
        stamps.append(st["started_at"])
        print(f"  run {run}: started_at={st['started_at']} runs_observed={st['runs_observed']}")

    assert len(set(stamps)) == 1, (
        f"the window start drifted across checkouts: {stamps}")
    assert ev_load(eval_path, cfg)["runs_observed"] == 3
    print("  -> start survived 3 clean checkouts unchanged")

    # Equity must stay sane: marking NO positions off the YES bid used to
    # inflate it far above the starting bankroll.
    pf_now = json.load(open(os.path.join(state, "portfolio.json")))
    eq = pf_now["cash"] + sum(
        p_["contracts"] * (p_.get("mark_price") or p_["avg_price"])
        for p_ in pf_now["positions"] if p_["status"] == "OPEN")
    assert eq < 52.0, f"equity {eq} implausible for a $50 bankroll -- marking bug?"
    print(f"  -> marked equity ${eq:.2f} is plausible for a $50 bankroll")

    cfg_raw = open(os.path.join(ROOT, "config", "risk_config.json")).read()
    assert "evaluation_start_utc" not in cfg_raw and "evaluation_complete" not in cfg_raw, \
        "mutable state leaked back into immutable deployment config"
    print("  -> config/risk_config.json remained immutable")

    # --- backdate: the window must now close and stay closed -----------------
    from datetime import datetime, timezone, timedelta
    st = ev_load(eval_path, cfg)
    st["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=48.5)).isoformat()
    ev_save(eval_path, st)
    assert ev_window(ev_load(eval_path, cfg))["expired"]

    rc = run_scan.main()
    st = ev_load(eval_path, cfg)
    assert st["complete"] and st["completed_at"], "backdated window must mark complete"
    assert os.path.exists(os.path.join(state, "final_report.json")), "final report missing"
    print(f"  -> backdated window marked complete at {st['completed_at']}")

    # completion must survive yet another clean checkout
    import tempfile as _tf
    keep = _tf.mkdtemp(); shutil.copytree(state, os.path.join(keep, "state"))
    shutil.rmtree(state); shutil.copytree(os.path.join(keep, "state"), state)
    shutil.rmtree(keep, ignore_errors=True)
    assert ev_load(eval_path, cfg)["complete"], "completion lost across checkout"
    run_scan.main()
    assert ev_load(eval_path, cfg)["complete"], "a later run un-completed the window"
    print("  -> completion persisted and later runs no-op")
    print("CLEAN-CHECKOUT PERSISTENCE PASSED")


if __name__ == "__main__":
    main()
    settlement_test()
    checkout_persistence_test()
