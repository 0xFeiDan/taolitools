# entropy-arb

**[中文文档 / Chinese documentation → README.zh-CN.md](README.zh-CN.md)**

Open-source two-venue perp arbitrage bot. One leg is always **Entropy**
(the `io` builder dex on Hyperliquid); the other leg — the hedge — is one of:

| `--hedge` | venue | quote | taker fee | protocol |
|---|---|---|---|---|
| `lighter` | Lighter mainnet | USDC | 0 bps | zkLighter ws (diff books, async settle) |
| `lighter-rh` | Lighter Robinhood chain | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book, sync IOC settle |

> **Referral links** — signing up through these supports this project:
> - Entropy — Tier 4 referral, 100% rebates: <https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood chain: <https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz (Hyperliquid): <https://app.hyperliquid.xyz/join/QUANTGUY>

When the same symbol trades rich on one venue and cheap on the other, the bot
simultaneously sells the rich book and buys the cheap book with taker orders,
carrying a delta-neutral position until the premium reverts and the opposite
crossing unwinds it. Every price it acts on is the **actual order book of the
exchange that will fill the order** — Hyperliquid books come from the official
websocket (`wss://api.hyperliquid.xyz/ws`), Lighter books from Lighter's
official websocket.

While it runs — even with no credentials and no strategy — it records both
books to **1-minute CSV bars**, and the bundled analyzer provides the spread
distribution used to configure either signal mode.

## The signal

There are two compatible signal modes. **Static** uses the configured bps band
shown below. **Dynamic** uses the rolling Slow Midline plus Z-score
OPEN/ADD/EXIT rules described later.

```
price_basis: usd (recommended)
premium_bps = (Entropy price × Entropy quote/USD
               / (hedge price × hedge quote/USD) − 1) × 10 000

price_basis: raw (legacy)
premium_bps = (Entropy price / hedge price − 1) × 10 000

                          ┌──────────────  SELL entropy + BUY hedge
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   the premium's usual level
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  BUY entropy + SELL hedge
```

- `price_basis` — `usd` keeps collection, Midline, Z-score, Regime, VWAP and
  thresholds in one normalized unit. Omitted legacy configs retain `raw`.
- `midline_bps` — the Static premium center and Dynamic warm-up display seed.
  Cross-venue premiums are
  rarely centered at zero (different oracles, different quote assets, listing
  premia), so a zero-centered band would fire one direction only, cap out and
  never unwind. Measure where the premium actually sits and type it in.
- `upper_bps` / `lower_bps` — Static-mode bands; Dynamic mode ignores them.

In Static mode, both hurdles are applied to **executable** prices (entropy bid vs hedge ask,
and vice versa) and are **net of both venues' taker fees** — the engine adds
fees on top before a slice qualifies. These bands are signal distances in
price-ratio space, **not an unconditional USD round-trip profit floor**. For
example, with midline 5, upper 4, and lower 3, selling one base at
Entropy/Hedge = 100.091/100 and later closing at 1000/999.801 satisfies both
directional hurdles but loses about $0.108 before fees: the common price level
rose tenfold. Slippage, funding, and quote/USD changes can reduce it further.

One Static-mode consequence worth understanding: with `midline_bps: 5`, the
lower boundary is `midline − lower` in the configured basis. The executable BUY-entropy direction uses
the exact reciprocal hedge/Entropy ratio (not merely `lower − midline`), which
can still be **negative**. That is intentional —
if entropy is persistently 5 bps rich, buying it at a 0 bps premium is 5 bps
cheap versus its own equilibrium, and that trade is the profitable unwind of
an earlier sell at `midline + upper`. It also means a **wrong midline loses
money**: if you type `midline_bps: 5` while the true premium sits at 0, the
bot happily buys entropy at fair value all day. Measure first, then trade —
that is what the recorder and analyzer are for.

## Quick start

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # data collection needs only this

cp config.example.yaml config.yaml       # the strategy (thresholds, sizing, risk)
cp .env.example .env                     # credentials — required to trade
```

For local development and unit tests, install the development requirements;
these do not include any live-trading signing SDK:

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/
```

The markets are **not** in the config file — you state them explicitly on
every start: `--symbol` (traded on both venues) and `--hedge` (one of
`lighter`, `lighter-rh`, `tradexyz`; Entropy is always the
other leg).

There is **no paper mode** — the bot either collects data (`--record-only`)
or trades live. Validate with recorded data and tiny position caps, not with
simulated fills.

**1. Collect data first** (no credentials needed):

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh
```

Let it run for at least a few hours (a day is better — premiums have
intraday regimes). It writes `logs/minutes.csv`.

**2. Analyze and set your thresholds:**

```bash
python3 tools/analyze.py
```

It prints the premium distribution, how often each candidate band would have
fired, and a ready-to-paste `thresholds:` block for `config.yaml`. It defaults
to USD-normalized samples and emits `price_basis: usd`; use `--basis raw` only
for legacy configurations.

**3. Go live** — fill in `.env`, install the signing SDKs, and start with
the smallest position caps that clear the venue minimums:

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --hedge lighter-rh
```

Running without `--record-only` sends real orders immediately once both
feeds are fresh and the band is crossed.

`requirements-live.txt` is a development convenience, not a reproducible
real-money lockfile: it currently contains lower-bounded packages and a Lighter
SDK reference to the moving GitHub `main` branch. A real-money deployment must
first pin locally audited package versions/commit SHAs and artifact hashes.

**Dashboard.** On a terminal the bot shows a live Rich dashboard: both
books with age/spread, positions and caps, equity and session PnL, the
executable premium of each direction against its full hurdle (fees and
inventory surcharge included, ● = armed), recorder progress, the last
executions, rolling latency P50/P95/P99, and a tail of the log (the full log goes to `logging.file`,
default `logs/engine.log`). It works in `--record-only` too. Add `--cn` to
display the dashboard in Chinese. Use `--no-dashboard` for plain console
logs (nohup/systemd — off-terminal runs fall back automatically), or set
`logging.dashboard: false`.

## Data collection & analysis

The recorder runs automatically in every mode (`recorder.enabled: true`).
Once per second it samples both live books; once per minute it writes a row:

| column | meaning |
|---|---|
| `minute_ts`, `time_utc` | minute start (epoch seconds, ISO UTC) |
| `entropy_bid/ask`, `hedge_bid/ask` | last fresh top-of-book of the minute |
| `premium_open/high/low/close/mean/std_bps` | mid-to-mid premium of Entropy over the hedge |
| `sell_edge_mean/max_bps` | executable premium for SELL entropy (entropy bid / hedge ask − 1) |
| `buy_edge_mean/max_bps` | executable premium for BUY entropy (hedge bid / entropy ask − 1) |
| `samples` | how many of the ~60 seconds both books were fresh |
| `entropy_quote_asset`, `hedge_quote_asset` | quote-asset identities used for the row |
| `entropy_quote_usd_close`, `hedge_quote_usd_close` | last fresh Kraken quote/USD midpoint |
| `hedge_entropy_quote_basis_close_bps` | hedge quote / Entropy quote basis; USDG/USDC for Lighter RH |
| `premium_usd_*_bps` | mid premium after both legs are converted to USD |
| `sell_edge_usd_*_bps`, `buy_edge_usd_*_bps` | executable directional edges in USD |
| `fx_samples` | subset of book samples with fresh quote/USD rates |

Raw fields are retained even when FX is missing, but USD fields remain blank
and `fx_samples` is zero; parity is never fabricated. A pre-existing CSV with
the old header is rotated to `.old`, because its missing historical FX cannot
be reconstructed safely. Recorded edges are pre-fee; the analyzer defaults to
the USD fields and subtracts `--fees-bps` (pass the
**sum** of both venues' taker fees — default 0.0 for the zero-fee venues,
~1.0 with a `tradexyz` hedge) before counting firings, so its table and
suggestions translate directly into config values. `--hours 24` restricts to
recent data; premiums drift, so re-run it regularly and update
`config.yaml`.

## Configuration

Strategy lives in `config.yaml` (validated — unknown keys are startup
errors), credentials in `.env`, and the markets on the command line
(`--symbol`, `--hedge`). Full commented reference:
[config.example.yaml](config.example.yaml). The essentials:

| key | meaning | default |
|---|---|---|
| `thresholds.price_basis` | `usd` for normalized signals; `raw` preserves legacy semantics | `raw` (example: `usd`) |
| `thresholds.midline_bps` | premium center (measure it!) | — |
| `thresholds.upper_bps` / `lower_bps` | entry bands (> 0) | — |
| `midline.mode` | `static` or live `dynamic` slow-median baseline | `dynamic` in the example |
| `midline.fast_window_seconds` / `slow_window_seconds` | Fast EMA / Slow rolling-median windows | 300 / 1800 |
| `midline.min_samples` | fresh 1Hz samples required before dynamic trading | 300 |
| `midline.volatility_method` | rolling `std` or robust scaled `mad` | `std` |
| `midline.entry_z_score` / `exit_z_score` | Dynamic OPEN/ADD and EXIT thresholds | 2.5 / 0.5 |
| `regime.enabled` | pause new entries on a confirmed regime break | `true` in the example |
| `entropy.dex` | Entropy's dex name on Hyperliquid | `io` |
| `*.taker_fee_bps` | per-venue taker fee | 0.0 (tradexyz hedge: 1.0) |
| `*.max_position_usd` | per-venue position cap | 500 in the example |
| `*.max_orders_per_min` | per-venue send budget (sliding 60 s) | 120; lighter hedges 30 |
| `sizing.take_fraction` | fraction of crossable depth taken | 0.5 |
| `sizing.max_order_notional_usd` | legacy per-slice cap | 100 in the example |
| `sizing.vwap_enabled` | current-orderbook VWAP + automatic sizing; `false` keeps legacy sizing | `true` in the example |
| `sizing.min_order_usd` / `max_order_usd` | search range for automatic sizing | 10 / 100 in the example |
| `sizing.minimum_net_edge_bps` | minimum directional deviation after modeled costs | 6 |
| `sizing.max_vwap_slippage_bps` / `max_book_impact_bps` | reject sizes that consume too much depth | 5 / 5 |
| `sizing.safety_buffer_bps` / `expected_latency_cost_bps` | explicit deductions beyond fees and visible depth | 2 / 0 |
| `inventory.scale_bps` / `floor_frac` | inventory ladder (extra bps past `floor_frac` of the cap) | 10 / 0.5 |
| `execution.premium_persist_sec` | edge must persist before firing | 0.3 |
| `execution.risk_recovery_enabled` / `hedge_timeout_ms` | one-leg timeout recovery | `true` / 250ms in the example |
| `execution.max_unhedged_delta_usd` | delta threshold for emergency hedging | 100 in the example |
| `kill_switch.enabled` | unified risk-event and persistent entry-pause handling | `true` in the example |
| `kill_switch.emergency_flatten_enabled` | allow reduce-only flatten after severe persistent risk | `false` |
| `accounting.enabled` | durable Pair ledger and restart snapshot | `true` in the example |
| `funding.enabled` / `expected_holding_hours` | live two-venue funding cost | `true` / 1h in the example |
| `stablecoin.enabled` | normalize each venue quote asset to USD and halt on depeg | `true` in the example |
| `stablecoin.provider` / `source_url` | public level-1 quote source | `kraken` / `https://api.kraken.com` |
| `stablecoin.max_spread_bps` | reject an illiquid/wide conversion book | `10` |
| `execution.*` | slippage bounds, timeouts, reconcile cadence… | see file |
| `market_data.enforce_book_age` | enable millisecond book-age rejection | `true` in the example |
| `market_data.max_book_age_ms` | reject new trades if either book is older | `300` |
| `session.enabled` | `false`: one crypto 24/7 statistics pool; `true`: session-isolated stock-perpetual statistics | `false` |
| `recorder.*` | minute-data recorder | on, `logs/minutes.csv` |
| `logging.dashboard` / `logging.file` | Rich dashboard on a tty; log file while it runs | on, `logs/engine.log` |

### Market-data quality and latency

Two freshness clocks are maintained. `execution.staleness_sec` checks whether
the websocket is still receiving messages; `market_data.max_book_age_ms` checks
when the order book itself last changed. With `enforce_book_age` enabled, a live
heartbeat cannot make an old book tradable, and stale data disarms any pending
signal.

Hyperliquid's documented millisecond `time` on `l2Book` is also checked against
`max_book_age_ms`; stale exchange snapshots and implausibly future timestamps
are rejected. The official Lighter order-book websocket currently
does not expose a server timestamp, so only local receive time is recorded and
exchange time remains unknown. Order ACK/fill timestamps are also local
observation times, not matching-engine timestamps.

Latency percentiles use the latest 2,000 in-memory observations and reset on
restart. The example 300ms threshold is deliberately conservative; observe the
actual update cadence and P95/P99 for the symbol and deployment before tuning it.

### Crypto versus stock sessions

Session awareness has one manual switch and never guesses from the symbol:

```yaml
session:
  enabled: false  # crypto 24/7
```

Keep it `false` for crypto. Set it to `true` for stock perpetuals. Enabled mode
uses US Eastern time and has exactly four statistics regimes: overnight
(20:00-04:00), pre-market, regular, and after-hours. Weekends and standard US
equity holidays are assigned to the overnight pool because a stock perpetual
can remain tradable while its cash reference market is closed. Recurring 13:00
ET early closes switch from regular to after-hours. **Every regime may
OPEN/ADD/EXIT** after its own Dynamic Midline has warmed up and the ordinary
edge, freshness, cost, regime, and risk checks pass.

Every session owns a separate Dynamic Midline, volatility, Z-score, and regime
detector, so overnight, pre-market, and after-hours observations cannot move
the regular-session baseline. If a Pair is already open while a new session
bank is warming up, EXIT may use the last ready baseline as a conservative
risk-reducing fallback. New OPEN/ADD waits for that session's own warmup; that
is estimator readiness, not a session trading ban. The 20:00 boundary is an
intentional stock-perpetual statistics boundary that removes the cash-market
20:00-21:00 gap; it is not a claim about cash-exchange hours. Calendar
conventions otherwise follow the
[NYSE trading-hours calendar](https://www.nyse.com/trade/hours-calendars).
Unscheduled national closures and venue-specific oracle rules cannot be
predicted by a static calendar; stale feeds and venue failures still fail
closed through the existing risk guards.

### Current-orderbook VWAP and automatic sizing

This is **not candle/TradingView VWAP**. For each candidate base quantity, the
engine walks the current buy asks and sell bids level by level, computes each
leg's volume-weighted fill price, and evaluates:

```
expected net profit
  = sell VWAP notional - buy VWAP notional
  - both taker fees
  - safety buffer - configured expected latency cost
```

Visible order-book slippage is already embedded in the two VWAP notionals and
is not deducted a second time. The engine then binary-searches the largest
shared base quantity that satisfies the minimum order, maximum order, net-edge,
VWAP-slippage, book-impact, venue minimum, and position-headroom constraints.
Insufficient depth or a failing constraint produces no order.

In Static mode, `minimum_net_edge_bps` is a minimum **directional deviation
from the configured midline after modeled costs** and is combined with the
legacy upper/lower band. In Dynamic mode the Z-score lifecycle determines
OPEN/ADD/EXIT and supplies the executable VWAP hurdle directly; the legacy
bands are ignored. When enabled, fresh two-venue funding rates and quote-asset
USD prices are included before the minimum net edge is checked. Missing or
stale enabled cost data fails closed for OPEN/ADD.

### Dynamic midline, Z-score, and regime detection

When `midline.mode: dynamic`, the engine samples the fresh mid-to-mid premium
at most once per second and maintains:

```
Fast Midline = time-based 5-minute EMA
Slow Midline = 30-minute rolling median (the trading baseline)
deviation    = current spread - Slow Midline
Z-score      = deviation / max(rolling volatility, volatility floor)
```

Volatility can use population standard deviation or normal-consistent MAD
(`1.4826 × median absolute deviation`). Until `min_samples` exists inside the
rolling windows, dynamic mode is `WARMUP` and **new entries are blocked**. It
does not silently trade against the configured static value. State is in
memory and warms again after restart.

The Fast EMA never becomes the sole trading baseline. With `regime.enabled`,
the guard watches Fast/Slow divergence, absolute spread, and absolute Z-score.
An abnormal condition must remain present for `break_persist_seconds` before
the engine enters `PAUSE_NEW_ENTRY`; recovery must remain continuously healthy
for `recovery_persist_seconds`. A pause disarms both strategy directions but
does not flatten positions or disable emergency delta hedging.

Dynamic mode uses Z-score as the primary lifecycle signal:

```
flat:                 Z >= +entry_z  -> OPEN sell-Entropy pair
flat:                 Z <= -entry_z  -> OPEN buy-Entropy pair
same-direction pair:  beyond entry_z -> ADD
sell-Entropy pair:    Z <= +exit_z   -> EXIT by buying Entropy
buy-Entropy pair:     Z >= -exit_z   -> EXIT by selling Entropy
```

An EXIT is hard-capped to the remaining matched Pair base quantity, so a
return-to-center signal cannot reverse into a new position. The executable
VWAP and modeled-cost check remains mandatory; therefore an exit may wait past
the exact Z boundary when the bid/ask spread or fees make that snapshot
untradeable. In Dynamic mode, `upper_bps` / `lower_bps` no longer drive the
signal; they remain solely for Static compatibility.

The runtime maintains a matched `PairPosition` (ID, direction, remaining base)
and conservatively infers an existing matched pair from venue positions on
startup. Each new execution attempt also has an evented state machine. With
`accounting.enabled`, the open Pair, the last 200 completed Pairs, execution
events, risk events, persistent pauses, and pending emergency flatten are
restored from an atomically replaced snapshot. Audit events are appended to
JSONL and flushed to disk. Live mode requires this accounting ledger; an
incomplete `.tmp` snapshot blocks restart for manual review.

### Pair PnL, funding, and quote-asset basis

One Pair record aggregates OPEN, ADD, EXIT, and emergency-flatten fills. It
stores entry/exit spread, Z-score and midline, per-leg entry/exit VWAP, fees,
expected and venue-reconciled funding, quote-basis adjustment, entry/exit
slippage, entry/exit market session, gross/net PnL, holding time, and maximum
adverse/favorable spread.

Funding uses each venue's current rate and each leg's own USD-normalized
notional; USDC and USDG amounts are never added as if they were identical.
Hyperliquid's asset context is already
hourly; Lighter's cross-exchange endpoint is documented as an 8-hour-equivalent
rate and is divided by eight. The opening cost model applies the configured
expected holding time. While a Pair is open, account funding history replaces
the estimate with venue-reported payments when available.

Quote prices are normalized before executable edge is calculated:

```text
adjusted gross edge
  = sell VWAP × sell quote/USD
  - buy VWAP  × buy quote/USD
```

The example reads level-1 `ASSET/USD` books from Kraken's public REST API,
including the actual Paxos USDG used by Lighter Robinhood. Bitget's similarly
named USDGO is a different Anchorage Digital/OSL asset and must not be used as
a proxy. Rates are committed only when every required book has a finite,
positive, non-crossed, current level with spread at or below
`max_spread_bps`; otherwise prior timestamps are retained and become stale.
A missing or stale source blocks OPEN/ADD. `warning_deviation_bps` is observable; crossing
`halt_deviation_bps` pauses new exposure. Set each venue's `quote_asset`
correctly; the defaults are USDC for Entropy/Lighter mainnet/trade.xyz and USDG
for Lighter Robinhood. The active midline hurdle is converted into the same
directional USD ratio (including the reciprocal direction), so a USDG/USDC
basis cannot move the executable edge without moving its hurdle. Live mode
refuses non-USD quote assets unless fresh stablecoin conversion is enabled.
Account/session PnL is reported as unavailable once that conversion is stale;
an old USDG/USDC rate is never presented as current USD value.

The reference USDG/USDC quote basis is derived from the two real USD rates
(the execution cost field applies a direction-dependent sign):

```text
USDG/USDC basis bps = (USDG_USD / USDC_USD - 1) * 10,000
```

## Credentials (`.env`, live only)

- **Entropy / tradexyz (Hyperliquid)** — create an API ("agent") wallet at
  <https://app.hyperliquid.xyz/API>. `HL_PRIVATE_KEY` is the **agent** key,
  `HL_ACCOUNT_ADDRESS` your main account address. With `--hedge tradexyz`
  both legs share this account by default (one nonce sequence is handled
  internally); set `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`
  to split them. Fund the dex-specific clearinghouses you trade.
- **Lighter** — `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`,
  `LIGHTER_API_PRIVATE_KEY`, registered on the **same deployment** as your
  `--hedge` flag (mainnet and the Robinhood chain are separate accounts and
  keys — see [lighter-python](https://github.com/elliottech/lighter-python)).

## How execution works

- Both legs are **taker** orders sent concurrently: Lighter market orders
  with average-price protection settling on the authenticated account
  websocket; Hyperliquid IOC limits settling synchronously (with
  orderStatus polling for unknown outcomes).
- **Fail-closed order outcomes**: an unresolved result persistently pauses
  OPEN/ADD before the venue locks are released. Only reconciliation, EXIT,
  and reduce-only recovery remain available until an operator verifies flat
  positions and explicitly clears the pause.
- **Reduce-only EXIT**: both EXIT legs are always sent with
  `reduce_only=true`; stale local Pair quantity can cause a safe rejection,
  but cannot reverse a venue position into a new exposure.
- A **persistence gate** (`premium_persist_sec`) arms each direction and only
  fires if the edge survives — one-tick phantoms are filtered.
- **Inventory ladder**: past `floor_frac` of a venue's cap, adding to the
  position requires linearly more edge, up to `scale_bps` extra at the cap.
- **Net-delta hedge**: if legs fill unevenly, the imbalance is immediately
  reduced (reduce-only, price-protected), and positions are reconciled
  against the chain every `reconcile_sec`.
- **Startup position guard**: the first strict position reconciliation runs
  before strategy tasks are created. Any unmatched base position creates a
  persistent entry pause and schedules reduce-only recovery.
- **One-leg timeout recovery**: with `risk_recovery_enabled`, both order tasks
  remain tracked. If only one OPEN/ADD leg has a confirmed fill by
  `hedge_timeout_ms`, the engine immediately sends a reduce-only reversal on
  that venue. It does not blindly unwind an EXIT while the other outcome is
  unknown; it waits for settlement and then restores net delta.
- **Failure containment**: a rate-limited venue pauses briefly; an unreachable
  venue is probed every `venue_probe_sec`. With the Kill Switch enabled,
  repeated execution failures pause new entries instead of continuing to add
  exposure.
- **Auditable fill prices**: Hyperliquid timeout/5xx recovery obtains actual
  fills by exchange order ID and computes fill-history VWAP. If quantity is
  known but actual price remains unavailable, Pair accounting is marked
  incomplete and OPEN/ADD stays persistently paused.
- **Live-only**: there is no simulated-fill mode. `--record-only` is the
  risk-free way to run it; anything else trades real money.

### Execution state machine

```text
NEW -> SIGNAL_CONFIRMED -> ORDERS_SENT
                              |       \
                              |        -> BOTH_FILLED -> COMPLETE
                              v
                           PARTIAL -> RECOVERY -> HEDGED -> COMPLETE
                                         \
                                          -> UNWINDING -> COMPLETE / FAILED
```

Every transition records timestamp, reason, Pair ID, and data. `RECOVERY` has
priority over expected arbitrage profit: the objective becomes restoring Delta
Neutral. `UNWINDING` is part of the state contract for full pair unwind flows;
the current immediate known-leg reversal remains recorded inside `RECOVERY`
because the counterpart may still settle afterward.

### Kill Switch

The runtime records typed risk events with one of `PAUSE_NEW_ENTRY`,
`EMERGENCY_HEDGE`, or `EMERGENCY_FLATTEN`:

- net delta above `max_unhedged_delta_usd` immediately blocks OPEN/ADD and
  requests emergency hedging; exceeding `max_unhedged_duration_ms` makes that
  pause persistent. If either leg cannot be priced, the timer keeps running;
- consecutive unequal/partial fills;
- consecutive execution failures;
- chain/local position reconciliation mismatch;
- session MTM loss limit, when configured;
- transient websocket/book staleness, venue API outage, and regime break.

Persistent triggers block OPEN/ADD but continue to allow risk-reducing EXIT and
hedging. Restart alone does not clear them. After operator review, restart with
`--clear-risk-pause`; the engine clears them only after live reconciliation
confirms both venues are flat and no emergency flatten is pending. Emergency
flatten is deliberately disabled by default. When enabled, a failed, locked,
bookless, or disconnected venue remains a persisted pending task and is retried
every `emergency_flatten_retry_sec`; zero max attempts means retry until flat.
No client can guarantee a fill while an external exchange is unavailable.
Shutdown waits only for a bounded period. Any execution still in flight is
persisted as unknown and blocks OPEN/ADD on restart; any restored non-terminal
execution state also requires reconciliation before new exposure is allowed.

Example operator reset after independently confirming that both venues are
flat (this performs live reconciliation; it is intentionally unavailable in
`--record-only` mode):

```bash
python3 main.py --symbol SNDK --hedge lighter-rh --clear-risk-pause
```

## Layout

```
main.py                  entry point (--record-only, or live by default)
entropy_arb/config.py    YAML + .env contract, validation
entropy_arb/book.py      order books + fee-aware crossing/sizing math
entropy_arb/pricing.py   current-book VWAP, executable edge, binary sizing
entropy_arb/midline.py   Fast/Slow baseline, volatility, Z-score, regime guard
entropy_arb/models.py    minimal Pair position + execution/risk contracts
entropy_arb/costs.py     funding forecast + quote/USD basis freshness guard
entropy_arb/ledger.py    durable Pair PnL ledger + restart snapshot
entropy_arb/metrics.py   rolling execution-latency percentiles
entropy_arb/session.py   one-switch crypto/US-equity session clock
entropy_arb/feeds.py     official HL ws + zkLighter ws book feeds
entropy_arb/venue_hl.py  Hyperliquid dex adapter (Entropy, tradexyz)
entropy_arb/venue_lighter.py  zkLighter adapter (mainnet, Robinhood chain)
entropy_arb/engine.py    the two-venue strategy loop
entropy_arb/dashboard.py Rich terminal dashboard
entropy_arb/recorder.py  1-minute orderbook bars
tools/analyze.py         minutes.csv -> suggested thresholds
tests/                   python3 -m pytest tests/
```

## Known risks

- **A wrong or contaminated baseline is a losing strategy.** Dynamic mode
  reduces manual drift but cannot distinguish every oracle/quote-asset change;
  keep the regime limits conservative and inspect recorded data.
- **Quote-source basis**: the independent basis guard removes known quote/USD
  movement, but the external spot source can itself be stale, unavailable, or
  less executable than its top of book. Enabled stale data blocks new entries.
  VWAP order bounds, venue position caps, delta/reconciliation limits, volume,
  account delta, and session MTM are converted with the same live rate.
- **Funding forecast error**: the entry model extrapolates the current rate for
  `expected_holding_hours`; future hourly rates can change. Venue history is
  reconciled into Pair PnL after the fact, not predicted perfectly.
- **Thin books**: VWAP sizing rejects clips that exceed visible slippage or
  impact limits, but the book can still move after the signal and slippage on
  a recovery hedge after a partial fill is real.
- **Trading-calendar exceptions**: stock mode handles recurring US-equity
  holidays and early closes, but cannot predict unscheduled national closures,
  venue-specific oracle freezes, or exchange rule changes. Session-disabled
  crypto mode is intentionally 24/7.
- **One-leg/exchange outage risk**: the bot persists and retries recovery, but
  cannot force an unavailable exchange to accept or fill an order. Manual
  operational monitoring remains necessary.

Use at your own risk. This is trading software operating with real money;
nothing here is investment advice. Start with tiny position caps.

## License

[MIT](LICENSE)
