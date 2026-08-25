"""
Static dashboard generator -> docs/index.html (served by GitHub Pages).

Deliberately a static file: it cannot place an order, cannot mutate state,
and works with no server. The emergency stop is a documented one-click GitHub
action rather than a button wired to a live endpoint, because a public web
page must never be able to move money.
"""
import html
import json
import os
from datetime import datetime, timezone


def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def _pct(x):
    return "—" if x is None else f"{x*100:.2f}%"


def _money(x):
    return "—" if x is None else f"${x:,.2f}"


def build(root: str) -> str:
    st = _load(os.path.join(root, "state", "status.json"), {})
    pf = _load(os.path.join(root, "state", "portfolio.json"), {})
    opps = _load(os.path.join(root, "state", "opportunities.json"), [])
    cfg = _load(os.path.join(root, "config", "risk_config.json"), {})

    positions = pf.get("positions", [])
    open_pos = [p for p in positions if p.get("status") == "OPEN"]
    closed = [p for p in positions if p.get("status") != "OPEN"]

    equity = st.get("equity", pf.get("cash", cfg.get("starting_bankroll", 0)))
    start = pf.get("starting_bankroll", cfg.get("starting_bankroll", 50))
    realized = st.get("realized_pnl", 0.0)
    unrealized = st.get("unrealized_pnl", 0.0)
    dd = st.get("drawdown_pct", 0.0)
    halted = st.get("halted", False)
    chain = st.get("audit_chain", {})

    stranded = [p for p in open_pos if p.get("settlement_pending")]
    traded = [o for o in opps if o.get("traded")]
    skipped = [o for o in opps if not o.get("traded")]

    wins = [p for p in closed if (p.get("realized_pnl") or 0) > 0]
    win_rate = (len(wins) / len(closed)) if closed else None

    # calibration
    bins = [(0.0,0.2),(0.2,0.4),(0.4,0.6),(0.6,0.8),(0.8,1.0)]
    calib = []
    resolved = [p for p in positions if p.get("outcome") is not None]
    for lo, hi in bins:
        grp = [p for p in resolved if lo <= p.get("predicted_prob", -1) < hi]
        if grp:
            calib.append((f"{int(lo*100)}–{int(hi*100)}%", len(grp),
                          sum(p["predicted_prob"] for p in grp)/len(grp),
                          sum(1 for p in grp if p["outcome"] == 1)/len(grp)))
        else:
            calib.append((f"{int(lo*100)}–{int(hi*100)}%", 0, None, None))

    cat = {}
    for p in closed:
        c = p.get("category", "other")
        cat[c] = cat.get(c, 0.0) + (p.get("realized_pnl") or 0.0)

    fees = sum((p.get("entry_fee") or 0) + (p.get("exit_fee") or 0) for p in positions)

    stranded_banner = ""
    if [p for p in positions if p.get("status") == "OPEN" and p.get("settlement_pending")]:
        rows = "".join(
            f"<li><strong>{html.escape(str(p.get('slug')))}</strong> "
            f"({html.escape(str(p.get('side')))}, {_money(p.get('stake'))}): "
            f"{html.escape(str(p.get('resolution_error','')))[:220]}</li>"
            for p in positions
            if p.get("status") == "OPEN" and p.get("settlement_pending"))
        stranded_banner = (
            '<div class="banner halt"><strong>SETTLEMENT NEEDS ATTENTION.</strong> '
            'These markets look terminal but their outcome could not be parsed. '
            'The positions are deliberately left OPEN — nothing is closed on a '
            'guess — and they are excluded from every performance figure below.'
            f'<ul style="margin:8px 0 0">{rows}</ul></div>')

    weather_only = not st.get("broad_scan_enabled", False)
    if weather_only:
        scope_note = "weather markets only"
    elif st.get("broad_scan_exhausted"):
        scope_note = "weather + complete broad listing"
    elif st.get("broad_scan_exhausted") is False:
        scope_note = "BROAD SCAN TRUNCATED"
    else:
        scope_note = ""

    disc_banner = ""
    if st.get("weather_markets_discovered") == 0 and st.get("last_successful_scan"):
        disc_banner = (
            '<div class="banner halt"><strong>NO WEATHER MARKETS FOUND.</strong> '
            f'The scan saw {st.get("markets_scanned")} markets but zero weather '
            'markets, so the weather strategy had nothing to evaluate. This is '
            'the condition that a broad, capped market listing hides — check the '
            'discovery sources below.</div>')
    elif weather_only:
        disc_banner = (
            '<div class="banner ok"><strong>Weather-only run.</strong> This '
            'evaluation deliberately scans weather markets only, discovered '
            'explicitly by category, tag and search and paginated to '
            'completion. The broad all-markets scan is switched off: nothing '
            'outside weather is auto-traded, so paging tens of thousands of '
            'unrelated markets every 10 minutes would buy nothing. Re-enable it '
            'via <code>"broad_scan_enabled": true</code> only alongside a '
            'deliberately designed non-weather strategy.</div>')
    elif st.get("broad_scan_exhausted") is False:
        disc_banner = (
            '<div class="banner" style="background:#3d2f14;border-color:var(--warn)">'
            '<strong>Broad market scan truncated.</strong> The all-markets listing '
            'hit its page ceiling and is NOT a complete view. Weather markets are '
            'discovered separately and exhaustively, so the strategy is unaffected; '
            'the non-weather shortlist is a partial sample.</div>')

    status_banner = (
        f'<div class="banner halt"><strong>HALTED — no new positions.</strong> '
        f'{html.escape(st.get("halt_reason","") or "")}</div>'
        if halted else
        '<div class="banner ok"><strong>Running.</strong> Paper mode. '
        'No live orders are possible: this repository contains no order-placement code.</div>'
    )

    def opp_rows(items, limit=250):
        rows = []
        for o in items[:limit]:
            ev = o.get("evidence") or []
            eviden = "<br>".join(
                html.escape(f"{e.get('source','')}: {e.get('detail', e.get('error',''))}")
                + (f" <a href='{html.escape(e['url'])}'>link</a>" if e.get("url") else "")
                for e in ev[:3]) or "—"
            est = o.get("estimate") or {}
            rows.append(f"""<tr>
<td><a href="{html.escape(o.get('url',''))}">{html.escape(str(o.get('question',''))[:110])}</a>
<div class="sub">{html.escape(o.get('category','')or'')} · {html.escape(str(o.get('slug',''))[:60])}</div></td>
<td class="num">{o.get('best_bid','—')} / {o.get('best_ask','—')}</td>
<td class="num">{o.get('spread','—')}</td>
<td class="num">{_pct(o.get('fair_probability'))}</td>
<td class="num">{('%.1fpp'%o['net_edge_pp']) if o.get('net_edge_pp') is not None else '—'}</td>
<td>{html.escape(str(o.get('reason','')))}</td>
<td class="ev">{eviden}<div class="sub">{html.escape(est.get('method',''))} {html.escape(est.get('notes','')[:180])}</div></td>
</tr>""")
        return "\n".join(rows) or '<tr><td colspan="7" class="empty">No markets recorded yet.</td></tr>'

    pos_rows = "\n".join(f"""<tr>
<td><a href="{html.escape(p.get('url',''))}">{html.escape(str(p.get('question',''))[:100])}</a></td>
<td class="num">{p.get('contracts')}</td>
<td class="num">{p.get('avg_price')}</td>
<td class="num">{_money(p.get('stake'))}</td>
<td class="num">{('%.4f' % p['mark_price']) if p.get('mark_price') is not None else '—'}{' <span style="color:var(--warn)">&#9888;</span>' if p.get('mark_is_fallback') else ''}</td>
<td class="num">{_pct(p.get('predicted_prob'))}</td>
<td class="num">{('%.1fpp'%p['edge_pp_at_entry']) if p.get('edge_pp_at_entry') is not None else '—'}</td>
<td class="sub">{html.escape(str(p.get('opened_at',''))[:16])}</td></tr>"""
        for p in open_pos) or '<tr><td colspan="8" class="empty">No open positions.</td></tr>'

    closed_rows = "\n".join(f"""<tr>
<td><a href="{html.escape(p.get('url',''))}">{html.escape(str(p.get('question',''))[:100])}</a></td>
<td class="num">{p.get('contracts')}</td>
<td class="num">{p.get('avg_price')}</td>
<td class="num">{p.get('exit_price','—')}</td>
<td class="num">{_pct(p.get('predicted_prob'))}</td>
<td class="num">{p.get('outcome','—')}</td>
<td class="num {'pos' if (p.get('realized_pnl') or 0)>0 else 'neg'}">{_money(p.get('realized_pnl'))}</td>
</tr>""" for p in closed) or '<tr><td colspan="7" class="empty">No completed trades yet.</td></tr>'

    calib_rows = "\n".join(
        f"<tr><td>{b}</td><td class='num'>{n}</td><td class='num'>{_pct(pr)}</td>"
        f"<td class='num'>{_pct(ac)}</td></tr>" for b, n, pr, ac in calib)

    cat_rows = "\n".join(
        f"<tr><td>{html.escape(k)}</td><td class='num {'pos' if v>0 else 'neg'}'>{_money(v)}</td></tr>"
        for k, v in sorted(cat.items())) or '<tr><td colspan="2" class="empty">No resolved trades yet.</td></tr>'

    d = st.get("discovery") or {}
    disc_rows = "\n".join(
        f"<tr><td>{html.escape(str(x.get('source','')))}</td>"
        f"<td class='num'>{x.get('market_count',0)}</td>"
        f"<td class='num'>{x.get('pages_fetched',0)}</td>"
        f"<td class='num' style='color:{'var(--pos)' if x.get('exhausted') else 'var(--warn)'}'>"
        f"{'yes' if x.get('exhausted') else 'no'}</td>"
        f"<td class='sub'>{html.escape(str(x.get('error','') or ''))[:160]}</td></tr>"
        for x in (list(d.get("weather_sources") or [])
                  + ([d["broad_scan"]] if d.get("broad_scan") else []))
    ) or '<tr><td colspan="5" class="empty">No discovery data yet.</td></tr>'

    scope_label = ("weather markets only" if weather_only
                   else "weather + broad market scan")
    broad_state = ("The broad all-markets scan is <strong>disabled</strong> for "
                   "this weather-only run." if weather_only else
                   "The broad all-markets scan runs afterwards and is additive only.")

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Cowork Agent</title>
<style>
:root {{ --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e6e9ef; --mut:#8b93a7;
        --pos:#3fb950; --neg:#f85149; --warn:#d29922; --acc:#58a6ff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
        font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
.wrap {{ max-width:1400px; margin:0 auto; padding:24px; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
h2 {{ font-size:15px; margin:32px 0 10px; color:var(--mut);
      text-transform:uppercase; letter-spacing:.06em; }}
.sub {{ color:var(--mut); font-size:12px; }}
.banner {{ padding:12px 16px; border-radius:8px; margin:16px 0; border:1px solid; }}
.banner.ok {{ background:#0d2818; border-color:#1f6f3d; }}
.banner.halt {{ background:#3d1418; border-color:var(--neg); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
.card .label {{ color:var(--mut); font-size:11px; text-transform:uppercase; letter-spacing:.05em; }}
.card .value {{ font-size:22px; font-weight:600; margin-top:6px; }}
.pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
table {{ width:100%; border-collapse:collapse; background:var(--panel);
         border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
th {{ text-align:left; padding:9px 10px; background:#1c2029; color:var(--mut);
      font-size:11px; text-transform:uppercase; letter-spacing:.05em;
      border-bottom:1px solid var(--line); }}
td {{ padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.ev {{ font-size:12px; color:var(--mut); max-width:320px; }}
td.empty {{ color:var(--mut); text-align:center; padding:20px; }}
a {{ color:var(--acc); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.scroll {{ overflow-x:auto; }}
.stop {{ background:#3d1418; border:1px solid var(--neg); border-radius:8px;
         padding:16px; margin:16px 0; }}
.stop code {{ background:#00000055; padding:2px 6px; border-radius:4px; }}
</style></head><body><div class="wrap">

<h1>Polymarket Cowork Agent</h1>
<div class="sub">Paper trading · Polymarket US (CFTC-regulated) · {scope_label} · generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>

{status_banner}
{stranded_banner}
{disc_banner}

<div class="stop">
<strong>■ EMERGENCY STOP</strong>
<p style="margin:8px 0 0">Set <code>"emergency_stop": true</code> in
<code>config/risk_config.json</code> and commit — the next scan halts and opens nothing.
To stop the schedule entirely, disable the workflow in the repository's
<strong>Actions</strong> tab. Both take effect without any code change.</p>
<p class="sub" style="margin:8px 0 0">This page is static and deliberately cannot
trigger any action. No order-placement code exists in this repository.</p>
</div>

<h2>Bankroll</h2>
<div class="grid">
<div class="card"><div class="label">Paper bankroll</div><div class="value">{_money(equity)}</div><div class="sub">started {_money(start)}</div></div>
<div class="card"><div class="label">Realized P&amp;L</div><div class="value {'pos' if realized>0 else 'neg' if realized<0 else ''}">{_money(realized)}</div></div>
<div class="card"><div class="label">Unrealized P&amp;L</div><div class="value {'pos' if unrealized>0 else 'neg' if unrealized<0 else ''}">{_money(unrealized)}</div></div>
<div class="card"><div class="label">Drawdown</div><div class="value">{_pct(dd)}</div><div class="sub">halt at {_pct(cfg.get('max_drawdown_halt_pct'))}</div></div>
<div class="card"><div class="label">Win rate</div><div class="value">{_pct(win_rate)}</div><div class="sub">{len(closed)} completed</div></div>
<div class="card"><div class="label">Fees + slippage paid</div><div class="value">{_money(fees)}</div></div>
</div>

<h2>System status</h2>
<div class="grid">
<div class="card"><div class="label">Last successful scan</div><div class="value" style="font-size:15px">{html.escape(str(st.get('last_successful_scan','never'))[:19])}</div></div>
<div class="card"><div class="label">Markets scanned</div><div class="value">{st.get('markets_scanned','—')}</div><div class="sub">{scope_note}</div></div>
<div class="card"><div class="label">Weather markets found</div><div class="value" style="color:{'var(--pos)' if st.get('weather_markets_discovered') else 'var(--neg)'}">{st.get('weather_markets_discovered','—')}</div><div class="sub">{'discovery complete' if st.get('weather_discovery_complete') else 'DISCOVERY INCOMPLETE'}</div></div>
<div class="card"><div class="label">Opportunities considered</div><div class="value">{st.get('opportunities_considered','—')}</div></div>
<div class="card"><div class="label">Traded this scan</div><div class="value">{st.get('traded_this_scan','—')}</div></div>
<div class="card"><div class="label">Audit chain</div><div class="value" style="font-size:15px" class="{'pos' if chain.get('ok') else 'neg'}">{'intact' if chain.get('ok') else 'BROKEN'}</div><div class="sub">{chain.get('records',0)} records</div></div>
<div class="card"><div class="label">Mode</div><div class="value" style="font-size:15px">{html.escape(str(st.get('mode','PAPER')))}</div><div class="sub">live disabled</div></div>
</div>

<h2>Open positions ({len(open_pos)})</h2>
<div class="scroll"><table><tr><th>Market</th><th>Contracts</th><th>Avg price</th><th>Stake</th><th>Mark (exit value)</th><th>Predicted</th><th>Edge at entry</th><th>Opened</th></tr>
{pos_rows}</table></div>

<h2>Completed trades ({len(closed)})</h2>
<div class="scroll"><table><tr><th>Market</th><th>Contracts</th><th>Entry</th><th>Exit</th><th>Predicted</th><th>Outcome</th><th>Realized P&amp;L</th></tr>
{closed_rows}</table></div>

<h2>Calibration by predicted probability</h2>
<table><tr><th>Bin</th><th>N</th><th>Mean predicted</th><th>Actual frequency</th></tr>
{calib_rows}</table>
<div class="sub" style="margin-top:6px">Calibration is meaningless below roughly 30 resolved trades per bin. Treat early rows as noise.</div>

<div class="sub" style="margin-top:14px;padding:10px;border:1px solid var(--line);border-radius:6px">
<strong>How open positions are marked.</strong> Each is valued at its
<em>executable, side-aware, fee-inclusive exit</em> — what it could actually be
closed into right now, walking the real book. A NO position exits against the
offers, not the bids. A fresh position therefore marks slightly <em>below</em>
its entry price: you buy at the ask and can only sell at the bid, less fees.
A <span style="color:var(--warn)">&#9888;</span> marks a position with no executable
exit, valued at the touch instead. The high-water mark used for drawdown only
moves after a scan in which every open position was re-marked successfully.
</div>

<h2>Profit by category</h2>
<table><tr><th>Category</th><th>Realized P&amp;L</th></tr>{cat_rows}</table>

<h2>Every opportunity considered this scan ({len(opps)})</h2>
<div class="sub" style="margin-bottom:8px">{len(traded)} traded · {len(skipped)} skipped, each with its reason.</div>
<div class="scroll"><table><tr><th>Market</th><th>Bid / Ask</th><th>Spread</th><th>Fair prob</th><th>Net edge</th><th>Decision &amp; reason</th><th>Evidence</th></tr>
{opp_rows(traded + skipped)}</table></div>

<h2>Market discovery</h2>
<div class="scroll"><table><tr><th>Source</th><th>Markets</th><th>Pages</th><th>Ran to exhaustion</th><th>Note</th></tr>
{disc_rows}</table></div>
<div class="sub" style="margin-top:6px">Weather markets are queried explicitly by category, tag and search, and paginated to completion. {broad_state} A truncated broad listing can therefore never hide a weather market.</div>

<h2>Risk configuration</h2>
<div class="scroll"><table>
<tr><th>Control</th><th>Value</th></tr>
<tr><td>Kelly fraction</td><td class="num">{cfg.get('kelly_fraction')}</td></tr>
<tr><td>Max per position</td><td class="num">{_pct(cfg.get('max_position_pct'))}</td></tr>
<tr><td>Max total exposure</td><td class="num">{_pct(cfg.get('max_total_exposure_pct'))}</td></tr>
<tr><td>Max correlated exposure</td><td class="num">{_pct(cfg.get('max_correlated_pct'))}</td></tr>
<tr><td>Minimum net edge</td><td class="num">{cfg.get('min_net_edge_pp')}pp</td></tr>
<tr><td>Max spread</td><td class="num">{cfg.get('max_spread')}</td></tr>
<tr><td>Daily loss halt</td><td class="num">{_pct(cfg.get('daily_loss_halt_pct'))}</td></tr>
<tr><td>Drawdown halt</td><td class="num">{_pct(cfg.get('max_drawdown_halt_pct'))}</td></tr>
<tr><td>Live trading</td><td class="num">DISABLED</td></tr>
</table></div>

<p class="sub" style="margin-top:28px">Paper trading only. Simulated fills walk real resting
order-book depth at the recorded timestamp and never use midpoint prices. Past simulated
results do not establish an edge. Nothing here is financial advice.</p>
</div></body></html>"""

    out = os.path.join(root, "docs", "index.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return out


if __name__ == "__main__":
    print(build(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
