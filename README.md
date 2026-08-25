# Polymarket Cowork Agent

Automated **paper-trading** research system for **Polymarket US** (the
CFTC-regulated exchange), specialising in temperature contracts settled
against National Weather Service data.

> ### This is a weather-only run
> The 48-hour evaluation scans **weather markets only**. They are discovered
> explicitly by category, tag and search, and paginated to exhaustion. The
> broad all-markets scan ships **disabled** (`"broad_scan_enabled": false`),
> because nothing outside weather is auto-traded and paging tens of thousands
> of unrelated markets every 10 minutes would buy nothing at the cost of
> thousands of API requests a day. See *Market discovery* for the switch.

> **Live trading is disabled and no live-order code exists in this repository.**
> `src/pmus_client.py` talks only to the public, unauthenticated market-data
> gateway. There is no authenticated client, no signing key handling, and no
> order-entry function. A test (`test_no_order_placement_code_anywhere`) fails
> the build if one is ever added.

---

## What it does

Every ~10 minutes, a GitHub Actions job:

1. Marks open paper positions to market and settles anything the venue resolved.
2. Evaluates risk gates. **Any condition it cannot positively verify halts trading.**
3. Pulls every active market from `gateway.polymarket.us`.
4. Applies cheap quantitative filters (volume, expiry, spread, depth).
5. For weather markets, pins the **exact settlement station** and reads the
   **target weather date from the contract text**, then pulls NWS observations
   and the gridpoint forecast **at that station's own coordinates** and
   estimates a fair probability with a confidence band.
6. Walks the **real order book on both sides** to compute all-in cost including
   fees and slippage, and picks whichever of YES/NO carries a qualifying edge.
7. Sizes with quarter-Kelly under hard caps, or refuses with a stated reason.
8. Records **every market considered** and why it was traded or skipped.
9. Rebuilds the dashboard and commits state.

All of step 3–8 is plain arithmetic. **No language model runs in this loop**, so
a scan costs a few seconds of CPU, not a Claude session.

## What it deliberately does *not* do

- **It does not auto-trade non-weather markets.** It has no defensible
  automated fair-value model for them, so they are written to
  `state/shortlist.json` for separate, much less frequent review. Pretending
  otherwise would manufacture false confidence.
- **It never uses midpoint prices.** Simulated fills walk actual resting depth.
  Insufficient depth produces a partial fill or a refusal, never invented liquidity.
- **It never claims a source it has not read.** The Daily Climate Report is cited
  only when the exact product for that station and date has been retrieved and
  parsed (see below).
- **It never averages down**, never uses leverage, martingale, or borrowed money.
- **It never rewrites a past prediction.** See the audit log below.

---

## Market discovery

**Weather markets are discovered explicitly and exhaustively, before any broad
scan.** The first live scan reported exactly 4,000 markets, every one of them
sports, and zero weather. 4,000 is 40 pages x 100 — precisely the old
`max_pages` cap. Nothing errored; the dashboard showed a healthy scan; the
weather strategy simply never saw its universe, because an unfiltered listing
dominated by sports hit its ceiling first.

`src/discovery.py` runs several overlapping targeted strategies and unions the
results, each paginated until the API stops returning rows:

| Strategy | Endpoint |
|---|---|
| Category filter | `GET /v1/markets?categories=weather` (case variants tried) |
| Tag filter | `GET /v2/tags?query=weather` → `GET /v1/markets?tagIds=<id>` |
| Free-text search | `GET /v1/search?query=temperature` |

They overlap deliberately: if the category label is not what we guessed, or the
tag is missing, another route still finds the markets. `test_weather_survives_a_broken_category_filter`
and `test_weather_survives_a_broken_tags_endpoint` hold that redundancy in place.

Two guarantees follow:

- **Weather is first in the evaluation order**, so a truncated broad listing can
  never crowd it out.
- **The broad scan is honest about its limits.** `PagedResult.exhausted` is True
  only when the API itself ran out of rows. Hitting the safety ceiling — or a
  server that ignores `offset` and returns the same page forever — sets it False,
  and the dashboard says **BROAD SCAN TRUNCATED** rather than presenting a slice
  as the whole market. If weather discovery returns nothing at all, that is
  raised as a scan error and a red banner, because "no weather markets exist"
  and "our filters are wrong" look identical from inside the process and both
  mean the strategy has nothing to trade.

`get_all_markets()` remains only for compatibility and is marked deprecated: it
returns a bare list and so cannot distinguish a complete listing from a
truncated one — the exact ambiguity that let a cap silently define the universe.

### The broad scan switch

| Setting | Default | Effect |
|---|---|---|
| `broad_scan_enabled` | **`false`** | Weather-only. The unfiltered `/v1/markets` listing is never requested. |
| `broad_scan_max_pages` | `200` | Safety ceiling, used only when the above is `true`. |
| `weather_discovery_max_pages` | `50` | Per-strategy ceiling for weather. Always active. |
| `weather_search_enabled` | `true` | Free-text backstop if category and tag both miss. |

Turning `broad_scan_enabled` on **changes no trading behaviour by itself**. Even
with the whole listing in hand, non-weather markets are never auto-traded — they
are written to `state/shortlist.json` for manual review, because this system has
no defensible automated fair-value model for them. Enable it only alongside a
deliberately designed non-weather strategy; on its own it costs API requests and
buys nothing.

Weather discovery is **unaffected** by that switch and always runs to
exhaustion.

## Both sides are always evaluated

A market can be mispriced in either direction. Buying only YES discards half the
opportunities and biases the system toward whatever the crowd already likes.

Both sides are priced off executable liquidity: **YES** is bought by lifting the
offer stack; **NO** is bought by hitting the bid stack, since buying NO at *q*
is selling YES at *1 − q*. Exits mirror this — a NO position is closed against
the **offers**, not the bids. Marking a NO position off the YES bid values it at
roughly *(1 − its true worth)* and inflates equity badly enough to make paper
results meaningless; `test_side_agnostic_marking_would_have_inflated_equity`
guards against a regression.

For NO, the conservative probability is the *pessimistic* end for that side —
`1 − prob_high`, not `prob_low`.

### Marking open positions

Entry and every later refresh use the **same** executable exit path
(`exit_fill(book, side, contracts, fee_coefficient=...)`), so a position's mark
is always what it could genuinely be closed into, net of fees. Three
consequences worth knowing:

- A freshly opened position marks slightly **below** its entry price. That is
  correct — you buy at the ask and can only exit at the bid, less fees.
- Each market's own `feeCoefficient` is stored on the position and reused for
  every subsequent mark, rather than silently falling back to the default theta.
- The **high-water mark only moves after a scan in which every open position
  was re-marked successfully**, and opening a position never moves it. An
  earlier version marked new positions at the raw bid, which valued a fresh NO
  position at roughly *(1 − its true worth)*: first-scan equity read $60.54
  against a $50 bankroll, and that fiction was immediately persisted as
  `peak_bankroll`, corrupting drawdown and every risk halt derived from it.
  A peak written from bad marks understates all later drawdowns, which is the
  direction that makes a halt *less* likely to fire.

`test_first_scan_no_entry_does_not_inflate_equity` exercises a real scan rather
than `exit_fill()` in isolation, because the defect lived in the entry path
while `exit_fill()` was already correct.

## Which sources are actually used

| Situation | What the system does |
|---|---|
| Target day is **still ahead or in progress** | The CLI does not exist yet. Values from NWS METAR observations at the station plus the gridpoint forecast, and says plainly that the CLI is *not yet published*. It does not cite it. |
| Target day has **passed** | Retrieves the CLI product, matching **both** the station's AWIPS id (e.g. `CLINYC`) and the `CLIMATE SUMMARY FOR <date>` line. On an exact match the CLI decides the outcome. |
| CLI **cannot be pinned** | The market is **skipped**, not estimated. A near-miss is not a match. |

## The target date comes from the contract, not `endDate`

`endDate` is when a contract settles — for a temperature contract, typically
**8:00 AM ET the morning after** the day being measured. Deriving the weather
date from it lands a day late and values the wrong day entirely. The date is
parsed from the question and resolution rules; if it is missing or two different
dates appear, the market is skipped.

## The weather thesis, honestly stated

There is no clever meteorology here. The only plausible edge is *bookkeeping*:
as a day progresses, part of the outcome becomes a matter of record rather than
forecast. If Central Park has already observed 92°F, then "high above 88°F" is
settled in all but name, and a price of 0.62 is simply wrong.

The corresponding trap — which an early version of this code fell into and
which `test_afternoon_observation_overrides_stale_forecast` now guards — is
trusting a *morning forecast* with a tight error band while the station itself
has failed to reach the threshold. At 6pm with the station stuck at 83°F, an
84°F threshold is unlikely, not 89% likely. Observations dominate forecasts
after peak heating, in both directions.

Early-in-the-day estimates are mostly just the NWS forecast with wide error
bars. That is exactly where the system should *not* be trading, and the
confidence-band gate is what keeps it out.

The model also never asserts certainty: forecast-derived probabilities are
clamped to ±99%. Beyond the meteorology there is always residual risk it cannot
see — a station outage, a revised observation, a CLI that disagrees with METAR,
an unforeseen rule clarification.

---

## Risk controls

| Control | Value |
|---|---|
| Starting paper bankroll | $50.00 |
| Kelly fraction | ¼ Kelly, sized off the **conservative** end of the confidence band |
| Max per position | 6% of bankroll |
| Max total exposure | 20% of bankroll |
| Max correlated exposure | 8% — all contracts on one station+date share a cluster |
| Minimum net edge | 8pp on **all-in cost** (fees + slippage included), **and** at the conservative probability, on whichever side is better |
| Max spread | 0.06 |
| Daily loss halt | 10% |
| Drawdown halt | 20% from peak |
| Stale data halt | 900s, and unknown data age also halts |

The correlated-exposure cluster key is `wx:<station>:<date>`. Five thresholds
on one city on one day are **one bet**, not five, and are capped together.

Every cap is enforced against **all-in cost including the fee**, and against the
*realised* fill rather than a pre-trade estimate. Sizing works from a probe
fill, but the actual fill can differ, so `largest_fill_within_caps()` walks the
size down until the cash actually committed fits every cap. A fee must never be
what pushes real exposure past a limit the sizer believed it had respected.

### Emergency stop

Set `"emergency_stop": true` in `config/risk_config.json` and commit. The next
scan halts and opens nothing. To stop the schedule entirely, disable the
workflow in the repository's **Actions** tab.

The dashboard is a static file and deliberately cannot trigger anything.

---

## The 48-hour window, and why its state lives in `state/`

Every GitHub Actions run is a **clean checkout**. Anything a run writes is
discarded unless it is committed. The workflow commits `state/` and `docs/`, so
the window's start timestamp lives in **`state/evaluation.json`**.

An earlier version stamped it into `config/risk_config.json`, which the workflow
does not commit. The failure was silent and total: every scheduled run started
with an empty timestamp, re-stamped "now", and the window could never expire —
the scanner would have run forever. The split is now structural:

| File | Role |
|---|---|
| `config/risk_config.json` | **Immutable** deployment configuration. Running code never writes it. |
| `state/evaluation.json` | **Mutable** run state. Written by the scanner, committed by the workflow, survives checkouts. |

`assert_config_immutable()` raises if mutable state reappears in config, and
`test_start_survives_a_clean_checkout` simulates separate checkouts to prove the
timestamp holds.

The workflow disables its own schedule **only after** the completion state is
confirmed present on the remote — disabling on the basis of state that was
thrown away would be the same bug wearing a different hat. If the disable call
ever fails, the committed `complete: true` flag still blocks all trading.

## Settlement

Settlement detection is **completely independent of the order book**. A resolved
market may stop serving a book, may report a terminal state this code never
enumerated, or may return a payload whose resolution field is not the assumed
name or type. An earlier version looked up resolution only *after* a successful
book fetch and only for two exact book states, parsed it inline as
`float(settle.get("settlementValue"))`, and swallowed every failure — so a
resolved position silently stayed open forever, holding a stale mark the final
report then counted as a live result.

Every open position now gets an authoritative resolution lookup on every scan,
whatever the book did. One documented adapter (`src/settlement.py`) turns the
payload into exactly one canonical verdict:

| Verdict | Meaning | Action |
|---|---|---|
| `RESOLVED` | Settled at $1.00 or $0.00 | Close and pay out |
| `UNRESOLVED` | Not settled yet (incl. HTTP 404) | Keep holding, keep marking |
| `UNSUPPORTED` | Settled, but not at $1/$0 | **Preserve**, flag, alert a human |
| `MALFORMED` | Terminal-looking, outcome unparseable | **Preserve**, flag, alert a human |

Two rules follow, and both matter:

- **A position is closed only on an explicit authoritative resolved outcome.**
  A book disappearing is never sufficient — that is an absence of evidence,
  not evidence of an outcome.
- **An unparseable outcome preserves the position**, sets `settlement_pending`,
  writes a `settlement_error` audit record, raises a dashboard banner, and is
  **excluded from every figure in the final report**. Its mark is stale by
  definition, so counting it would present a settlement failure as performance.
  The report states separately how much capital is stranded.

### Endpoints and schemas used

| Purpose | Endpoint | Schema / notes |
|---|---|---|
| Resolution (authoritative) | `GET /v1/markets/{slug}/settlement` | `gateway.market.v1.GetMarketSettlementResponse` → `{slug: string, settlement: decimal}`. **404 = "Market not found or unsettled"**, treated as unresolved, never as an error. [docs](https://docs.polymarket.us/api-reference/markets/get-market-settlement) |
| Market lifecycle | `GET /v1/market/slug/{slug}` | Returns an **envelope**: `{"market": {...}}`. Lifecycle fields `active`, `closed`, `archived`, `hidden`, `ep3Status` live on the **inner** object. Note the path uses singular `market`. [docs](https://docs.polymarket.us/api-reference/markets/get-market-by-slug) |
| Order book | `GET /v1/markets/{slug}/book` | `marketData.state` ∈ `MARKET_STATE_OPEN | PREOPEN | SUSPENDED | EXPIRED | TERMINATED | HALTED | MATCH_AND_CLOSE_AUCTION`. [docs](https://docs.polymarket.us/api-reference/markets/get-market-book) |
| Payout semantics | — | "every contract settles at either $1.00 (YES won) or $0.00 (NO won)" ([docs](https://docs.polymarket.us/concepts/market-data)); "some markets include predefined settlement terms that differ from the standard $1/$0 structure" ([docs](https://docs.polymarket.us/learn/markets/contract-settlement)) — hence the `UNSUPPORTED` verdict. |

### The market-payload contract

`gateway.market.v1.GetMarketBySlugResponse` has exactly one property, `market`,
referencing `gateway.market.v1.Market`. The wire format is therefore an
envelope:

```json
{"market": {"active": false, "closed": true, "archived": false, "ep3Status": "SETTLED"}}
```

**`pmus_client.get_market_by_slug()` unwraps this once and returns the inner
`Market` object.** Everything downstream — `settlement.classify()`,
`market_looks_terminal()` — receives the inner object and never the envelope.
That is the contract, enforced in one place.

Ambiguous shapes are **refused, not guessed**. A response with no `market` key,
or whose `market` is not an object, raises `SchemaError`. As a second line of
defence `looks_like_envelope()` detects an un-normalized envelope reaching the
adapter: `market_looks_terminal()` raises on it, and `classify()` — which must
never raise, being the safety net — returns `MALFORMED` so it surfaces as
needing attention.

This strictness exists because the failure mode is invisible. Reading `closed`,
`active`, and `ep3Status` off the envelope returns `None` for every one of
them, with no error anywhere. A settled market then classifies as merely
unresolved, and its position sits open forever with no alert. The danger is
sharpest when the order book is also gone — the book state is normally a second
terminality signal, and without it the market payload is the only one left.
`test_e2e_terminality_from_market_payload_alone_when_book_is_gone` pins exactly
that case.

### A caveat on the test fixtures

`tests/fixtures/polymarket_us_payloads.py` was **constructed from the published
schemas above, field by field — not captured from live traffic.** This
repository was authored in an environment with no network route to
`gateway.polymarket.us`, so no live response could be recorded, and presenting
an invented one as "captured" would be worse than useless.

These fixtures therefore prove the adapter handles the *documented* shape and a
range of malformed shapes. They cannot prove the live service matches its own
documentation. The first real settlement is still a genuine test — which is
precisely why an unparseable payload preserves the position and raises an alert
instead of guessing. If you capture a real settlement response, drop it into
that file (stripped of account identifiers) and the existing tests run against
it unchanged.

Fixtures are named by shape: `*_ENVELOPE` is the raw wire response exactly as
documented, and the plain names are the inner `Market` object that
`get_market_by_slug()` returns after normalization. Tests that exercise the
client feed the envelope through the **transport** (`_get`) so the real
unwrapping code runs; mocking `get_market_by_slug()` itself would bypass the
very code under test.

## Audit log

`state/audit.jsonl` is append-only and **hash-chained**: each record embeds the
SHA-256 of the previous one. Altering any earlier entry breaks the chain from
that point on, and the workflow fails the build when it does. Combined with git
history — every scan is a commit — a prediction cannot be quietly revised after
the outcome is known.

Each decision records the fair probability, the confidence band, the bid/ask and
spread at that instant, the book levels consumed, the fee, the slippage, the
evidence sources with timestamps, and the sizing rationale.

Verify at any time:

```bash
python3 -c "import sys;sys.path.insert(0,'.');from src.audit import AuditLog;print(AuditLog('state/audit.jsonl').verify())"
```

---

## Local use

```bash
python3 -m pytest tests/ -q          # 39 unit tests, no network
python3 tests/integration_dryrun.py  # full scan cycle, network mocked
python3 -m src.run_scan              # live data (needs internet)
python3 -m src.dashboard             # rebuild docs/index.html
```

## Notifications

Telegram is **send-only**. `src/notify.py` has no `getUpdates`, no webhook, and
no command handler, so there is no path by which a Telegram message can
instruct this system to do anything. Set `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` as repository secrets.

## Data sources

| Purpose | Source |
|---|---|
| Markets, order books | `gateway.polymarket.us/v1` (public, no credentials) |
| Weather observations | `api.weather.gov/stations/{ID}/observations` |
| Weather forecast | `api.weather.gov` gridpoint at the station's coordinates |
| Settlement of record | NWS Daily Climate Report (CLI) from the listed WFO |

Stations are pinned in `config/stations.json`: KNYC, KSFO, KMIA, KMDW, KLAX.
A citywide forecast is never substituted for the market's named station.

---

*Paper trading only. Simulated results do not establish an edge. Nothing here
is financial advice.*
