"""Offline test suite. No network. Run: python3 -m pytest tests/ -q"""
import json, os, sys, math
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fees import taker_fee
from src.book import Book, Level, simulate_market_buy, simulate_market_sell
from src.kelly import size_position, full_kelly_fraction
from src.weather_model import parse_temperature_market, estimate, resolve_station
from src.audit import AuditLog
from src.portfolio import Portfolio, Position
from src.risk import evaluate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config", "risk_config.json")))
STATIONS = json.load(open(os.path.join(ROOT, "config", "stations.json")))


# ---------------- fees ----------------
def test_fee_matches_published_example():
    # docs: taker theta 0.06 -> $1.50 per 100 contracts at p=0.50
    assert taker_fee(100, 0.50) == 1.50

def test_fee_symmetric_and_lower_at_extremes():
    assert taker_fee(100, 0.30) == taker_fee(100, 0.70)
    assert taker_fee(100, 0.05) < taker_fee(100, 0.50)

def test_fee_respects_market_coefficient():
    assert taker_fee(100, 0.50, fee_coefficient=0.02) == 0.50


# ---------------- book / fills ----------------
def _book():
    return Book(slug="t", state="MARKET_STATE_OPEN",
                bids=[Level(0.60, 40), Level(0.58, 100)],
                offers=[Level(0.62, 25), Level(0.65, 75)])

def test_fill_never_uses_midpoint():
    b = _book()
    f = simulate_market_buy(b, 50)
    assert f.avg_price > b.mid, "fill must be worse than mid for a taker"
    assert f.avg_price > b.best_ask, "crossing multiple levels must cost more than the touch"

def test_fill_walks_real_depth():
    f = simulate_market_buy(_book(), 50)
    assert f.levels_consumed == [{"px": 0.62, "qty": 25}, {"px": 0.65, "qty": 25}]
    assert math.isclose(f.avg_price, (25*0.62 + 25*0.65)/50, rel_tol=1e-9)

def test_partial_fill_when_depth_insufficient():
    f = simulate_market_buy(_book(), 500)
    assert not f.complete and f.filled == 100 and "unavailable" in f.reason

def test_no_liquidity_refuses():
    empty = Book(slug="t", state="MARKET_STATE_OPEN", bids=[], offers=[])
    assert simulate_market_buy(empty, 10).filled == 0

def test_sell_side_returns_proceeds_net_of_fee():
    f = simulate_market_sell(_book(), 40)
    assert f.avg_price == 0.60 and f.net_cost < f.gross_cost

def test_untradeable_state_flagged():
    b = _book(); b.state = "MARKET_STATE_HALTED"
    assert not b.is_tradeable()


# ---------------- kelly / caps ----------------
def test_kelly_formula():
    assert math.isclose(full_kelly_fraction(0.70, 0.55), (0.70-0.55)/(1-0.55))

def test_no_bet_without_edge():
    assert full_kelly_fraction(0.50, 0.60) < 0

def test_quarter_kelly_never_exceeded():
    r = size_position(prob_conservative=0.99, price=0.10, bankroll=50, cfg=CFG,
                      current_total_exposure=0, current_cluster_exposure=0)
    assert r.used_kelly_fraction <= CFG["kelly_fraction"] + 1e-12

def test_position_cap_binds():
    r = size_position(prob_conservative=0.95, price=0.50, bankroll=50, cfg=CFG,
                      current_total_exposure=0, current_cluster_exposure=0)
    assert r.stake <= CFG["max_position_pct"] * 50 + 1e-9

def test_total_exposure_cap_blocks():
    r = size_position(prob_conservative=0.90, price=0.50, bankroll=50, cfg=CFG,
                      current_total_exposure=10.0, current_cluster_exposure=0)
    assert not r.approved

def test_correlated_cap_blocks():
    r = size_position(prob_conservative=0.90, price=0.50, bankroll=50, cfg=CFG,
                      current_total_exposure=0, current_cluster_exposure=4.0)
    assert not r.approved

def test_venue_minimum_causes_skip():
    r = size_position(prob_conservative=0.60, price=0.50, bankroll=50, cfg=CFG,
                      current_total_exposure=0, current_cluster_exposure=0,
                      venue_min_contracts=1000)
    assert not r.approved and "minimum" in r.reason


# ---------------- weather ----------------
def test_parses_clean_market():
    p = parse_temperature_market("Will the high temperature in NYC be above 88F on Aug 24?")
    assert p == {"metric": "high", "direction": "above", "threshold_f": 88.0}

def test_refuses_ambiguous_market():
    assert parse_temperature_market("Will it be hot in New York tomorrow?") is None
    assert parse_temperature_market("Will Bitcoin reach $100k?") is None

def test_station_from_rules_beats_city_alias():
    s = resolve_station("Chicago temperature market", "resolves per KMDW", STATIONS["city_aliases"])
    assert s == "KMDW"

def test_unknown_city_returns_none():
    assert resolve_station("Will Boise be above 90F?", "", STATIONS["city_aliases"]) is None

def test_already_observed_high_is_near_certain():
    e = estimate(metric="high", direction="above", threshold_f=88,
                 forecast_high_f=89, forecast_low_f=None, observed_max_f=92,
                 observed_min_f=None, hours_elapsed_local=16, is_target_day=True)
    assert e.determined and e.probability > 0.97

def test_day_ahead_has_wide_band():
    e = estimate(metric="high", direction="above", threshold_f=88,
                 forecast_high_f=90, forecast_low_f=None, observed_max_f=None,
                 observed_min_f=None, hours_elapsed_local=8, is_target_day=False)
    assert e.sigma_f >= 4.0 and (e.prob_high - e.prob_low) > 0.05

def test_missing_forecast_is_unusable():
    e = estimate(metric="high", direction="above", threshold_f=88,
                 forecast_high_f=None, forecast_low_f=None, observed_max_f=None,
                 observed_min_f=None, hours_elapsed_local=8, is_target_day=True)
    assert e.method == "no_forecast_available"


# ---------------- audit ----------------
def test_audit_chain_detects_rewritten_prediction(tmp_path):
    p = str(tmp_path / "a.jsonl")
    log = AuditLog(p)
    log.append("decision", {"prob": 0.7})
    log.append("decision", {"prob": 0.4})
    assert log.verify()["ok"]
    lines = open(p).read().splitlines()
    lines[0] = lines[0].replace('"prob":0.7', '"prob":0.95')
    open(p, "w").write("\n".join(lines) + "\n")
    assert not log.verify()["ok"]

def test_audit_is_append_only(tmp_path):
    p = str(tmp_path / "a.jsonl")
    log = AuditLog(p)
    log.append("x", {"n": 1}); log.append("x", {"n": 2})
    assert len(list(log.read())) == 2


# ---------------- portfolio / risk ----------------
def _pos(**kw):
    d = dict(decision_id="d1", slug="s", question="q", url="u", side="YES",
             contracts=10, avg_price=0.5, stake=5.0, entry_fee=0.15,
             opened_at="t", category="weather", cluster="wx:KNYC:2026-08-24",
             predicted_prob=0.7, predicted_prob_low=0.65, predicted_prob_high=0.75,
             edge_pp_at_entry=15.0)
    d.update(kw); return Position(**d)

def test_exposure_accounting(tmp_path):
    pf = Portfolio(str(tmp_path/"p.json"), 50.0)
    pf.open_position(_pos())
    assert pf.cash == 45.0 and pf.total_exposure() == 5.0
    assert pf.cluster_exposure("wx:KNYC:2026-08-24") == 5.0

def test_resolution_records_outcome(tmp_path):
    pf = Portfolio(str(tmp_path/"p.json"), 50.0)
    p = _pos(); pf.open_position(p)
    pf.close_position(p, 1.0, 0.0, outcome=1)
    assert p.realized_pnl == 5.0 and pf.cash == 55.0

def test_drawdown_and_halt(tmp_path):
    pf = Portfolio(str(tmp_path/"p.json"), 50.0)
    p = _pos(stake=20.0, contracts=40); pf.open_position(p)
    pf.close_position(p, 0.0, 0.0, outcome=0)   # total loss
    assert pf.drawdown_pct() >= 0.20
    v = evaluate(pf, CFG, data_age_seconds=10, consecutive_errors=0, data_ok=True)
    assert not v.allow_new_positions

def test_risk_fails_closed_on_unknown_data_age(tmp_path):
    pf = Portfolio(str(tmp_path/"p.json"), 50.0)
    v = evaluate(pf, CFG, data_age_seconds=None, consecutive_errors=0, data_ok=True)
    assert not v.allow_new_positions

def test_emergency_stop_halts(tmp_path):
    pf = Portfolio(str(tmp_path/"p.json"), 50.0)
    cfg = dict(CFG); cfg["emergency_stop"] = True
    v = evaluate(pf, cfg, data_age_seconds=10, consecutive_errors=0, data_ok=True)
    assert not v.allow_new_positions

def test_stale_data_halts(tmp_path):
    pf = Portfolio(str(tmp_path/"p.json"), 50.0)
    v = evaluate(pf, CFG, data_age_seconds=99999, consecutive_errors=0, data_ok=True)
    assert not v.allow_new_positions


# ---------------- safety invariants ----------------
def test_no_order_placement_code_anywhere():
    """The repository must contain no live order-entry path."""
    banned = ["api.polymarket.us", "create-order", "createOrder", "place_order",
              "insert-order", "POST /v1/orders"]
    hits = []
    for dirpath, _, files in os.walk(os.path.join(ROOT, "src")):
        for f in files:
            if f.endswith(".py"):
                txt = open(os.path.join(dirpath, f)).read()
                for b in banned:
                    if b in txt:
                        hits.append(f"{f}:{b}")
    assert not hits, f"live order code present: {hits}"

def test_live_trading_disabled_in_config():
    assert CFG["live_trading_enabled"] is False
    assert CFG["mode"] == "PAPER"
    assert CFG["starting_bankroll"] == 50.00

def test_notify_has_no_inbound_handler():
    txt = open(os.path.join(ROOT, "src", "notify.py")).read()
    for b in ["getUpdates", "setWebhook", "polling", "on_message"]:
        assert b not in txt


# ---------------- regression: stale-forecast bug ----------------
# Found by tests/integration_dryrun.py. The model trusted a morning forecast
# with a tight post-peak error band while the station itself had failed to
# reach the threshold, producing a confident BUY on a losing contract.

def test_afternoon_observation_overrides_stale_forecast():
    e = estimate(metric="high", direction="above", threshold_f=84,
                 forecast_high_f=85, forecast_low_f=None, observed_max_f=83,
                 observed_min_f=None, hours_elapsed_local=18.0, is_target_day=True)
    assert e.probability < 0.35, (
        "after peak heating with the station at 83F, exceeding 84F must be "
        f"unlikely; got {e.probability}")
    assert e.expected_high_f < 85

def test_morning_forecast_still_governs():
    e = estimate(metric="high", direction="above", threshold_f=84,
                 forecast_high_f=85, forecast_low_f=None, observed_max_f=70,
                 observed_min_f=None, hours_elapsed_local=8.0, is_target_day=True)
    assert e.expected_high_f == 85, "a morning observation must not cap the forecast"

def test_daily_high_never_estimated_below_observation():
    e = estimate(metric="high", direction="above", threshold_f=90,
                 forecast_high_f=80, forecast_low_f=None, observed_max_f=88,
                 observed_min_f=None, hours_elapsed_local=17.0, is_target_day=True)
    assert e.expected_high_f >= 88, "a daily high cannot be below what was observed"

def test_remaining_rise_decays_after_peak():
    from src.weather_model import remaining_rise_f
    assert remaining_rise_f(8.0) > 30
    assert remaining_rise_f(12.0) > remaining_rise_f(15.0)
    assert remaining_rise_f(19.0) <= 0.35

def test_low_metric_is_floored_by_observation():
    e = estimate(metric="low", direction="below", threshold_f=60,
                 forecast_high_f=None, forecast_low_f=55, observed_max_f=None,
                 observed_min_f=64, hours_elapsed_local=12.0, is_target_day=True)
    assert e.expected_high_f >= 63, "midday: today's low is already set near 64F"


# ---------------- 48-hour evaluation window ----------------
# REGRESSION: an earlier version stamped the window start into
# config/risk_config.json, which the workflow does NOT commit. Every scheduled
# run therefore checked out an empty start, re-stamped "now", and the window
# could never expire -- the scanner would have run forever and the 48-hour
# auto-shutoff would never have fired. These tests simulate separate CLEAN
# CHECKOUTS to prove the start survives, because that is exactly what a
# GitHub Actions run is.
import shutil as _shutil
from src import evaluation


def _fresh_checkout(src_repo, dest):
    """Simulate a clean `actions/checkout`: only committed paths survive.

    The workflow commits `state/` and `docs/`. Everything else a previous run
    wrote is gone. Copying only those directories reproduces that faithfully.
    """
    os.makedirs(dest, exist_ok=True)
    for sub in ("state", "docs"):
        s_dir = os.path.join(src_repo, sub)
        d_dir = os.path.join(dest, sub)
        if os.path.exists(d_dir):
            _shutil.rmtree(d_dir)
        if os.path.exists(s_dir):
            _shutil.copytree(s_dir, d_dir)
        else:
            os.makedirs(d_dir, exist_ok=True)
    return os.path.join(dest, "state", "evaluation.json")


def test_config_is_immutable_of_evaluation_state():
    """Deployment config must never carry mutable evaluation state."""
    evaluation.assert_config_immutable(CFG)          # must not raise
    for k in evaluation.FORBIDDEN_CONFIG_KEYS:
        assert k not in CFG, (
            f"{k} is back in risk_config.json; it would be lost on every "
            "clean checkout")

def test_config_immutability_guard_actually_fires():
    with pytest.raises(RuntimeError, match="mutable evaluation state"):
        evaluation.assert_config_immutable(
            dict(CFG, evaluation_start_utc="2026-01-01T00:00:00+00:00"))

def test_start_survives_a_clean_checkout(tmp_path):
    """Run 1 stamps the start. Run 2, from a clean checkout, must reuse it."""
    repo1 = tmp_path / "run1"; os.makedirs(repo1 / "state", exist_ok=True)
    p1 = str(repo1 / "state" / "evaluation.json")
    st1, stamped1 = evaluation.stamp_start_if_needed(p1, CFG)
    assert stamped1 and st1["started_at"]

    # --- everything not committed is discarded; state/ is carried over ---
    p2 = _fresh_checkout(str(repo1), str(tmp_path / "run2"))
    st2, stamped2 = evaluation.stamp_start_if_needed(p2, CFG)

    assert not stamped2, "second run must NOT re-stamp the window"
    assert st2["started_at"] == st1["started_at"], (
        f"start drifted across checkouts: {st1['started_at']} -> {st2['started_at']}")
    assert st2["runs_observed"] == 2, "each run must be counted"

def test_start_survives_many_checkouts(tmp_path):
    repo = tmp_path / "r0"; os.makedirs(repo / "state", exist_ok=True)
    p = str(repo / "state" / "evaluation.json")
    st, _ = evaluation.stamp_start_if_needed(p, CFG)
    origin = st["started_at"]
    prev = str(repo)
    for i in range(1, 8):
        p = _fresh_checkout(prev, str(tmp_path / f"r{i}"))
        st, stamped = evaluation.stamp_start_if_needed(p, CFG)
        assert not stamped and st["started_at"] == origin, f"drifted at checkout {i}"
        prev = str(tmp_path / f"r{i}")
    assert st["runs_observed"] == 8

def test_backdated_start_marks_complete_across_checkout(tmp_path):
    """A backdated start must expire, and completion must survive a checkout."""
    from datetime import datetime, timezone, timedelta
    repo = tmp_path / "run1"; os.makedirs(repo / "state", exist_ok=True)
    p1 = str(repo / "state" / "evaluation.json")
    evaluation.stamp_start_if_needed(p1, CFG)

    st = evaluation.load(p1, CFG)
    st["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=48.5)).isoformat()
    evaluation.save(p1, st)

    w = evaluation.window(evaluation.load(p1, CFG))
    assert w["expired"] and w["remaining_hours"] == 0.0
    evaluation.mark_complete(p1, CFG)

    p2 = _fresh_checkout(str(repo), str(tmp_path / "run2"))
    st2 = evaluation.load(p2, CFG)
    assert st2["complete"] and st2["completed_at"], (
        "completion must persist across a checkout, otherwise the schedule is "
        "disabled while the state that justifies it has been thrown away")
    assert evaluation.window(st2)["complete"]

def test_window_not_expired_at_start(tmp_path):
    p = str(tmp_path / "e.json")
    st, _ = evaluation.stamp_start_if_needed(p, CFG)
    w = evaluation.window(st)
    assert w["started"] and not w["expired"] and w["remaining_hours"] > 47.9

def test_duration_comes_from_config_not_state(tmp_path):
    p = str(tmp_path / "e.json")
    evaluation.stamp_start_if_needed(p, CFG)
    st = evaluation.load(p, dict(CFG, evaluation_hours=6))
    assert st["hours"] == 6, "config stays authoritative for the window length"

def test_corrupt_state_refuses_rather_than_restarting(tmp_path):
    p = str(tmp_path / "e.json")
    open(p, "w").write("{ this is not json")
    with pytest.raises(RuntimeError, match="Refusing to run"):
        evaluation.load(p, CFG)

def test_completed_window_stays_completed(tmp_path):
    p = str(tmp_path / "e.json")
    evaluation.stamp_start_if_needed(p, CFG)
    evaluation.mark_complete(p, CFG)
    assert evaluation.is_complete(p, CFG)
    evaluation.stamp_start_if_needed(p, CFG)      # a stray later run
    assert evaluation.is_complete(p, CFG), "a later run must not un-complete it"


# ---------------- secrets hygiene ----------------
SECRET_PATTERNS = [
    r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}",
    r"gho_[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b\d{4,10}:[A-Za-z0-9_-]{35}\b",              # telegram bot token
    r"(?i)\b(seed[_ ]?phrase|mnemonic|private[_ ]?key)\b\s*[:=]\s*['\"][^'\"]+",
    r"(?i)\bpassword\b\s*[:=]\s*['\"][^'\"]{3,}",
]

def test_repository_contains_no_secrets():
    import re as _re
    skip_dirs = {".git", "__pycache__", ".pytest_cache", "state"}
    hits = []
    for dirpath, dirnames, files in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for f in files:
            fp = os.path.join(dirpath, f)
            if f.endswith((".png", ".jpg", ".ico")):
                continue
            try:
                txt = open(fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for pat in SECRET_PATTERNS:
                # the pattern list itself is not a finding
                if os.path.basename(fp) == "test_engine.py":
                    continue
                for m in _re.finditer(pat, txt):
                    hits.append(f"{os.path.relpath(fp, ROOT)}: {m.group(0)[:24]}")
    assert not hits, f"possible secrets committed: {hits}"

def test_credentials_only_ever_come_from_environment():
    import re as _re
    txt = open(os.path.join(ROOT, "src", "notify.py")).read()
    assert "os.environ" in txt, "credentials must come from the environment"
    # builtin open(...) only -- urlopen() is a network call, not a file read
    file_reads = _re.findall(r"(?<![\w.])open\s*\(", txt)
    assert not file_reads, "notify.py must not read credentials from files"
    assert "json.load(" not in txt, "notify.py must not load credentials from JSON"

def test_no_polymarket_dot_com_anywhere():
    """The global exchange must never be contacted from this system."""
    hits = []
    for dirpath, dirnames, files in os.walk(os.path.join(ROOT, "src")):
        for f in files:
            if f.endswith(".py"):
                txt = open(os.path.join(dirpath, f)).read()
                if "polymarket.com" in txt:
                    hits.append(f)
    assert not hits, f"references to the global exchange found in {hits}"


# ---------------- target date from contract text, not endDate ----------------
from datetime import date as _d
from src.weather_model import parse_target_date

def test_target_date_parsed_from_question():
    d, note = parse_target_date("Will NYC high be above 88F on August 24, 2026?",
                                "", today=_d(2026, 8, 20))
    assert d == _d(2026, 8, 24)

def test_target_date_infers_year_when_absent():
    d, _ = parse_target_date("NYC high above 88F on Aug 24?", "", today=_d(2026, 8, 20))
    assert d == _d(2026, 8, 24)

def test_target_date_refuses_when_absent():
    d, why = parse_target_date("Will NYC be hot?", "", today=_d(2026, 8, 20))
    assert d is None and "no weather date" in why

def test_target_date_refuses_when_ambiguous():
    d, why = parse_target_date("High above 88F on August 24, 2026",
                               "Resolves per CLI for August 25, 2026",
                               today=_d(2026, 8, 20))
    assert d is None and "ambiguous" in why

def test_end_date_is_never_the_weather_date():
    """endDate is the settlement moment -- typically the morning AFTER the day
    being measured -- so it must not drive valuation."""
    market = {"question": "Will NYC high be above 88F on August 24, 2026?",
              "endDate": "2026-08-25T12:00:00Z", "rulesDisclaimer": "Station KNYC."}
    d, _ = parse_target_date(market["question"], market["rulesDisclaimer"],
                             today=_d(2026, 8, 24))
    assert d == _d(2026, 8, 24), "must read Aug 24 from the text, not Aug 25 from endDate"


# ---------------- CLI is only claimed when actually retrieved ----------------
from src.nws_client import parse_cli_date, cli_for_date

CLI_TEXT = """CDUS41 KOKX 250800
CLINYC
CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK NY
...THE NEW YORK CENTRAL PARK CLIMATE SUMMARY FOR AUGUST 24 2026...
TEMPERATURE (F)
  MAXIMUM         91    2:51 PM
  MINIMUM         73    5:20 AM
"""

def test_cli_date_parsed():
    assert parse_cli_date(CLI_TEXT) == _d(2026, 8, 24)

def test_cli_date_absent_returns_none():
    assert parse_cli_date("CLIMATE REPORT with no summary line") is None

def test_cli_requires_exact_date_match(monkeypatch):
    from src import nws_client as n
    monkeypatch.setattr(n, "cli_report", lambda office, limit=12: [{"id": "P1"}])
    monkeypatch.setattr(n, "cli_text", lambda pid: CLI_TEXT)
    assert n.cli_for_date("OKX", "CLINYC", _d(2026, 8, 24))["matched"] is True
    off = n.cli_for_date("OKX", "CLINYC", _d(2026, 8, 23))
    assert off["matched"] is False, "a different date must never be accepted"

def test_cli_requires_station_match(monkeypatch):
    from src import nws_client as n
    monkeypatch.setattr(n, "cli_report", lambda office, limit=12: [{"id": "P1"}])
    monkeypatch.setattr(n, "cli_text", lambda pid: CLI_TEXT)
    res = n.cli_for_date("OKX", "CLIMDW", _d(2026, 8, 24))
    assert res["matched"] is False, "a Chicago contract must not settle on a NYC report"

def test_future_market_does_not_claim_cli_as_retrieved():
    """For a day not yet over, the CLI does not exist. Say so; don't cite it."""
    import json as _j
    stations = _j.load(open(os.path.join(ROOT, "config", "stations.json")))
    from src import scanner as _sc, nws_client as _n
    _sc.nws.observed_extremes_f = lambda s, t: {"max_f": 80.0, "min_f": 60.0, "count": 5}
    _sc.nws.forecast_daily_extremes_f = lambda a, b, c: {"high_f": 90.0, "low_f": 70.0, "hours": 24}
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    tomorrow = (_dt.now(_tz.utc) + _td(days=1)).date().isoformat()
    m = {"question": f"Will NYC high be above 88F on {tomorrow}?",
         "rulesDisclaimer": "Station KNYC.", "endDate": tomorrow + "T23:59:00Z"}
    val = _sc.value_weather_market(m, stations)
    assert val["ok"]
    assert "not published yet" in val["settlement_source"]
    assert val.get("cli") is None
    for e in val["evidence"]:
        assert "Daily Climate Report" not in e.get("source", ""), (
            "must not cite a CLI it has not read")


# ---------------- both sides evaluated ----------------
from src.book import simulate_market_buy_no, best_no_ask, no_depth_within

def _mkt():
    return Book(slug="t", state="MARKET_STATE_OPEN",
                bids=[Level(0.60, 500)], offers=[Level(0.62, 500)])

def test_no_side_priced_off_bid_stack():
    b = _mkt()
    assert best_no_ask(b) == 0.40
    f = simulate_market_buy_no(b, 100)
    assert f.avg_price == 0.40

def test_picks_no_when_market_is_too_high():
    from src.scanner import compute_edge
    r = compute_edge(_mkt(), 0.20, 0.15, 0.25, 100, None)
    assert r["side"] == "NO" and r["conservative_edge_pp"] > 8

def test_picks_yes_when_market_is_too_low():
    from src.scanner import compute_edge
    r = compute_edge(_mkt(), 0.85, 0.80, 0.90, 100, None)
    assert r["side"] == "YES" and r["conservative_edge_pp"] > 8

def test_fair_market_qualifies_on_neither_side():
    from src.scanner import compute_edge
    r = compute_edge(_mkt(), 0.61, 0.56, 0.66, 100, None)
    assert r["conservative_edge_pp"] < 8
    assert r["sides"]["YES"]["conservative_edge_pp"] < 8
    assert r["sides"]["NO"]["conservative_edge_pp"] < 8

def test_no_side_conservative_uses_upper_band():
    """For NO, the pessimistic case is the HIGH end of the YES band."""
    from src.scanner import compute_edge
    r = compute_edge(_mkt(), 0.20, 0.05, 0.35, 100, None)
    assert abs(r["probability_conservative"] - 0.65) < 1e-6


# ---------------- caps enforced on all-in cost including fees ----------------
from src.kelly import fit_fill_to_caps, largest_fill_within_caps
from src.book import simulate_market_buy as _buy

def test_fee_cannot_push_exposure_past_cap():
    b = Book(slug="t", state="MARKET_STATE_OPEN", bids=[Level(0.49, 9999)],
             offers=[Level(0.50, 9999)])
    cap = CFG["max_position_pct"] * 50          # $3.00
    fill, n = largest_fill_within_caps(_buy, b, 100, bankroll=50, cfg=CFG,
                                       current_total_exposure=0,
                                       current_cluster_exposure=0)
    assert fill is not None
    assert fill.net_cost <= cap + 1e-9, (
        f"all-in ${fill.net_cost} (fee ${fill.fee}) exceeded the ${cap} cap")
    assert fill.fee > 0, "this fixture must actually incur a fee"

def test_naive_sizing_would_have_breached_the_cap():
    """Guards the specific failure: sizing on price alone, then paying a fee."""
    cap = CFG["max_position_pct"] * 50
    naive_contracts = int(cap / 0.50)            # ignores the fee entirely
    b = Book(slug="t", state="MARKET_STATE_OPEN", bids=[Level(0.49, 9999)],
             offers=[Level(0.50, 9999)])
    naive = _buy(b, naive_contracts)
    assert naive.net_cost > cap, "fixture should demonstrate the breach"
    fill, n = largest_fill_within_caps(_buy, b, naive_contracts, bankroll=50,
                                       cfg=CFG, current_total_exposure=0,
                                       current_cluster_exposure=0)
    assert fill.net_cost <= cap + 1e-9 and n < naive_contracts

def test_fit_reports_binding_ceiling():
    b = Book(slug="t", state="MARKET_STATE_OPEN", bids=[Level(0.49, 9999)],
             offers=[Level(0.50, 9999)])
    ok, ceiling, why = fit_fill_to_caps(_buy(b, 100), bankroll=50, cfg=CFG,
                                        current_total_exposure=0,
                                        current_cluster_exposure=0)
    assert not ok and "exceeds the permitted" in why and ceiling == 3.0

def test_caps_respect_existing_cluster_exposure():
    b = Book(slug="t", state="MARKET_STATE_OPEN", bids=[Level(0.49, 9999)],
             offers=[Level(0.50, 9999)])
    fill, n = largest_fill_within_caps(_buy, b, 100, bankroll=50, cfg=CFG,
                                       current_total_exposure=0,
                                       current_cluster_exposure=3.9)
    assert fill is None or fill.net_cost <= 0.08 * 50 - 3.9 + 1e-9


# ---------------- regression: NO positions must be marked side-awarely -------
# A NO contract exits against the YES OFFER stack, not the YES bid. Marking a
# NO position off the bid values it at roughly (1 - its true worth), which
# inflates equity and makes paper results meaningless.
from src.book import simulate_market_sell_no, exit_fill as _exit

def _skewed():
    return Book(slug="t", state="MARKET_STATE_OPEN",
                bids=[Level(0.78, 400)], offers=[Level(0.80, 400)])

def test_no_position_marks_near_its_true_worth():
    f = _exit(_skewed(), "NO", 13)
    per = f.net_cost / f.filled
    assert 0.15 < per < 0.21, f"NO contract should mark near 0.20, got {per}"

def test_yes_position_still_marks_off_the_bid():
    f = _exit(_skewed(), "YES", 13)
    per = f.net_cost / f.filled
    assert 0.74 < per < 0.79, per

def test_side_agnostic_marking_would_have_inflated_equity():
    naive = simulate_market_sell(_skewed(), 13)          # the old, wrong path
    correct = _exit(_skewed(), "NO", 13)
    assert naive.net_cost - correct.net_cost > 6.0, (
        "fixture must demonstrate the inflation the side-aware fix removes")

def test_no_exit_walks_the_offer_stack():
    b = Book(slug="t", state="MARKET_STATE_OPEN", bids=[Level(0.70, 10)],
             offers=[Level(0.75, 5), Level(0.80, 5)])
    f = simulate_market_sell_no(b, 10)
    assert f.levels_consumed[0]["px"] == 0.25 and f.levels_consumed[1]["px"] == 0.20

def test_no_exit_without_offers_refuses():
    b = Book(slug="t", state="MARKET_STATE_OPEN", bids=[Level(0.70, 10)], offers=[])
    assert simulate_market_sell_no(b, 5).filled == 0


# ---------------------------------------------------------------------------
# REGRESSION: first-scan NO entry must not inflate equity or the peak
# ---------------------------------------------------------------------------
# The bug: a newly opened position was created with mark_price=book.best_bid.
# For a NO position that is the YES bid -- roughly (1 - its true worth) -- so
# a fresh NO position was booked far above what it could be sold for. Equity
# read $60.54 against a $50 bankroll on the FIRST scan, and because
# open_position() touched the high-water mark, that fiction was persisted as
# peak_bankroll, corrupting drawdown and every risk halt derived from it.
#
# Testing exit_fill() alone would NOT have caught this: exit_fill was already
# correct. The defect lived in the entry path. So this exercises a real scan.


def _mock_discovery(monkeypatch, markets):
    """Mock the endpoints discovery uses, with real offset pagination.

    run_scan now goes through src.discovery, which calls get_markets /
    get_tags / search_markets. Mocking the old get_all_markets would leave
    discovery reaching for the network.
    """
    from src import pmus_client as pmc, discovery as dsc

    def _get_markets(limit=100, offset=0, active=True, closed=False,
                     categories=None, tagIds=None, **kw):
        if categories:
            pool = [m for m in markets
                    if str(m.get("category", "")).lower()
                    in {str(c).lower() for c in categories}]
        elif tagIds:
            pool = [m for m in markets
                    if str(m.get("category", "")).lower() == "weather"]
        else:
            pool = markets
        return pool[offset:offset + limit]

    monkeypatch.setattr(pmc, "get_markets", _get_markets)
    monkeypatch.setattr(pmc, "get_tags",
                        lambda **kw: [{"id": 77, "slug": "weather", "label": "Weather"}])
    monkeypatch.setattr(pmc, "search_markets", lambda q, **kw: [])
    monkeypatch.setattr(dsc, "pm", pmc)
    return pmc

def _run_first_scan(tmp_path, monkeypatch, markets, books, obs, fcst):
    """Run one real scan cycle against mocked feeds, in an isolated state dir."""
    from src import run_scan as rs, scanner as sc, pmus_client as pmc, nws_client as nwc

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    # final_report.build(HERE) reads config from the repo root, so the temp
    # repo needs a complete skeleton, not just state/.
    (tmp_path / "config").mkdir(exist_ok=True)
    for f in ("risk_config.json", "stations.json"):
        _shutil.copy(os.path.join(ROOT, "config", f), tmp_path / "config" / f)
    for attr, val in [
        ("STATE_DIR", str(state)),
        ("PORTFOLIO_PATH", str(state / "portfolio.json")),
        ("AUDIT_PATH", str(state / "audit.jsonl")),
        ("STATUS_PATH", str(state / "status.json")),
        ("SHORTLIST_PATH", str(state / "shortlist.json")),
        ("OPPS_PATH", str(state / "opportunities.json")),
        ("EVAL_PATH", str(state / "evaluation.json")),
    ]:
        monkeypatch.setattr(rs, attr, val)
    monkeypatch.setattr(rs, "HERE", str(tmp_path))
    monkeypatch.setattr(rs.dashboard, "build", lambda root: None)

    _mock_discovery(monkeypatch, markets)
    monkeypatch.setattr(pmc, "get_market_book", lambda slug: books[slug])
    monkeypatch.setattr(pmc, "get_market_by_slug", lambda slug: {})
    monkeypatch.setattr(nwc, "observed_extremes_f", lambda s, t: obs)
    monkeypatch.setattr(nwc, "forecast_daily_extremes_f", lambda a, b, c: fcst)
    monkeypatch.setattr(sc, "pm", pmc)
    monkeypatch.setattr(sc, "nws", nwc)
    monkeypatch.setattr(rs, "pm", pmc)

    # config/ and stations/ still come from the repo
    monkeypatch.setattr(rs, "CFG_PATH", os.path.join(ROOT, "config", "risk_config.json"))
    monkeypatch.setattr(rs, "STATIONS_PATH", os.path.join(ROOT, "config", "stations.json"))
    rc = rs.main()
    pf = json.load(open(state / "portfolio.json"))
    return rc, pf, state


def _no_entry_fixture():
    """A market priced far too high, so the engine buys NO on the first scan."""
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZI
    la_today = _dt.now(_ZI("America/Los_Angeles")).date()
    human = la_today.strftime("%B %-d, %Y")
    settles = (la_today + _td(days=1)).isoformat() + "T12:00:00Z"
    markets = [{
        "slug": "la-no-entry",
        "question": f"Will the high temperature in Los Angeles be above 105F on {human}?",
        "description": "NWS CLI KLAX.", "rulesDisclaimer": "Station KLAX.",
        "category": "weather", "active": True, "closed": False, "volume": 40000,
        "endDate": settles, "minimumTradeQty": 1, "feeCoefficient": 0.06}]
    books = {"la-no-entry": {"marketData": {
        "marketSlug": "la-no-entry", "state": "MARKET_STATE_OPEN",
        "transactTime": "2026-08-25T00:00:00Z",
        "bids": [{"px": 0.78, "qty": 900}],
        "offers": [{"px": 0.80, "qty": 900}]}}}
    obs = {"station": "KLAX", "count": 30, "max_f": 84.0, "min_f": 66.0,
           "latest_f": 83.0, "max_ts": "2026-08-25T00:00:00Z"}
    fcst = {"high_f": 85.0, "low_f": 66.0, "hours": 24}
    return markets, books, obs, fcst


def test_first_scan_no_entry_does_not_inflate_equity(tmp_path, monkeypatch):
    rc, pf, _ = _run_first_scan(tmp_path, monkeypatch, *_no_entry_fixture())
    assert rc == 0
    pos = [p for p in pf["positions"] if p["status"] == "OPEN"]
    assert pos and pos[0]["side"] == "NO", "fixture must open a NO position"

    equity = pf["cash"] + sum(p["contracts"] * p["mark_price"] for p in pos)
    start = pf["starting_bankroll"]
    assert equity <= start + 1e-6, (
        f"first-scan equity ${equity:.2f} exceeds the ${start:.2f} bankroll purely "
        "from bookkeeping -- a NO position is being marked off the YES bid")

def test_first_persisted_mark_equals_executable_no_exit(tmp_path, monkeypatch):
    markets, books, obs, fcst = _no_entry_fixture()
    _, pf, _ = _run_first_scan(tmp_path, monkeypatch, markets, books, obs, fcst)
    p = [x for x in pf["positions"] if x["status"] == "OPEN"][0]

    bk = Book.from_api(books["la-no-entry"])
    expected = _exit(bk, "NO", p["contracts"], fee_coefficient=0.06)
    expected_mark = expected.net_cost / expected.filled
    assert abs(p["mark_price"] - expected_mark) < 1e-9, (
        f"persisted mark {p['mark_price']} != executable fee-inclusive NO exit "
        f"{expected_mark}")
    assert p["mark_is_fallback"] is False
    assert "executable NO exit" in p["mark_source"]

def test_first_scan_does_not_inflate_high_water_mark(tmp_path, monkeypatch):
    _, pf, _ = _run_first_scan(tmp_path, monkeypatch, *_no_entry_fixture())
    assert pf["peak_bankroll"] <= pf["starting_bankroll"] + 1e-6, (
        f"peak_bankroll {pf['peak_bankroll']} was inflated above the starting "
        "bankroll on the first scan; drawdown and every risk halt built on it "
        "would be wrong")

def test_opening_a_position_never_raises_the_peak(tmp_path):
    """Opening converts cash into an asset worth slightly less. Never a high."""
    pf = Portfolio(str(tmp_path / "p.json"), 50.0)
    before = pf.peak_bankroll
    p = _pos(stake=3.0, contracts=10, avg_price=0.30)
    p.mark_price = 0.28
    pf.open_position(p)
    assert pf.peak_bankroll == before

def test_high_water_mark_requires_verified_marks(tmp_path):
    pf = Portfolio(str(tmp_path / "p.json"), 50.0)
    p = _pos(stake=3.0, contracts=10, avg_price=0.30)
    p.mark_price = 2.00                       # absurd, as if mis-marked
    pf.open_position(p)
    assert pf.update_high_water_mark(marks_verified=False) is False
    assert pf.peak_bankroll == 50.0, "an unverified pass must not move the peak"

def test_fee_coefficient_is_preserved_on_the_position(tmp_path, monkeypatch):
    _, pf, _ = _run_first_scan(tmp_path, monkeypatch, *_no_entry_fixture())
    p = [x for x in pf["positions"] if x["status"] == "OPEN"][0]
    assert p["fee_coefficient"] == 0.06, (
        "the market's own fee coefficient must persist for later marks/exits")

def test_market_fee_coefficient_changes_the_mark():
    """Guards against silently dropping the coefficient on the refresh path."""
    b = Book(slug="t", state="MARKET_STATE_OPEN", bids=[Level(0.50, 500)],
             offers=[Level(0.52, 500)])
    default = _exit(b, "NO", 100)
    custom = _exit(b, "NO", 100, fee_coefficient=0.30)
    assert custom.net_cost < default.net_cost, (
        "a higher fee must reduce net exit proceeds; if these are equal the "
        "coefficient is being ignored")


# ---------------- final report reads the committed evaluation state ----------
def test_final_report_uses_persisted_evaluation_start(tmp_path, monkeypatch):
    from src import final_report, evaluation as ev
    state = tmp_path / "state"; state.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    _shutil.copy(os.path.join(ROOT, "config", "risk_config.json"),
                 tmp_path / "config" / "risk_config.json")

    ev_path = str(state / "evaluation.json")
    st, _ = ev.stamp_start_if_needed(ev_path, CFG)
    started = st["started_at"]
    ev.mark_complete(ev_path, CFG)

    json.dump({"cash": 50.0, "starting_bankroll": 50.0, "peak_bankroll": 50.0,
               "positions": []}, open(state / "portfolio.json", "w"))
    open(state / "audit.jsonl", "a").close()

    rep = final_report.build(str(tmp_path))
    assert rep["evaluation_start"] == started, (
        "the report must carry the PERSISTED start; reading the immutable "
        "config would report null")
    assert rep["evaluation_start"] is not None
    assert rep["evaluation_complete"] is True
    assert rep["evaluation_completed_at"]
    assert rep["evaluation_state_source"] == "state/evaluation.json"
    assert rep["schedule_evidence"]["window_start_persisted"] == started

def test_final_report_start_is_null_only_when_never_started(tmp_path):
    from src import final_report
    state = tmp_path / "state"; state.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    _shutil.copy(os.path.join(ROOT, "config", "risk_config.json"),
                 tmp_path / "config" / "risk_config.json")
    json.dump({"cash": 50.0, "starting_bankroll": 50.0, "peak_bankroll": 50.0,
               "positions": []}, open(state / "portfolio.json", "w"))
    open(state / "audit.jsonl", "a").close()
    rep = final_report.build(str(tmp_path))
    assert rep["evaluation_start"] is None and rep["evaluation_complete"] is False

def test_final_report_records_the_window_as_closed(tmp_path, monkeypatch):
    """The final report must not snapshot the window a moment before it closed."""
    from src import run_scan as rs, evaluation as ev
    from datetime import datetime, timezone, timedelta
    markets, books, obs, fcst = _no_entry_fixture()
    _, _, state = _run_first_scan(tmp_path, monkeypatch, markets, books, obs, fcst)

    ev_path = str(state / "evaluation.json")
    st = ev.load(ev_path, CFG)
    st["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=48.5)).isoformat()
    ev.save(ev_path, st)

    assert rs.main() == 0
    rep = json.load(open(state / "final_report.json"))
    assert rep["evaluation_complete"] is True
    assert rep["evaluation_completed_at"], "completion timestamp must be in the report"
    assert rep["evaluation_start"] == st["started_at"]


# ---------------------------------------------------------------------------
# END-TO-END SETTLEMENT TESTS
# ---------------------------------------------------------------------------
# These drive real scan cycles, NOT Portfolio.close_position() directly. The
# earlier defect was never in close_position() -- it was that settlement
# detection was gated behind a successful book fetch and two hard-coded book
# states, so resolved positions silently stayed open. Only an end-to-end scan
# exercises that path.
from tests.fixtures import polymarket_us_payloads as FIX
from src import settlement as _settle


def _scan_with_settlement(tmp_path, monkeypatch, *, side, book, settle_payload,
                          market_payload=None, book_raises=False,
                          second_book=None, second_settle="same",
                          market_envelope=None):
    """Open a position on scan 1, then re-scan with a settlement payload.

    Scan 1 always sees an open, tradeable market so a position is created.
    Scan 2 swaps in the settlement/book state under test.
    """
    from src import run_scan as rs, scanner as sc, pmus_client as pmc, nws_client as nwc

    state = tmp_path / "state"; state.mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    for f in ("risk_config.json", "stations.json"):
        _shutil.copy(os.path.join(ROOT, "config", f), tmp_path / "config" / f)
    for attr, val in [
        ("STATE_DIR", str(state)),
        ("PORTFOLIO_PATH", str(state / "portfolio.json")),
        ("AUDIT_PATH", str(state / "audit.jsonl")),
        ("STATUS_PATH", str(state / "status.json")),
        ("SHORTLIST_PATH", str(state / "shortlist.json")),
        ("OPPS_PATH", str(state / "opportunities.json")),
        ("EVAL_PATH", str(state / "evaluation.json")),
        ("HERE", str(tmp_path)),
        ("CFG_PATH", os.path.join(ROOT, "config", "risk_config.json")),
        ("STATIONS_PATH", os.path.join(ROOT, "config", "stations.json")),
    ]:
        monkeypatch.setattr(rs, attr, val)
    monkeypatch.setattr(rs.dashboard, "build", lambda root: None)

    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZI
    today = _dt.now(_ZI("America/New_York")).date()
    human = today.strftime("%B %-d, %Y")
    settles_at = (today + _td(days=1)).isoformat() + "T12:00:00Z"

    # Threshold and station data chosen so the engine takes the wanted side.
    if side == "YES":
        question = f"Will the high temperature in NYC be above 88F on {human}?"
        obs = {"station": "KNYC", "count": 30, "max_f": 95.0, "min_f": 74.0}
        entry_book = FIX.BOOK_OPEN            # YES cheap at 0.62
    else:
        question = f"Will the high temperature in NYC be above 105F on {human}?"
        obs = {"station": "KNYC", "count": 30, "max_f": 84.0, "min_f": 66.0}
        entry_book = FIX.BOOK_OPEN_HIGH       # YES expensive at 0.80 -> buy NO
    fcst = {"high_f": 96.0 if side == "YES" else 85.0, "low_f": 70.0, "hours": 24}

    market = {**FIX.MARKET_OPEN, "question": question, "endDate": settles_at,
              "rulesDisclaimer": "Station KNYC.", "description": "Station KNYC."}

    state_box = {"phase": 1}

    def _book(slug):
        if state_box["phase"] == 1:
            return entry_book
        if book_raises:
            raise pmc.DataError("no order book for a resolved market")
        return book

    def _settlement(slug):
        return None if state_box["phase"] == 1 else settle_payload

    def _market(slug):
        return market if state_box["phase"] == 1 else market_payload

    _mock_discovery(monkeypatch, [market])
    monkeypatch.setattr(pmc, "get_market_book", _book)
    monkeypatch.setattr(pmc, "get_market_settlement", _settlement)

    if market_envelope is not None:
        # WIRE MODE: mock the TRANSPORT so the real get_market_by_slug() runs
        # and must unwrap the documented {"market": {...}} envelope itself.
        # Mocking get_market_by_slug would bypass the very code under test.
        def _transport(path, **kw):
            if "/v1/market/slug/" in path:
                return ({"market": market} if state_box["phase"] == 1
                        else market_envelope)
            raise AssertionError(f"unexpected transport call: {path}")
        monkeypatch.setattr(pmc, "_get", _transport)
    else:
        monkeypatch.setattr(pmc, "get_market_by_slug", _market)
    monkeypatch.setattr(nwc, "observed_extremes_f", lambda s, t: obs)
    monkeypatch.setattr(nwc, "forecast_daily_extremes_f", lambda a, b, c: fcst)
    monkeypatch.setattr(sc, "pm", pmc); monkeypatch.setattr(sc, "nws", nwc)
    monkeypatch.setattr(rs, "pm", pmc)

    assert rs.main() == 0
    pf1 = json.load(open(state / "portfolio.json"))
    opened = [p for p in pf1["positions"] if p["status"] == "OPEN"]
    assert opened and opened[0]["side"] == side, (
        f"scan 1 must open a {side} position, got "
        f"{[(p['side'], p['status']) for p in pf1['positions']]}")

    state_box["phase"] = 2
    assert rs.main() == 0
    pf2 = json.load(open(state / "portfolio.json"))
    audit = [json.loads(l) for l in open(state / "audit.jsonl") if l.strip()]
    return opened[0], pf2, audit, state


def _only(pf):
    return pf["positions"][0]


# ---- the four payout permutations, each through a real scan ----------------

def test_e2e_yes_position_resolved_yes_pays_one(tmp_path, monkeypatch):
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_YES_WON, market_payload=FIX.MARKET_CLOSED_SETTLED)
    p = _only(pf)
    assert p["status"] == "RESOLVED" and p["outcome"] == 1
    assert p["exit_price"] == 1.0, "a winning YES contract pays $1.00"
    assert abs(p["realized_pnl"] - (p["contracts"] * 1.0 - p["stake"])) < 1e-9
    assert p["realized_pnl"] > 0
    assert any(a["event_type"] == "position_resolved" for a in audit)

def test_e2e_yes_position_resolved_no_pays_zero(tmp_path, monkeypatch):
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_NO_WON, market_payload=FIX.MARKET_CLOSED_SETTLED)
    p = _only(pf)
    assert p["status"] == "RESOLVED" and p["outcome"] == 0
    assert p["exit_price"] == 0.0, "a losing YES contract pays $0.00"
    assert abs(p["realized_pnl"] + p["stake"]) < 1e-9, "loss equals the full stake"

def test_e2e_no_position_resolved_yes_pays_zero(tmp_path, monkeypatch):
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="NO", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_YES_WON, market_payload=FIX.MARKET_CLOSED_SETTLED)
    p = _only(pf)
    assert p["side"] == "NO"
    assert p["status"] == "RESOLVED" and p["outcome"] == 1
    assert p["exit_price"] == 0.0, "YES won, so a NO contract pays $0.00"
    assert abs(p["realized_pnl"] + p["stake"]) < 1e-9

def test_e2e_no_position_resolved_no_pays_one(tmp_path, monkeypatch):
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="NO", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_NO_WON, market_payload=FIX.MARKET_CLOSED_SETTLED)
    p = _only(pf)
    assert p["side"] == "NO"
    assert p["status"] == "RESOLVED" and p["outcome"] == 0
    assert p["exit_price"] == 1.0, "NO won, so a NO contract pays $1.00"
    assert p["realized_pnl"] > 0

# ---- resolution must not depend on the order book -------------------------

def test_e2e_resolves_even_when_book_fetch_fails(tmp_path, monkeypatch):
    """THE regression: a resolved market that no longer serves a book."""
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=None, book_raises=True,
        settle_payload=FIX.SETTLEMENT_YES_WON, market_payload=FIX.MARKET_CLOSED_SETTLED)
    p = _only(pf)
    assert p["status"] == "RESOLVED" and p["exit_price"] == 1.0, (
        "settlement must be detected independently of the order book")

def test_e2e_resolves_on_unenumerated_terminal_state(tmp_path, monkeypatch):
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_UNKNOWN_STATE,
        settle_payload=FIX.SETTLEMENT_YES_WON, market_payload=FIX.MARKET_CLOSED_SETTLED)
    assert _only(pf)["status"] == "RESOLVED"

# ---- malformed / unsupported must preserve the position -------------------

def test_e2e_malformed_payload_keeps_position_open_and_logs(tmp_path, monkeypatch):
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_NON_NUMERIC,
        market_payload=FIX.MARKET_CLOSED_NO_PRICE)
    p = _only(pf)
    assert p["status"] == "OPEN", "must NOT close on an unparseable outcome"
    assert p["settlement_pending"] is True
    assert p["resolution_error"], "the failure reason must be recorded"
    assert p["realized_pnl"] is None, "no P&L may be fabricated"
    errs = [a for a in audit if a["event_type"] == "settlement_error"]
    assert errs, "a settlement_error audit record is required"
    assert "preserved" in errs[-1]["payload"]["action"]

def test_e2e_non_binary_settlement_is_not_guessed(tmp_path, monkeypatch):
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_NON_BINARY,
        market_payload=FIX.MARKET_CLOSED_SETTLED)
    p = _only(pf)
    assert p["status"] == "OPEN" and p["settlement_pending"] is True
    assert "UNSUPPORTED" in p["resolution_error"]

def test_e2e_book_disappearing_alone_never_closes_a_position(tmp_path, monkeypatch):
    """No settlement price + no book = still open. Never a fabricated close."""
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=None, book_raises=True,
        settle_payload=FIX.SETTLEMENT_UNSETTLED_404, market_payload=FIX.MARKET_OPEN)
    p = _only(pf)
    assert p["status"] == "OPEN" and p["realized_pnl"] is None
    assert not p["settlement_pending"], "an open market is not settlement_pending"

def test_e2e_unsettled_404_keeps_marking_normally(tmp_path, monkeypatch):
    entry, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_OPEN,
        settle_payload=FIX.SETTLEMENT_UNSETTLED_404, market_payload=FIX.MARKET_OPEN)
    p = _only(pf)
    assert p["status"] == "OPEN" and p["mark_price"] is not None
    assert p["mark_is_fallback"] is False

def test_final_report_excludes_stranded_settlements(tmp_path, monkeypatch):
    """A settlement failure must never be presented as unrealised performance."""
    from src import final_report, evaluation as ev
    state = tmp_path / "state"; state.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    _shutil.copy(os.path.join(ROOT, "config", "risk_config.json"),
                 tmp_path / "config" / "risk_config.json")
    ev.stamp_start_if_needed(str(state / "evaluation.json"), CFG)
    open(state / "audit.jsonl", "a").close()
    json.dump({"cash": 45.0, "starting_bankroll": 50.0, "peak_bankroll": 50.0,
               "positions": [
                   {"slug": "stuck", "side": "YES", "status": "OPEN",
                    "contracts": 10, "avg_price": 0.50, "stake": 5.0,
                    "mark_price": 0.95, "settlement_pending": True,
                    "resolution_error": "MALFORMED: unparseable",
                    "entry_fee": 0.1, "exit_fee": 0.0, "outcome": None,
                    "realized_pnl": None, "category": "weather",
                    "predicted_prob": 0.7, "opened_at": "t"}]},
              open(state / "portfolio.json", "w"))

    rep = final_report.build(str(tmp_path))
    assert rep["positions_awaiting_manual_settlement"] == 1
    assert rep["positions_still_open"] == 0
    assert rep["capital_in_unresolved_settlements"] == 5.0
    assert rep["settlement_failures"][0]["slug"] == "stuck"
    # 10 contracts marked at 0.95 = $9.50 must NOT appear in final equity
    assert abs(rep["final_equity"] - 45.0) < 1e-9, (
        f"stranded position leaked into final_equity: {rep['final_equity']}")
    assert "unaccounted for" in rep["verdict"]


# ---------------------------------------------------------------------------
# REGRESSION: the documented {"market": {...}} envelope
# ---------------------------------------------------------------------------
# GetMarketBySlugResponse has exactly one property, `market`. Reading `closed`,
# `active`, or `ep3Status` off the ENVELOPE yields None for every one of them,
# so a genuinely settled market classifies as UNRESOLVED and its position sits
# open forever with no alert -- a failure that is invisible because nothing
# raises. These tests drive the real client normalization by mocking the
# TRANSPORT (_get), not get_market_by_slug itself.
from src.pmus_client import SchemaError, _unwrap_market
from src.settlement import looks_like_envelope, market_looks_terminal


def test_client_unwraps_the_documented_envelope():
    inner = _unwrap_market(FIX.MARKET_TERMINAL_MINIMAL_ENVELOPE, "t")
    assert inner == {"active": False, "closed": True, "archived": False,
                     "ep3Status": "SETTLED"}

def test_client_refuses_undocumented_shapes():
    for bad in (FIX.MARKET_ENVELOPE_MISSING_KEY, FIX.MARKET_ENVELOPE_WRONG_TYPE,
                FIX.MARKET_ENVELOPE_NULL, ["not", "a", "dict"], "string", 42):
        with pytest.raises(SchemaError):
            _unwrap_market(bad, "t")

def test_get_market_by_slug_returns_inner_object(monkeypatch):
    from src import pmus_client as pmc
    monkeypatch.setattr(pmc, "_get", lambda path, **kw: FIX.MARKET_CLOSED_SETTLED_ENVELOPE)
    got = pmc.get_market_by_slug("x")
    assert got["closed"] is True and got["ep3Status"] == "SETTLED"
    assert "market" not in got, "the envelope must not leak through"

def test_get_market_by_slug_404_is_none(monkeypatch):
    from src import pmus_client as pmc
    def _raise(path, **kw):
        raise pmc.NotFound("404")
    monkeypatch.setattr(pmc, "_get", _raise)
    assert pmc.get_market_by_slug("x") is None

def test_envelope_is_detected_not_silently_misread():
    assert looks_like_envelope(FIX.MARKET_TERMINAL_MINIMAL_ENVELOPE)
    assert not looks_like_envelope(FIX.MARKET_CLOSED_SETTLED)
    with pytest.raises(ValueError, match="un-normalized"):
        market_looks_terminal(FIX.MARKET_TERMINAL_MINIMAL_ENVELOPE)

def test_terminality_read_from_inner_object():
    assert market_looks_terminal(FIX.MARKET_TERMINAL_MINIMAL_ENVELOPE["market"]) is True
    assert market_looks_terminal(FIX.MARKET_OPEN) is False

def test_classify_never_raises_on_an_envelope():
    """classify() is the safety net; it must degrade, not explode."""
    r = _settle.classify(None, FIX.MARKET_TERMINAL_MINIMAL_ENVELOPE,
                         "MARKET_STATE_TERMINATED")
    assert r.status == _settle.MALFORMED and "envelope" in r.detail


# ---- the three required end-to-end envelope scenarios ----------------------
# Each drives a real scan AND the real client normalization (wire mode), so a
# regression in either the envelope contract or the settlement path fails here.

def test_e2e_wrapped_terminal_market_with_404_goes_to_manual_settlement(
        tmp_path, monkeypatch):
    """settlement 404 + wrapped terminal market -> MALFORMED, held, alerted."""
    _, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_UNSETTLED_404,
        market_envelope=FIX.MARKET_TERMINAL_MINIMAL_ENVELOPE)
    p = _only(pf)
    assert p["status"] == "OPEN", "must not close without an authoritative outcome"
    assert p["settlement_pending"] is True
    assert "MALFORMED" in p["resolution_error"]
    assert p["realized_pnl"] is None, "no P&L may be fabricated"
    assert p["outcome"] is None
    errs = [a for a in audit if a["event_type"] == "settlement_error"]
    assert errs and "preserved" in errs[-1]["payload"]["action"]
    assert errs[-1]["payload"]["status"] == "MALFORMED"

def test_e2e_wrapped_terminal_market_with_malformed_value_goes_to_manual_settlement(
        tmp_path, monkeypatch):
    """Malformed settlement value + wrapped terminal market -> same path."""
    _, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="NO", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_NON_NUMERIC,
        market_envelope=FIX.MARKET_TERMINAL_MINIMAL_ENVELOPE)
    p = _only(pf)
    assert p["side"] == "NO"
    assert p["status"] == "OPEN" and p["settlement_pending"] is True
    assert p["realized_pnl"] is None and p["outcome"] is None
    assert "not numeric" in p["resolution_error"]
    assert any(a["event_type"] == "settlement_error" for a in audit)

def test_e2e_wrapped_open_market_with_404_keeps_marking(tmp_path, monkeypatch):
    """Wrapped OPEN market + 404 -> unresolved, still marking normally."""
    _, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_OPEN,
        settle_payload=FIX.SETTLEMENT_UNSETTLED_404,
        market_envelope=FIX.MARKET_OPEN_ENVELOPE)
    p = _only(pf)
    assert p["status"] == "OPEN"
    assert p["settlement_pending"] is False, "an open market is not pending settlement"
    assert not p["resolution_error"]
    assert p["mark_price"] is not None and p["mark_is_fallback"] is False
    assert "executable YES exit" in p["mark_source"]
    assert not [a for a in audit if a["event_type"] == "settlement_error"]

def test_e2e_wrapped_terminal_market_still_resolves_when_settled(
        tmp_path, monkeypatch):
    """Sanity: the envelope path must not break normal resolution."""
    _, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_YES_WON,
        market_envelope=FIX.MARKET_CLOSED_SETTLED_ENVELOPE)
    p = _only(pf)
    assert p["status"] == "RESOLVED" and p["exit_price"] == 1.0

def test_e2e_undocumented_market_shape_does_not_fabricate_a_close(
        tmp_path, monkeypatch):
    """A schema violation must strand nothing and close nothing silently."""
    _, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=FIX.BOOK_TERMINAL_EMPTY,
        settle_payload=FIX.SETTLEMENT_UNSETTLED_404,
        market_envelope=FIX.MARKET_ENVELOPE_MISSING_KEY)
    p = _only(pf)
    assert p["status"] == "OPEN" and p["realized_pnl"] is None
    st = json.load(open(tmp_path / "state" / "status.json"))
    assert any("market lookup" in e for e in st["errors"]), (
        "a schema violation must be surfaced as an error, not swallowed")


def test_e2e_terminality_from_market_payload_alone_when_book_is_gone(
        tmp_path, monkeypatch):
    """The case the envelope bug actually strands.

    When the book fetch FAILS there is no book_state to fall back on, so
    terminality can only come from the market payload's lifecycle fields. If
    those are read off the un-normalized envelope they are all None, the market
    reads as merely unresolved, and the position sits open forever with no
    alert and no audit record -- silently, because nothing raises.
    """
    _, pf, audit, _ = _scan_with_settlement(
        tmp_path, monkeypatch, side="YES", book=None, book_raises=True,
        settle_payload=FIX.SETTLEMENT_UNSETTLED_404,
        market_envelope=FIX.MARKET_TERMINAL_MINIMAL_ENVELOPE)
    p = _only(pf)
    assert p["status"] == "OPEN" and p["realized_pnl"] is None
    assert p["settlement_pending"] is True, (
        "a closed/settled market with no order book must be flagged for manual "
        "settlement, not left quietly unresolved")
    assert "MALFORMED" in p["resolution_error"]
    errs = [a for a in audit if a["event_type"] == "settlement_error"]
    assert errs, "the operator must be told; silence here is the whole failure"


# ---------------------------------------------------------------------------
# REGRESSION: the 4,000-market cap that hid every weather market
# ---------------------------------------------------------------------------
# The first live scan reported exactly 4,000 markets, all sports, zero weather.
# 4,000 = 40 pages x 100, the old max_pages cap. Nothing errored; the dashboard
# showed a healthy scan; the weather strategy simply never saw its universe.
from src import discovery as _disc


def _sports(n, start=0):
    return [{"slug": f"nba-game-{i}", "id": 100000 + i, "question": f"Team A vs B #{i}",
             "category": "sports", "active": True, "closed": False,
             "volume": 50000, "endDate": "2026-12-01T00:00:00Z"}
            for i in range(start, start + n)]


def _weather(n=5):
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZI
    today = _dt.now(_ZI("America/New_York")).date()
    human = today.strftime("%B %-d, %Y")
    settles = (today + _td(days=1)).isoformat() + "T12:00:00Z"
    return [{"slug": f"nyc-high-above-{80 + i}", "id": 900000 + i,
             "question": f"Will the high temperature in NYC be above {80 + i}F on {human}?",
             "description": "Station KNYC.", "rulesDisclaimer": "Station KNYC.",
             "category": "weather", "active": True, "closed": False,
             "volume": 40000, "endDate": settles, "minimumTradeQty": 1,
             "feeCoefficient": 0.06}
            for i in range(n)]


# 4,500 sports markets, then the weather markets -- i.e. weather sits FAR
# beyond where the old 40-page cap stopped looking.
BIG_SPORTS = _sports(4500)
BIG_WEATHER = _weather(5)
BIG_UNIVERSE = BIG_SPORTS + BIG_WEATHER


def _install_big_universe(monkeypatch, *, category_filter_works=True,
                          tags_work=True, search_works=True):
    """Mock /v1/markets with real offset pagination over 4,505 markets."""
    from src import pmus_client as pmc

    calls = {"unfiltered_pages": 0, "category_pages": 0, "tag_pages": 0, "search": 0}

    def _get_markets(limit=100, offset=0, active=True, closed=False,
                     categories=None, tagIds=None, **kw):
        if categories:
            calls["category_pages"] += 1
            if not category_filter_works:
                return []
            pool = [m for m in BIG_UNIVERSE
                    if m["category"].lower() in {c.lower() for c in categories}]
        elif tagIds:
            calls["tag_pages"] += 1
            if not tags_work:
                return []
            pool = BIG_WEATHER
        else:
            calls["unfiltered_pages"] += 1
            pool = BIG_UNIVERSE
        return pool[offset:offset + limit]

    def _get_tags(query=None, slug=None, limit=100, offset=0):
        if not tags_work:
            raise pmc.DataError("tags endpoint unavailable")
        return [{"id": 77, "slug": "weather", "label": "Weather"}]

    def _search(query, limit=100, page=1):
        calls["search"] += 1
        if not search_works:
            raise pmc.DataError("search unavailable")
        return BIG_WEATHER if "temp" in query.lower() or "weather" in query.lower() else []

    monkeypatch.setattr(pmc, "get_markets", _get_markets)
    monkeypatch.setattr(pmc, "get_tags", _get_tags)
    monkeypatch.setattr(pmc, "search_markets", _search)
    monkeypatch.setattr(_disc, "pm", pmc)
    return calls


def test_weather_found_beyond_the_old_4000_cap(monkeypatch):
    """The headline regression: weather sits past market 4,000 and is still found."""
    _install_big_universe(monkeypatch)
    res = _disc.discover(page_size=100, weather_max_pages=50, broad_max_pages=200)
    slugs = {m["slug"] for m in res.weather}
    assert slugs == {m["slug"] for m in BIG_WEATHER}, (
        f"weather discovery missed markets: {slugs}")
    assert res.weather_complete is True

def test_weather_is_first_in_the_universe(monkeypatch):
    """A truncated broad scan must never crowd weather out of evaluation."""
    _install_big_universe(monkeypatch)
    res = _disc.discover(page_size=100, weather_max_pages=50, broad_max_pages=5)
    head = [m["slug"] for m in res.universe[:len(BIG_WEATHER)]]
    assert set(head) == {m["slug"] for m in BIG_WEATHER}
    assert res.broad.exhausted is False, "a 5-page scan of 4,505 markets is truncated"

def test_broad_scan_reports_truncation_honestly(monkeypatch):
    _install_big_universe(monkeypatch)
    res = _disc.discover(page_size=100, weather_max_pages=50, broad_max_pages=40)
    assert len(res.broad.markets) == 4000, "reproduces the exact reported symptom"
    assert res.broad.exhausted is False
    assert "TRUNCATED" in res.broad.error

def test_broad_scan_reports_completion_when_it_finishes(monkeypatch):
    _install_big_universe(monkeypatch)
    res = _disc.discover(page_size=100, weather_max_pages=50, broad_max_pages=200)
    assert res.broad.exhausted is True and res.broad.error == ""
    assert len(res.broad.markets) == len(BIG_UNIVERSE)

def test_weather_survives_a_broken_category_filter(monkeypatch):
    """If the category label is wrong, tags and search must still find weather."""
    _install_big_universe(monkeypatch, category_filter_works=False)
    res = _disc.discover(page_size=100, weather_max_pages=50, broad_max_pages=200)
    assert {m["slug"] for m in res.weather} == {m["slug"] for m in BIG_WEATHER}

def test_weather_survives_a_broken_tags_endpoint(monkeypatch):
    _install_big_universe(monkeypatch, tags_work=False)
    res = _disc.discover(page_size=100, weather_max_pages=50, broad_max_pages=200)
    assert {m["slug"] for m in res.weather} == {m["slug"] for m in BIG_WEATHER}

def test_total_weather_discovery_failure_is_flagged(monkeypatch):
    _install_big_universe(monkeypatch, category_filter_works=False,
                          tags_work=False, search_works=False)
    res = _disc.discover(page_size=100, weather_max_pages=50, broad_max_pages=200)
    assert res.weather == [] and res.weather_complete is False

def test_pagination_stops_on_a_non_advancing_offset(monkeypatch):
    """A server ignoring `offset` must not spin forever, nor look complete."""
    from src import pmus_client as pmc
    monkeypatch.setattr(pmc, "get_markets",
                        lambda limit=100, offset=0, **kw: _sports(limit))
    monkeypatch.setattr(_disc, "pm", pmc)
    res = _disc.paginate(lambda lim, off: pmc.get_markets(limit=lim, offset=off),
                         page_size=100, max_pages=50)
    assert res.exhausted is False and "stopped advancing" in res.error
    assert res.pages_fetched < 50

def test_discovery_dedupes_across_strategies(monkeypatch):
    _install_big_universe(monkeypatch)
    res = _disc.discover(page_size=100, weather_max_pages=50, broad_max_pages=200)
    slugs = [m["slug"] for m in res.weather]
    assert len(slugs) == len(set(slugs))
    universe = [m["slug"] for m in res.universe]
    assert len(universe) == len(set(universe))


def test_e2e_weather_evaluated_despite_4500_sports_markets_first(
        tmp_path, monkeypatch):
    """Full scan over 4,505 markets where weather sits past the old cap.

    Discovery alone is not enough -- the weather markets must reach the
    evaluator and be priced. This asserts on the recorded opportunities, so a
    regression that finds them but drops them before evaluation still fails.
    """
    from src import run_scan as rs, scanner as sc, pmus_client as pmc, nws_client as nwc

    state = tmp_path / "state"; state.mkdir(parents=True)
    (tmp_path / "docs").mkdir(); (tmp_path / "config").mkdir()
    # This test is about BROAD-scan behaviour, so it enables the switch that
    # ships off. It doubles as proof the switch still works.
    cfg = dict(CFG); cfg["broad_scan_enabled"] = True
    cfg_path = tmp_path / "config" / "risk_config.json"
    json.dump(cfg, open(cfg_path, "w"))
    _shutil.copy(os.path.join(ROOT, "config", "stations.json"),
                 tmp_path / "config" / "stations.json")
    for attr, val in [
        ("STATE_DIR", str(state)),
        ("PORTFOLIO_PATH", str(state / "portfolio.json")),
        ("AUDIT_PATH", str(state / "audit.jsonl")),
        ("STATUS_PATH", str(state / "status.json")),
        ("SHORTLIST_PATH", str(state / "shortlist.json")),
        ("OPPS_PATH", str(state / "opportunities.json")),
        ("EVAL_PATH", str(state / "evaluation.json")),
        ("HERE", str(tmp_path)),
        ("CFG_PATH", str(cfg_path)),
        ("STATIONS_PATH", os.path.join(ROOT, "config", "stations.json")),
    ]:
        monkeypatch.setattr(rs, attr, val)
    monkeypatch.setattr(rs.dashboard, "build", lambda root: None)

    _install_big_universe(monkeypatch)

    books = {m["slug"]: {"marketData": {
        "marketSlug": m["slug"], "state": "MARKET_STATE_OPEN",
        "transactTime": "2026-08-25T00:00:00Z",
        "bids": [{"px": 0.60, "qty": 600}],
        "offers": [{"px": 0.62, "qty": 600}]}} for m in BIG_UNIVERSE}
    monkeypatch.setattr(pmc, "get_market_book", lambda slug: books[slug])
    monkeypatch.setattr(pmc, "get_market_settlement", lambda slug: None)
    monkeypatch.setattr(pmc, "get_market_by_slug", lambda slug: None)
    monkeypatch.setattr(nwc, "observed_extremes_f",
                        lambda s, t: {"station": "KNYC", "count": 30,
                                      "max_f": 95.0, "min_f": 74.0})
    monkeypatch.setattr(nwc, "forecast_daily_extremes_f",
                        lambda a, b, c: {"high_f": 96.0, "low_f": 74.0, "hours": 24})
    monkeypatch.setattr(sc, "pm", pmc); monkeypatch.setattr(sc, "nws", nwc)
    monkeypatch.setattr(rs, "pm", pmc)

    assert rs.main() == 0

    opps = json.load(open(state / "opportunities.json"))
    status = json.load(open(state / "status.json"))
    by_slug = {o["slug"]: o for o in opps}

    # Every weather market must have been CONSIDERED...
    for m in BIG_WEATHER:
        assert m["slug"] in by_slug, (
            f"{m['slug']} never reached the evaluator -- discovery regression")

    # ...and actually EVALUATED, not merely listed.
    priced = [by_slug[m["slug"]] for m in BIG_WEATHER
              if by_slug[m["slug"]].get("fair_probability") is not None]
    assert priced, (
        "weather markets were listed but none were priced; they reached the "
        "loop and were dropped before valuation")
    assert any(o.get("station") == "KNYC" for o in priced)

    assert status["weather_markets_discovered"] == len(BIG_WEATHER)
    assert status["weather_discovery_complete"] is True
    assert status["markets_scanned"] > 4000, (
        f"only {status['markets_scanned']} scanned; the cap is back")

def test_e2e_weather_evaluated_even_when_broad_scan_is_capped(
        tmp_path, monkeypatch):
    """Weather must survive a broad scan that truncates, and be labelled so."""
    from src import run_scan as rs, scanner as sc, pmus_client as pmc, nws_client as nwc

    state = tmp_path / "state"; state.mkdir(parents=True)
    (tmp_path / "docs").mkdir(); (tmp_path / "config").mkdir()
    cfg = json.load(open(os.path.join(ROOT, "config", "risk_config.json")))
    cfg["broad_scan_enabled"] = True          # this test is about the broad scan
    cfg["broad_scan_max_pages"] = 5           # force truncation
    cfg_path = tmp_path / "config" / "risk_config.json"
    json.dump(cfg, open(cfg_path, "w"))
    _shutil.copy(os.path.join(ROOT, "config", "stations.json"),
                 tmp_path / "config" / "stations.json")
    for attr, val in [
        ("STATE_DIR", str(state)),
        ("PORTFOLIO_PATH", str(state / "portfolio.json")),
        ("AUDIT_PATH", str(state / "audit.jsonl")),
        ("STATUS_PATH", str(state / "status.json")),
        ("SHORTLIST_PATH", str(state / "shortlist.json")),
        ("OPPS_PATH", str(state / "opportunities.json")),
        ("EVAL_PATH", str(state / "evaluation.json")),
        ("HERE", str(tmp_path)),
        ("CFG_PATH", str(cfg_path)),
        ("STATIONS_PATH", os.path.join(ROOT, "config", "stations.json")),
    ]:
        monkeypatch.setattr(rs, attr, val)
    monkeypatch.setattr(rs.dashboard, "build", lambda root: None)
    _install_big_universe(monkeypatch)
    books = {m["slug"]: {"marketData": {
        "marketSlug": m["slug"], "state": "MARKET_STATE_OPEN",
        "transactTime": "2026-08-25T00:00:00Z",
        "bids": [{"px": 0.60, "qty": 600}], "offers": [{"px": 0.62, "qty": 600}]}}
        for m in BIG_UNIVERSE}
    monkeypatch.setattr(pmc, "get_market_book", lambda slug: books[slug])
    monkeypatch.setattr(pmc, "get_market_settlement", lambda slug: None)
    monkeypatch.setattr(pmc, "get_market_by_slug", lambda slug: None)
    monkeypatch.setattr(nwc, "observed_extremes_f",
                        lambda s, t: {"station": "KNYC", "count": 30,
                                      "max_f": 95.0, "min_f": 74.0})
    monkeypatch.setattr(nwc, "forecast_daily_extremes_f",
                        lambda a, b, c: {"high_f": 96.0, "low_f": 74.0, "hours": 24})
    monkeypatch.setattr(sc, "pm", pmc); monkeypatch.setattr(sc, "nws", nwc)
    monkeypatch.setattr(rs, "pm", pmc)

    assert rs.main() == 0
    opps = {o["slug"] for o in json.load(open(state / "opportunities.json"))}
    status = json.load(open(state / "status.json"))
    for m in BIG_WEATHER:
        assert m["slug"] in opps, "a capped broad scan must not hide weather"
    assert status["broad_scan_exhausted"] is False
    assert any("TRUNCATED" in w for w in status["warnings"]), (
        "a truncated scan must be labelled honestly, not reported as complete")


# ---------------------------------------------------------------------------
# WEATHER-ONLY RUN: the broad scan is off by default
# ---------------------------------------------------------------------------
# Non-weather markets are never auto-traded, so paging up to 20,000 of them
# every 10 minutes would spend thousands of API requests a day buying nothing.
# Explicit weather discovery is unaffected and still runs to exhaustion.

def test_broad_scan_is_disabled_by_default_in_shipped_config():
    assert CFG["broad_scan_enabled"] is False, (
        "the 48-hour evaluation is weather-only; the broad scan must ship off")
    assert CFG["weather_search_enabled"] is True
    assert CFG["weather_discovery_max_pages"] >= 50

def test_weather_only_never_calls_the_unfiltered_endpoint(monkeypatch):
    """The whole point: zero unfiltered pages fetched."""
    calls = _install_big_universe(monkeypatch)
    res = _disc.discover(page_size=100, weather_max_pages=50,
                         broad_max_pages=200, include_broad=False)
    assert calls["unfiltered_pages"] == 0, (
        f"{calls['unfiltered_pages']} unfiltered pages fetched in a weather-only run")
    assert calls["category_pages"] > 0, "the category filter must still be queried"
    assert {m["slug"] for m in res.weather} == {m["slug"] for m in BIG_WEATHER}
    assert res.weather_complete is True
    assert res.broad is None and res.other == []

def test_weather_only_universe_is_exactly_the_weather_markets(monkeypatch):
    _install_big_universe(monkeypatch)
    res = _disc.discover(page_size=100, weather_max_pages=50, include_broad=False)
    assert {m["slug"] for m in res.universe} == {m["slug"] for m in BIG_WEATHER}

def test_broad_scan_switch_still_works_when_enabled(monkeypatch):
    """The switch is documented and must remain functional for later use."""
    calls = _install_big_universe(monkeypatch)
    res = _disc.discover(page_size=100, weather_max_pages=50,
                         broad_max_pages=200, include_broad=True)
    assert calls["unfiltered_pages"] > 0
    assert res.broad is not None and res.broad.exhausted is True
    assert len(res.universe) == len(BIG_UNIVERSE)

def test_e2e_weather_only_scan_reports_its_scope(tmp_path, monkeypatch):
    """A real scan with the shipped config: weather evaluated, scope labelled."""
    from src import run_scan as rs, scanner as sc, pmus_client as pmc, nws_client as nwc

    state = tmp_path / "state"; state.mkdir(parents=True)
    (tmp_path / "docs").mkdir(); (tmp_path / "config").mkdir()
    for f in ("risk_config.json", "stations.json"):
        _shutil.copy(os.path.join(ROOT, "config", f), tmp_path / "config" / f)
    for attr, val in [
        ("STATE_DIR", str(state)),
        ("PORTFOLIO_PATH", str(state / "portfolio.json")),
        ("AUDIT_PATH", str(state / "audit.jsonl")),
        ("STATUS_PATH", str(state / "status.json")),
        ("SHORTLIST_PATH", str(state / "shortlist.json")),
        ("OPPS_PATH", str(state / "opportunities.json")),
        ("EVAL_PATH", str(state / "evaluation.json")),
        ("HERE", str(tmp_path)),
        ("CFG_PATH", os.path.join(ROOT, "config", "risk_config.json")),
        ("STATIONS_PATH", os.path.join(ROOT, "config", "stations.json")),
    ]:
        monkeypatch.setattr(rs, attr, val)
    monkeypatch.setattr(rs.dashboard, "build", lambda root: None)

    calls = _install_big_universe(monkeypatch)
    books = {m["slug"]: {"marketData": {
        "marketSlug": m["slug"], "state": "MARKET_STATE_OPEN",
        "transactTime": "2026-08-25T00:00:00Z",
        "bids": [{"px": 0.60, "qty": 600}], "offers": [{"px": 0.62, "qty": 600}]}}
        for m in BIG_UNIVERSE}
    monkeypatch.setattr(pmc, "get_market_book", lambda slug: books[slug])
    monkeypatch.setattr(pmc, "get_market_settlement", lambda slug: None)
    monkeypatch.setattr(pmc, "get_market_by_slug", lambda slug: None)
    monkeypatch.setattr(nwc, "observed_extremes_f",
                        lambda s, t: {"station": "KNYC", "count": 30,
                                      "max_f": 95.0, "min_f": 74.0})
    monkeypatch.setattr(nwc, "forecast_daily_extremes_f",
                        lambda a, b, c: {"high_f": 96.0, "low_f": 74.0, "hours": 24})
    monkeypatch.setattr(sc, "pm", pmc); monkeypatch.setattr(sc, "nws", nwc)
    monkeypatch.setattr(rs, "pm", pmc)

    assert rs.main() == 0
    status = json.load(open(state / "status.json"))
    opps = json.load(open(state / "opportunities.json"))

    assert calls["unfiltered_pages"] == 0, "weather-only run touched the broad listing"
    assert status["scan_scope"] == "WEATHER_ONLY"
    assert status["broad_scan_enabled"] is False
    assert status["weather_markets_discovered"] == len(BIG_WEATHER)
    assert status["weather_discovery_complete"] is True
    assert status["markets_scanned"] == len(BIG_WEATHER), (
        "a weather-only scan should evaluate only weather markets")

    by_slug = {o["slug"]: o for o in opps}
    for m in BIG_WEATHER:
        assert m["slug"] in by_slug
    assert any(by_slug[m["slug"]].get("fair_probability") is not None
               for m in BIG_WEATHER), "weather markets must still be priced"
    # No sports market should have been considered at all.
    assert not [o for o in opps if o["slug"].startswith("nba-game-")]

def test_weather_only_does_not_relax_any_risk_control():
    """Scope is not a risk setting. Nothing here may drift."""
    assert CFG["mode"] == "PAPER"
    assert CFG["live_trading_enabled"] is False
    assert CFG["starting_bankroll"] == 50.00
    assert CFG["kelly_fraction"] == 0.25
    assert CFG["max_position_pct"] == 0.06
    assert CFG["max_total_exposure_pct"] == 0.20
    assert CFG["max_correlated_pct"] == 0.08
    assert CFG["min_net_edge_pp"] == 8.0
    assert CFG["max_spread"] == 0.06
    assert CFG["daily_loss_halt_pct"] == 0.10
    assert CFG["max_drawdown_halt_pct"] == 0.20
    assert CFG["evaluation_hours"] == 48
    assert CFG["emergency_stop"] is False
