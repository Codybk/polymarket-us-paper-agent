"""
Sanitized sample payloads for Polymarket US settlement tests.

PROVENANCE -- read this before trusting these fixtures
------------------------------------------------------
These payloads were CONSTRUCTED FROM THE PUBLISHED SCHEMAS at
docs.polymarket.us (verified 2026-08-25), field name by field name. They were
NOT captured from live traffic: the environment this repository was authored in
has no network route to gateway.polymarket.us, so no live response could be
recorded, and inventing one while calling it "captured" would be worse than
useless.

What that means practically: these fixtures prove the adapter handles the
DOCUMENTED shape and a range of malformed shapes. They cannot prove the live
service matches its own documentation. The first live settlement is therefore
still a real test, which is exactly why an unparseable payload preserves the
position and raises an alert instead of guessing.

If you capture a real settlement response, drop it in here (strip any account
identifiers) and the same tests will run against it unchanged.

SCHEMAS
-------
GET /v1/markets/{slug}/settlement
    gateway.market.v1.GetMarketSettlementResponse
    { "slug": string, "settlement": decimal }
    200 settled | 404 "Market not found or unsettled" | 500 error
    https://docs.polymarket.us/api-reference/markets/get-market-settlement

GET /v1/market/slug/{slug}
    gateway.market.v1.GetMarketBySlugResponse has EXACTLY ONE property,
    `market`, referencing gateway.market.v1.Market. The wire format is an
    ENVELOPE:
        {"market": { ...lifecycle fields... }}
    Lifecycle fields used here: active, closed, archived, hidden, ep3Status.
    pmus_client.get_market_by_slug() unwraps this envelope, so everything
    downstream sees the inner Market object only.
    https://docs.polymarket.us/api-reference/markets/get-market-by-slug

GET /v1/markets/{slug}/book
    marketData.state enum: MARKET_STATE_OPEN | PREOPEN | SUSPENDED | EXPIRED
    | TERMINATED | HALTED | MATCH_AND_CLOSE_AUCTION
    https://docs.polymarket.us/api-reference/markets/get-market-book

PAYOUTS
-------
"every contract settles at either $1.00 (YES won) or $0.00 (NO won)"
https://docs.polymarket.us/concepts/market-data
"some markets include predefined settlement terms that differ from the
standard $1/$0 structure"
https://docs.polymarket.us/learn/markets/contract-settlement
"""

SLUG = "nyc-high-above-88"

# ---- settlement endpoint -------------------------------------------------
SETTLEMENT_YES_WON = {"slug": SLUG, "settlement": 1}
SETTLEMENT_NO_WON = {"slug": SLUG, "settlement": 0}
SETTLEMENT_YES_WON_DECIMAL = {"slug": SLUG, "settlement": 1.0}
SETTLEMENT_STRING_NUMERIC = {"slug": SLUG, "settlement": "1.0"}

# 404 -> the client returns None. Documented as "not found or unsettled".
SETTLEMENT_UNSETTLED_404 = None

# Legitimate but non-binary: allowed by the docs, not modelled by this system.
SETTLEMENT_NON_BINARY = {"slug": SLUG, "settlement": 0.42}

# Malformed shapes a defensive parser must classify rather than crash on.
SETTLEMENT_NULL_FIELD = {"slug": SLUG, "settlement": None}
SETTLEMENT_NON_NUMERIC = {"slug": SLUG, "settlement": "pending"}
SETTLEMENT_BOOLEAN = {"slug": SLUG, "settlement": True}
SETTLEMENT_OUT_OF_RANGE = {"slug": SLUG, "settlement": 7}
SETTLEMENT_MISSING_FIELD = {"slug": SLUG}
SETTLEMENT_WRONG_TYPE = ["not", "an", "object"]

# ---- market lookup -------------------------------------------------------
# NOTE ON NAMING
#   *_ENVELOPE  = the raw wire response, exactly as documented.
#   plain names = the INNER Market object, i.e. what get_market_by_slug()
#                 returns after normalization. Tests that exercise the client
#                 feed the ENVELOPE through the transport; tests that exercise
#                 the adapter pass the inner object.
MARKET_OPEN = {
    "slug": SLUG, "question": "Will the high temperature in NYC be above 88F?",
    "active": True, "closed": False, "archived": False, "hidden": False,
    "ep3Status": "OPEN", "category": "weather", "volume": 42000,
    "minimumTradeQty": 1, "feeCoefficient": 0.06,
}
MARKET_CLOSED_SETTLED = {**MARKET_OPEN, "active": False, "closed": True,
                         "ep3Status": "SETTLED"}
MARKET_CLOSED_NO_PRICE = {**MARKET_OPEN, "active": False, "closed": True,
                          "ep3Status": "SETTLED"}

# ---- market lookup: raw wire envelopes -----------------------------------
MARKET_OPEN_ENVELOPE = {"market": MARKET_OPEN}
MARKET_CLOSED_SETTLED_ENVELOPE = {"market": MARKET_CLOSED_SETTLED}
MARKET_CLOSED_NO_PRICE_ENVELOPE = {"market": MARKET_CLOSED_NO_PRICE}

# Exactly the payload shape the reviewer cited:
MARKET_TERMINAL_MINIMAL_ENVELOPE = {
    "market": {"active": False, "closed": True, "archived": False,
               "ep3Status": "SETTLED"}}

# Envelopes that violate the documented schema -- must be refused, not guessed.
MARKET_ENVELOPE_MISSING_KEY = {"active": False, "closed": True}   # no "market"
MARKET_ENVELOPE_WRONG_TYPE = {"market": "settled"}                # not an object
MARKET_ENVELOPE_NULL = {"market": None}


# ---- order books ---------------------------------------------------------
BOOK_OPEN = {"marketData": {
    "marketSlug": SLUG, "state": "MARKET_STATE_OPEN",
    "transactTime": "2026-08-25T00:00:00Z",
    "bids": [{"px": 0.60, "qty": 500}],
    "offers": [{"px": 0.62, "qty": 500}]}}

BOOK_OPEN_HIGH = {"marketData": {
    "marketSlug": SLUG, "state": "MARKET_STATE_OPEN",
    "transactTime": "2026-08-25T00:00:00Z",
    "bids": [{"px": 0.78, "qty": 900}],
    "offers": [{"px": 0.80, "qty": 900}]}}

# A resolved market may report a terminal state with an EMPTY book...
BOOK_TERMINAL_EMPTY = {"marketData": {
    "marketSlug": SLUG, "state": "MARKET_STATE_TERMINATED",
    "transactTime": "2026-08-25T00:00:00Z", "bids": [], "offers": []}}

# ...or a state this code never enumerated...
BOOK_UNKNOWN_STATE = {"marketData": {
    "marketSlug": SLUG, "state": "MARKET_STATE_MATCH_AND_CLOSE_AUCTION",
    "transactTime": "2026-08-25T00:00:00Z", "bids": [], "offers": []}}

# ...or refuse to serve a book at all (the client raises).
BOOK_FETCH_FAILS = "raise"
