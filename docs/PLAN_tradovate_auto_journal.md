# Plan — Tradovate → Discord auto trade logging

**Goal:** Kill the "I never record my trades" problem by making the *factual*
trade log automatic. Tradovate is the source of truth for fills and P&L, so the
bot pulls closed trades from the Tradovate API and posts them to Discord with
win/loss and realized P&L already filled in — no typing, works from any machine,
independent of discipline.

This complements the existing flow:
- **`model_m_smt.pine`** decides (the logical engine).
- **`cogs/alerts.py`** posts TradingView signals with bias/narrative context.
- **This plan** records what was *actually executed* and links it to the trade
  channels + reflection/analysis pipeline already in `cogs/trades.py`.

> ⚠️ Verify every endpoint/field against the current docs before coding:
> https://api.tradovate.com/  (REST) and the WebSocket/market-data guides.
> The names below are from the documented v1 API but must be confirmed.

---

## Phase 0 — Prerequisites (manual, do first)

1. In Tradovate, create an **API Key** (Application settings) → gives `cid` +
   `sec`. Note your `appId` / `appVersion` strings.
2. Decide environment: **demo** (`demo.tradovateapi.com`) first, then **live**
   (`live.tradovateapi.com`). Build and test against demo.
3. Add secrets to `.env` (never commit):
   ```
   TRADOVATE_ENV=demo            # demo | live
   TRADOVATE_USERNAME=...
   TRADOVATE_PASSWORD=...
   TRADOVATE_APP_ID=FractalJournal
   TRADOVATE_APP_VERSION=1.0
   TRADOVATE_CID=...
   TRADOVATE_SEC=...
   ```

---

## Phase 1 — Tradovate service (`services/tradovate_service.py`)

Mirror the shape of `services/couchdb_service.py` (async, singleton, shared
`aiohttp` session).

- **Auth:** `POST /auth/accessTokenRequest` with
  `{name, password, appId, appVersion, cid, sec}` → returns `accessToken` +
  `expirationTime` (~80 min). Cache token + expiry; auto-renew via
  `POST /auth/renewAccessToken` before expiry. All calls send
  `Authorization: Bearer <accessToken>`.
- **Base URL** derived from `TRADOVATE_ENV`.
- **Methods (confirm entity names in docs):**
  - `get_fill_pairs(since_id)` — `fillPair` entities pair an entry+exit with
    realized P&L. This is the cleanest "one closed trade = one row with W/L"
    source. Prefer this.
  - Fallbacks if `fillPair` is insufficient: `get_fills()` (`/fill/list`) +
    `get_orders()` (`/order/list`) and pair them manually; `get_positions()`
    (`/position/list`) for open exposure.
  - `get_contract(id)` / cache — map contract IDs → readable symbols (ES, NQ…).

**Deliverable:** unit tests with mocked `aiohttp` (follow
`tests/test_couchdb_service.py`). Cover: token fetch, renewal-before-expiry,
401 → re-auth retry, fill-pair parsing.

---

## Phase 2 — Poller cog (`cogs/tradovate.py`)

A scheduled task that finds new closed trades and posts them.

- **Schedule:** `@tasks.loop(minutes=5)` (or a WebSocket subscription later —
  see Phase 5). **Gate every run with `is_trading_day()`** from
  `services/schedule_utils.py` so it stays quiet on weekends, consistent with
  the bias/reminders fix.
- **Dedupe:** track the last processed fill/fillPair id, same pattern as
  `last_analyzed.txt` / `TRACKER_PATH`. Store in CouchDB (new db
  `trading_fills`) or a tracker file. Persist each closed trade to
  `trading_fills` so history survives restarts.
- **On each new closed trade:** build an embed →
  `🟢 WIN` / `🔴 LOSS`, symbol, side, qty, entry/exit, realized P&L, R multiple
  if stop is known. Post to a `#executed_trades` channel (add
  `EXECUTED_CHANNEL_NAME` to `config.py`).

**Deliverable:** `cogs/tradovate.py` + `tests/test_tradovate.py`
(weekday gating, dedupe advances only on success, embed win/loss coloring).

---

## Phase 3 — Link execution → journal (the payoff)

Tie auto-logged fills into the existing trade-channel + reflection + AI-analysis
pipeline so a closed trade becomes a review with almost zero effort.

- When a new closed trade arrives, reuse `Trades._do_new_trade` logic to
  auto-create / reuse a `trade_N_<asset>` channel under `Fractal_Trades`,
  pre-posting the execution facts (entry/exit/P&L/W-L) as the first message.
- The channel already auto-posts the **checklist** + **reflection** button
  (`on_guild_channel_create` in `cogs/trades.py`) — so all that's left for the
  trader is one tap to add *why/how-felt/what-learned*.
- The existing 2-min-silence **auto-sync + AI analysis** then folds the trade
  into `master_insights.json`, which feeds the morning prep.

This closes the loop: **engine signals → you execute on Tradovate → bot logs the
result → you add a one-tap reflection → AI extracts habits/mistakes → morning
prep shows them back to you.**

---

## Phase 4 — Weekly review digest (become an effective reviewer)

- New command `/weekly_review` (+ optional Friday-evening scheduled post,
  weekday-gated) that compiles the week from `trading_fills`:
  win rate, net R, best/worst trade, and a per-asset breakdown of
  **model bias vs. actual result** (join `trading_fills` with `trading_bias`).
- Surfaces where you overrode the engine and lost — the core behavior the whole
  system exists to fix.

---

## Phase 5 — (Optional) Real-time via WebSocket

Replace/augment polling with the Tradovate WebSocket
(`wss://<env>.tradovateapi.com/v1/websocket`): authorize, subscribe to user
data, receive fills in real time and post instantly. Only do this once the
REST polling version is proven — polling is simpler and good enough to start.

---

## Related follow-up (not Tradovate) — low-friction *idea* capture

Separate from execution logging: capturing pre-trade *ideas* per asset (the
"I forget my ideas when switching assets" problem). Planned approach:
voice message from phone → Discord → Whisper transcription → tag asset. Tracked
separately; do not block the Tradovate work on it.

---

## Suggested build order

1. Phase 0 secrets + demo account.
2. Phase 1 service + tests (against demo).
3. Phase 2 poller posting to `#executed_trades` (verify real fills show up).
4. Phase 3 link into trade channels.
5. Phase 4 weekly digest.
6. Phase 5 WebSocket only if polling proves too laggy.
