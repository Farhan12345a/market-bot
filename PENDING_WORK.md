# Pending work

Open work only. Resolved items were removed 2026-09-02 — the record of what was
fixed and why lives in git history and in the code comments at each fix site,
which is where it is actually useful. Anything still listed here is genuinely
not done.

---

## Active entry-variable measurement window

Per CLAUDE.md: one entry variable at a time, held for a week, read via
`ops/session-metrics.py` against the prior window.

**Window started 2026-09-03**: `min_stock_price: 20` (from 10) + `max_stock_price: 400`
(from 300), tracked together as ONE variable - "universe price band 20-400".
`max_stock_price` was raised the same day the window started rather than run as a
second, separate variable, since 300 was found to be accidentally excluding META and
other names above that price - restarting the window kept this to one attributable
change instead of stacking two. Compare against the pre-2026-09-03 window once a
week of trades since this change exists.

---

# RISK MANAGEMENT — Tier 2 and 3

Tier 1 (rate limits, two-tier loss response, slippage persistence, no-entry-on-
stale-data) SHIPPED 2026-09-02. What follows is what is left from that review,
ordered. **Tier 2 is "before real money"; Tier 3 is process, not code.**

## R1. Fee and commission modelling in replay/grid — SHIPPED 2026-09-02

Nothing in this repo models execution cost. Zero references to commission,
SEC fee or TAF anywhere.

Alpaca is commission-free but not cost-free. On SELLS only:
  - SEC Section 31 fee: ~$0.0000278 per dollar of principal
  - FINRA TAF: $0.000166 per share, capped at $8.30 per trade

Per trade this is cents. That is not the reason to build it. The reason is
that **ops/replay.py and ops/grid.py model zero cost**, so every config
comparison they produce is optimistic in the same direction for every cell -
and the cells being compared differ mostly in HOW MANY TRADES they take. A
config that trades twice as often looks equally good and is not. Until this
exists, no grid result should be trusted to choose between configs of
different trade frequency.

Shape: a `costs` block in config (per-share, per-dollar, caps), applied in the
replay/grid P&L computation and reported as a separate line so its size is
visible rather than buried.

## R2. Intraday liquidity cap — SHIPPED 2026-09-02

The screener filters on `universe_min_dollar_volume: 3000000` and
`min_avg_volume: 1000000` - both DAILY averages. Nothing checks position size
against liquidity at the moment of entry.

The failure this misses: a name with $3M of daily dollar volume can trade
almost none of it in the minute you are buying. A $9,000 slot share into a
symbol printing $40,000/minute is 22% of that minute's volume, and the fill
reflects it - which is a slippage cost this codebase now measures but does not
prevent.

The data is already in hand: `volume_history` is a 20-sample deque per symbol,
maintained every poll. Shape: cap position notional at some fraction (1-2%) of
recent 1-minute dollar volume, floored so a thin print does not refuse an
otherwise good entry outright.

## R3. Halt detection — SHIPPED 2026-09-02 (entry refusal only)

No `trading_status` polling anywhere. A stock can halt while held, and a -0.5%
stop is not a promise about execution: if it reopens at -3% the stop fills
there. Nothing can prevent that, but two things limit the damage and neither
exists:
  - refuse ENTRIES on a halted or auction-state symbol
  - treat "a held position halted" as its own alert, because the human
    response (wait, or close on reopen) is not something the bot should guess

Alpaca's asset model carries a tradable/status flag; LULD band data is not on
this plan. Note this interacts with R2 - halts cluster in exactly the thin
names the liquidity cap would already be sizing down.

## R4. Position/exposure reconciliation — SHIPPED 2026-09-02

**Not on the original list; added because the evidence for it is already in.**
The bot's idea of what it holds and the broker's have diverged twice: phantom
positions (2026-09-01, 2026-09-02) and partial fills (AI 400 -> 152, OLLI
109 -> 14). Both were caught by guards written after the fact, each covering
its own specific case.

A periodic hard reconcile - broker is truth, bot adjusts, ALERT on any
mismatch - catches the whole class rather than the instances. It is cheap:
`get_positions()` is already fetched every poll for the exposure snapshot.

## R3b. Halt detection — WHAT IS STILL MISSING

Entry refusal shipped. Two halves did NOT:

  - **A held position that halts.** No alert fires. The right response (wait
    for the reopen, or close into it) is a human decision the bot should not
    guess at, so this wants an alert rather than an automatic action.
  - **LULD bands.** Not available on this plan. A halt is detectable; the
    price bounds it will reopen within are not. So position sizing remains the
    only real defence against a gap, which is what R2 is for.

## R4b. Reconciliation — REPORTS ONLY, BY DESIGN

Shipped as report-and-alert, never repair. The right repair differs by cause:
a phantom should be dropped, a partial resized, an unexpected short never
silently adopted. Automatic repair would also destroy the evidence of what
caused the divergence, which this codebase has needed every time.

Revisit only if the alert fires repeatedly for the same cause - at that point
the cause is known and a targeted repair is safe.

## R5. §475(f) mark-to-market election — TIER 3, NOT CODE

The strategy buys, stops out at -0.5%, and can re-enter the same name minutes
later. The wash-sale rule is 30 days, so a `reentry_cooldown_minutes: 5` does
nothing about it - an active day trader generates wash sales constantly.

For a trader flat at year end they largely wash out. The actual answer is the
IRS §475(f) mark-to-market election, which has a filing deadline (generally by
the original due date of the PRIOR year's return) and is a conversation with a
trader-tax CPA, not something to build. `trade_history.csv` and
`trade_context.csv` are already complete enough to hand over.

**Spend zero engineering time here.** Recorded so it is not forgotten, not so
it gets built.

## R6. Position sizing model — DECISION NEEDED, not work

Requested: "max position $5,000". The hard cap is currently
`max_position_per_stock_usd: 10000`, but it is NOT the binding constraint -
the even-slot-share is ($100k x 0.9 / 10 slots = $9,000).

Setting a $5,000 hard cap halves every position and leaves 10 slots using only
50% of equity, at which point `max_total_exposure_fraction` stops describing
anything real. If $5,000 positions are wanted, the lever is
`max_concurrent_positions` (10 -> 18) or the exposure fraction. Same position
size, model stays coherent.

## R7. The sample-size gate on scaling

`WHEN I START WINNING` (bottom of this file) gates scaling on "a stable edge".
Worth naming the measurement precisely: **the sample that matters is trades
since the last config change, not total trades.** ~400 trades exist; the
number on any single stable config is approximately zero, because every recent
session changed several variables at once. That number resets to zero again
with the 2026-09-02 batch.

---

# RANKED — what is actually left

Highest priority first. The ranking is by *expected effect on the next
session*, not by how interesting the work is.

| # | Item | Why it ranks here | Blocked on |
|---|---|---|---|
| 1 | **Notification keys on the Droplet** | Every alert is built and wired, and none can deliver until Pushover/Resend keys exist. A silent 09:38 loss-limit day happens again otherwise. | Signup + two env vars. No code. |
| 2 | **Passive-limit entry experiment** | Entries are marketable limits now; the passive version could improve fills and LOWER P&L by missing the fastest movers. `ops/fill-rate.py` measures which. | One week on one config. |
| 3 | **Chop detection / retire `breadth_halt`** | 2026-08-28's shape (19 of 30 peaked under +0.5%, −$484) is unhandled. Folding it in as a 4th regime label inherits cadence, hysteresis and bearish-exit machinery already built. | Decision on retiring `breadth_halt`. |
| 4 | **Dead-process watchdog** | The bot not running at 09:25 is still completely silent. A dead process cannot alert about itself. | A cron on the Droplet. |
| 5 | **Milestone stop recalculation** | `DynamicStops.should_recalculate` exists and nothing calls it — stops are set at entry and never move as a position improves. | Touches `TradeManager`. |
| 6 | **Limit orders on ENTRIES** | Exits went marketable-limit 2026-09-02; entries are still market. 09-02 showed +0.45% and +0.91% entry slippage. Needs a fill-rate measurement first. | See 0e below. |
| 7 | **Continuation-score weights** | `cf_score` is unweighted pending a 2-week journal gate. | Journal data (checkpoint 2026-09-16). |
| 8 | **Rank within a burst** | Same 2-week journal gate. | Journal data. |
| 9 | **Correlation limiter beyond sector** | Deliberately deferred; `max_positions_per_sector` went 3→2 on 2026-09-02 and needs weeks of evidence before a sharper tool is justified. | Sector-cap evidence. |
| 10 | **Threshold grid search** | Methodology settled (one variable per week, never a sweep). Purely a matter of running it. | Sessions. |
| 11 | **Per-method behavioural watchlists** | Speculative. No evidence yet that it beats the dynamic universe. | Nothing — lowest value. |

Everything under **WHEN I START WINNING** at the bottom of this file is gated
behind a demonstrated edge and is deliberately not in this ranking.

---


## A real answer for bearish tape (TODO - the halt is a bandaid)

`breadth_halt` (added 2026-08-30) stops new entries when the watchlist mean move
since the open is below -0.3% at 09:40. That is damage control, not a way to
profit. It rests on an ASSUMPTION nobody has measured: that a weak first ten
minutes implies a weak session. It will sometimes halt a day that recovers, and
those days leave no row in the P&L to notice them by.

Measure it before trusting it. The signal journal already records every signal
whether taken or not, so a halted day still has the counterfactual: compare the
forward returns of signals that fired AFTER the halt against what the halt cost.
If post-halt signals mostly rose, the assumption is wrong.

Real options, none of them free:
1. Short the weak side. Needs the sign bugs fixed first, and note no_shorting is
   being set on the account precisely to prevent accidental shorts - a
   deliberate short strategy would have to undo that consciously.
2. Trade an inverse ETF (SQQQ, SH) as a long. No shorting machinery needed and
   no borrow. Probably the cheapest honest path.
3. Require positive market breadth as an ENTRY condition rather than a halt, so
   the size scales with the tape instead of switching off.

The deeper point from 2026-08-28: `edge` was +1.03pp on both a winning and a
losing session. Selection is adding value in both regimes. What is missing is
something to be long OF on a day with no upside in it.

## 0e. Limit orders for streamed symbols only - INVESTIGATE, do not assume

Proposed 2026-08-25: use LIMIT buys for the ~14 streamed symbols (where the
price is live) and MARKET buys for the rest. The reasoning is sound - the
objection to limit orders is stale pricing, and streamed symbols do not have
stale prices.

**What has to be measured first.** Fill rate. The entry signal is "up 0.3% in 3
minutes", i.e. buying into a move already running, so a limit at the signal
price is a bet the move pauses. Slippage is now +0.205% mean adverse; a limit
that misses even 1 entry in 5 costs far more than that in foregone winners
(CHWY +1.61%, DASH +1.37% on 2026-08-24).

**Cheap way to find out, no risk:** the signal journal already records
signal_pct and forward returns. Add the quote at signal time and, for each
signal, whether price traded back to the signal price within 60s. That
answers "would a limit have filled?" without placing a single limit order.
Two weeks of that decides it.

**If it goes ahead:** marketable limit (signal price + a few cents), not a
passive one, with a timeout that converts to market after N seconds - never a
resting limit that silently does not fill. Exits stay MARKET always: a limit
sell in a falling stock does not fill, which turns a stop into a suggestion.

---

## 4. Rank within a burst (needs journal data first)

The burst throttle currently takes the **first N** of a burst by list order —
arbitrary, not merit. Ranking would improve *which* get bought.

Do not build this until `logs/signal_journal.csv` has ~2 weeks of data. Every
ranking is a claim that the skipped signals would have done worse, and the journal
exists to test that claim. Candidate factors, best-evidenced first:

1. **Excess return vs SPY** — directly separates "this stock is strong" from "the
   market went up". Already logged.
2. **RVOL** — only became measurable on 2026-08-20 (the `end=today` bug meant it
   returned exactly 1.00x for every symbol, forever). Let it collect a week.
3. **Spread %** — the precise version of what `min_stock_price` approximates.
4. **Move maturity (inverted)** — see item 3.

Deprioritized: **news** (latency, unbacktestable on this data, expensive — RVOL
captures most of the same information as a number). **Per-symbol history** (~2
observations per symbol; guaranteed overfitting).

Remember ranking is not diversification: taking the best 3 of a burst is the same
bet held 3 times instead of 20. Only sizing and caps control that.

---

## Backlog from the 2026-09-02 feature dump (updated 2026-09-02, second pass)

User provided a large batch of quant-strategy suggestions. Config-only asks
(max_daily_loss_usd -> 500, take-profit tiers -> 40/30/30) were applied same
day; the rest are real features, each its own piece of work. A second pass
the same day, framed around the user's own diagnosis - "what's missing is
breadth, not better picking of what's there" - shipped three of these
(2, 3-partial, 4-partial) and a promised tool (7); the rest are explicitly
scoped below rather than rushed.

1. **Correlation limiter beyond sector - STILL WAITING (RANKED #9). It DOES
   already run on the normal entry path** (`main.py`, the per-poll candidate
   loop) at threshold 0.85; it did not fire on 2026-09-02's three XLK names
   because 0.85 only catches near-duplicates, not sector co-movement.
   `max_positions_per_sector` went 3 -> 2 that day, which is the guard that
   actually covers this. Original note follows.** max_positions_per_sector (08-31) is a proxy - buckets by ETF
   membership, not measured return correlation - and a real version (rolling
   5-min return correlation matrix, refuse a new entry above a threshold)
   would only ever REDUCE how many positions the bot is willing to hold at
   once. That cuts directly against the thing 2026-08-28's session already
   showed: `edge` (taken vs. do-nothing) is positive even on losing days, so
   selection isn't the constraint - opportunity is. Tightening a limiter
   before the sector cap has even been evaluated (it needs a few more weeks
   running first, per the original note below) would add a second,
   overlapping restriction to an already breadth-starved book, in the
   opposite direction from every other change made this session (regime
   sizing REPLACING a hard halt, the spread gate ADDING selectivity only
   where evidence already flagged real noise). A reminder was scheduled via
   send_later to revisit this after the sector cap has run a few more weeks -
   check whether P&L-by-sector-complex data (in the daily report) shows
   max_positions_per_sector actually binding often enough to need a sharper
   tool, or whether it's rarely hit and the real version is solving a
   problem that mostly isn't occurring.

2. **Market regime filter / position-size scaling - SHIPPED 2026-09-02.**
   `trading.regime_sizing` in config.yaml, `_regime_multiplier` in
   src/main.py. 100% size bullish, 50% neutral, 15% bearish (the requested
   0-25% band's midpoint), read from the watchlist's own breadth (reused
   from breadth_halt's measurement) plus SPY's move since the open.
   REPLACES breadth_halt's binary halt - breadth_halt stays enabled in
   config so its measurement keeps running, but main.py now only lets it
   actually halt when regime_sizing is off. QQQ was deliberately left out of
   the trend reading (not currently streamed/benchmarked anywhere in this
   file); adding it is real future work, not a blocker. Tested:
   tests/test_regime.py (19 cases). UNPROVEN, same status breadth_halt
   shipped with - watch the REGIME log line against session-metrics.py
   after a few live sessions.

3. **Dynamic, ATR/MAE-based stops - SHIPPED 2026-09-02 (second pass).**
   Wired and enabled: `_build_dynamic_stops` in src/main.py builds the engine
   at screener completion from the screener's per-symbol `_atr_pct` plus MAE
   history, and `_dynamic_exit_config` applies it at entry. ATR was the
   unblocking input all along - it needs no trade history, only a volatility
   measurement. Stops are CAPPED at final_exit_loss_pct so they can only ever
   be tighter. STILL OPEN: milestone recalculation (see RANKED #5) - stops are
   set at entry and never move as the position improves. Original note follows.
   ~~PARTIALLY BUILT, NOT wired to live stops.~~ The MAE-percentile half is done: `ops/mae-percentiles.py`,
   per-symbol and pooled percentile bands (50/75/90/95th) from mae_pct
   already logged per trade, with a --min-n guard so a thin sample doesn't
   get its own row. Deliberately not wired into any stop-placement decision
   yet, for two reasons: (a) most symbols have too few trades to trust a
   per-symbol percentile while the dynamic universe keeps reshuffling the
   pool daily, and (b) the OTHER half of this item - a true 1-min ATR
   calculator - is not built. `_get_volatility_percentile` in
   stock_screener.py is still the 5-bucket ladder flagged unfixed in item 0d
   above; combining a real MAE percentile with a 5-bucket volatility proxy
   would be a stop built on a proxy for a proxy. Fix 0d (or pull real minute
   bars for a proper ATR) before wiring this into final_exit_loss_pct.
   Milestone-based recalculation (entry, +0.5%, +1.0%, not continuous - the
   user's own thrashing-risk flag) is still just a design, no code.

4. **Opening-burst multi-factor gate - PARTIALLY BUILT; the spread gate it
   shipped was FIXED 2026-09-02.** The gate refused every candidate it
   measured on 2026-09-02 using unreadable IEX quotes (WDAY at $230 quoting an
   11.2% spread). `_usable_spread_pct` now discards implausible readings and
   treats unknown as no-information rather than as a refusal. The full cf_score
   composite is still not wired - original note follows. Shipped a spread
   gate (`opening_burst.min_move_to_spread_ratio`, default 2.0): refuses a
   move that isn't at least 2x its own bid-ask spread, operationalizing the
   HOOD example already documented in this mode's own config comments (a
   0.593% median spread wider than the move thresholds tried here). Did
   NOT wire in the full cf_score composite as originally proposed - by
   09:32-09:33 this mode decides within, vwap_pos/exhaustion/sector_strength
   all need a window that does not exist yet (opening_range_minutes is 5;
   the mode closes before that), and spy_pct is one value per poll so it
   cannot reorder candidates WITHIN a poll (every candidate in the same poll
   shares it) - a composite built mostly from None or from a constant offset
   would not be doing what "multi-factor" implies. Revisit once there is
   ~5 minutes of intraday history to compute those factors from, i.e. this
   naturally waits on nothing except time-of-day.

5. **Continuation/quality composite (Market 20 / momentum 25 / rel-strength
   25 / volume 20 / technical 10).** cf_score is a partial version, unweighted
   pending the 2-week gate in PENDING_WORK item 4 (see the TWO-WEEK CHECKPOINT
   trigger). Don't build a second scoring system in parallel - fit weights on
   the existing one first.

6. **Threshold grid search** (entry momentum 0.3-1.0%, ceiling 1.0-2.0%,
   entry window variants) - **methodology recommendation, 2026-09-02: change
   ONE variable at a time, one config per week (or two), never a full
   combinatorial sweep.** At ~20-70 trades/day a week is already a thin
   sample per config; splitting that further across a grid (e.g. 3
   parameters x 4 values = 64 combinations) would need over a year of
   sessions to fill honestly, and a shorter run per cell just fits noise -
   the exact trap continuation_weights was deferred to avoid, at greater
   scale. Concretely: pick the single highest-uncertainty parameter, hold
   everything else fixed for a week, compare against the prior week's
   edge/expectancy via session-metrics.py and the signal journal's
   forward-return columns (which record EVERY signal, taken or not, so a
   parameter's effect on what got skipped is measurable too), then move to
   the next parameter. This is the same discipline already visible
   throughout this file's history (rapid_increase_pct, entry_window_start,
   take_profit_tiers, etc. each changed alone with the before/after evidence
   recorded) - it just names it as a deliberate procedure rather than
   something that happened to be the style. Data collection for this needs
   no new work: `analytics.log_signals` is already on and already records
   forward returns at 15/30 min for every signal, taken or skipped.

7. **BE-outcome distribution tool - SHIPPED 2026-09-02.**
   `ops/be-outcomes.py`. For trades whose mfe_pct cleared a trigger (default
   0.15%, the opening-burst tier), reports how many also ran to +0.75%,
   +1.0%, closed at -0.3% or worse despite touching the trigger, or
   scratched via BREAKEVEN_STOP, plus the full exit_reason breakdown and
   whether BREAKEVEN_STOP's mean pl_pct actually lands near the intended
   +0.05% floor. Tested: tests/test_be_outcomes.py (14 cases, hand-checked
   fixture). Cannot answer event ORDER (did it fall to -0.3% before or after
   touching the trigger) - mae_pct/mfe_pct don't carry timing, and the tool
   says so rather than assuming it.

8. **Soft loss-velocity warning below the hard max_daily_loss_usd ceiling -
   SHIPPED 2026-09-02** (`trading.loss_velocity_warning` in config.yaml,
   `Executor.check_loss_velocity`). Fires once per threshold at 40/60/80% of
   max_daily_loss_usd (-$200/-$300/-$400 at today's $500), reporting BOTH
   depth and velocity ($/min since the first check, projected to when the
   ceiling would be reached at that rate) - because -$300 by 10:00 and -$300
   by 15:30 are the same depth and very different days. Warning only: it
   never halts, resizes, or blocks an entry, and `tests/test_velocity.py`
   asserts that directly. Original write-up: 500 is currently doing
   double duty as both "circuit breaker" and "the only number that exists" -
   there is no distinction yet between a normal day's expected drawdown
   (worst so far: -$546.24 on 08-31, i.e. already over the current $500
   ceiling once) and the exceptional-event ceiling itself. Still not built.
   Shape: track realized-loss velocity intraday (e.g. $ lost per N minutes,
   or loss as a fraction of max_daily_loss_usd reached by a given clock
   time) and log/notify a soft warning well before the hard stop fires,
   without changing the hard stop's own behavior. Worth doing before the
   next real-money re-derivation of max_daily_loss_usd (see the REMINDER
   already on that config key) - a soft warning gives an early read on
   whether the hard number is even the right order of magnitude.

**Weekly (not just daily) analysis - already covered, no new tool needed.**
`ops/session-metrics.py` already re-derives its tables from the FULL
history in `logs/trade_history.csv` and `logs/signal_journal.csv` on every
run (not a rolling daily snapshot) - `--since` narrows it to any window,
including a trailing week (`--since $(date -d '7 days ago' +%F)` on the
VPS). Nothing needs to be built to get a weekly rollup; the data is already
continuously accumulated and the tool already re-reads all of it each time.

---

## 0e. PROPOSED (user, 2026-08-21): per-method behavioural watchlists

**The idea.** Watch how symbols behave in the first 30 minutes, and remember
the ones that repeatedly satisfy a given signal's criteria - e.g. "XYZ made
+0.3% in 3 min on 3 of the last 4 days". Keep a SEPARATE list per config/method,
and when that method is enabled, feed its list back into the watchlist. Lists
stay dynamic and rebuild themselves as behaviour changes.

**Why this is worth doing.** Candidate generation is currently the weakest link:
a hand-written 50-name `stock_universe` plus a screener scoring gap and
momentum. Neither asks the only question that matters for THIS strategy - does
this symbol actually produce the move the entry logic looks for? A behavioural
list answers that directly, from observation.

**Most of the data already exists.** `logs/signal_journal.csv` has recorded
every signal since 2026-08-21, taken or not, with signal_pct, excess vs SPY,
RVOL, spread, burst width and forward returns at 15/30 min. This feature is
largely a READER over data already being collected, not new plumbing.

**The distinction that decides whether this works.** Two different things could
be remembered, and they are not equally safe:

  (a) symbols that repeatedly TRIGGER the signal - a behavioural fact, several
      observations per symbol per week, cheap to measure, low overfitting risk.
  (b) symbols that repeatedly MADE MONEY on that signal - an outcome fact, at
      roughly 1-2 trades per symbol per week and a ~24% win rate, a "3 of 4
      winners" symbol is overwhelmingly likely to be noise.

The user's phrasing ("stocks that succeeded") points at (b). **Build (a) first.**
It is honest with the data volume available, and it improves candidate
generation on its own. Layer (b) on only once the journal can show that a
symbol's past win rate predicts its future one - which is a claim the journal is
there to TEST, not to assume.

**Sequencing.** Needs ~2-3 weeks of journal data before any list is meaningful,
which puts it after the ranking work (item 4) and alongside it. Not blocked on
anything else.

---

## 1d. Entry quality is the larger problem — do not let exits distract from it

| Metric        | 2026-08-19 |
|---------------|------------|
| Avg win       | +$73.60 (+0.74%) |
| Avg loss      | -$55.49 (-0.85%) |
| Payoff ratio  | **1.33x**  |
| Win rate      | **23%**    |

Winners are already 1.33x losers, so the exit logic is not what is bleeding the
account. At a 1.33x payoff the breakeven win rate is **43%**; the strategy is at
23%. Better profit capture on a 23%-hit-rate entry signal still loses money.

Fix the exits because they are cheap and correct to fix — but the entry signal is
where the actual expectancy gap is.

---

## 7. Claude Code is not installed on the VPS — deploys are still manual

Decided days ago (option B: install Claude Code on the Droplet rather than
push-from-here / pull-on-the-box), never done. The open question then was
whether this container could reach the VPS over SSH and do it directly.

**Answered 2026-08-21: it cannot.** Outbound port 22 times out from here, the
same wall that blocks SMTP, the WebSocket, yfinance and api.nasdaq.com. So this
container can only ever push to GitHub; something on the VPS has to pull.

Cost of leaving it: every change waits on a manual `git pull && systemctl
restart`, and commits queue up unshipped. There were **10 queued** on 2026-08-21.

One-time fix, run on the Droplet:

    cd /root/market-bot && git pull && bash ops/setup-claude-on-vps.sh

Thereafter `bash ops/deploy.sh` pulls, syntax-checks, parses config, import-
checks, prints the settings about to go live, warns if the market is open, and
rolls back if the service doesn't come up.

---

## 6. Tuning values deliberately NOT changed

- `rapid_increase_pct: 0.5` — raising it is backwards, see item 3.
- `resistance_lookback_samples: 3` — superseded by `resistance_min_decline_pct`,
  which fixes the actual defect (no magnitude floor) rather than just requiring
  more consecutive ticks.

---

## 10. Data that is NOT obtainable on this plan — do not design around it

Recorded 2026-08-26 after a proposed opening-volatility model leaned on two
inputs the account cannot get. Both are genuinely strong predictors; neither is
available, and finding that out after building around them would be expensive.

**Options implied volatility / expected move.** The `S x IV x sqrt(T/365)`
expected-move calculation needs an options chain with ATM IV, ideally 0DTE/1DTE.
Alpaca sells options market data on paid tiers only. Not available.

**Opening auction imbalance.** Imbalance side, imbalance quantity, paired
shares, indicative clearing price. These come from NYSE/Nasdaq proprietary
auction feeds — institutional products, not something a retail plan carries, and
not exposed by Alpaca at any tier. Not available at a realistic price.

**What IS obtainable, roughly in order of cost:**

| input | status |
|---|---|
| gap %, RVOL (daily), ATR, 5-day return | already built into the screener |
| historical opening volatility | already built (`opening_hit_rate`) |
| relative strength vs SPY | already built (`cf_rel_strength`) |
| VWAP position, volume acceleration, efficiency, exhaustion | already built |
| sector ETF relative strength | obtainable and cheap — item 9 |
| pre-market range / pre-market RVOL | obtainable via extended-hours bars, but see caveat |
| news / analyst changes | Alpaca news API is free; latency and unbacktestability are the problems |
| options IV | **PAID — not available** |
| auction imbalance | **NOT AVAILABLE at any realistic price** |

**Caveat on pre-market data specifically.** This account is on the IEX feed,
roughly 2% of consolidated volume. Pre-market liquidity is thin to begin with
and thinner still on one venue's share of it, so pre-market RVOL and range here
are weak, noisy signals rather than the strong ones the literature describes for
consolidated data. Worth collecting; not worth weighting heavily.

**The larger point, which matters more than the missing inputs.** A model that
predicts |move| predicts VOLATILITY, not direction, and `score_stock`'s own
docstring already reads "Score a stock 0-100 for likelihood of volatility in
first 30 min." The entry signal then fires on `rapid_increase` — a stock that IS
moving. Volatility is therefore already selected for twice. On 2026-08-26, 12 of
22 positions went the wrong way immediately; those were not quiet stocks failing
to move, they were stocks moving against a long-only book. **The gap is
direction, and direction is the hard half.** More volatility modelling buys more
of what the bot already has.

## Test suites

**1,066 cases across 26 suites as of 2026-08-26.** Run them with:

    ./ops/runall.sh

**They now live in `tests/`, in the repo.** Until 2026-08-26 they sat in the
session scratchpad — outside version control, on an ephemeral container, and
therefore one reclaim away from losing the whole safety net. They also hardcoded
one machine's checkout path, so they only ran where they were written.
`tests/_repo.py` derives the repo root from its own location instead.

**Running the suite cannot touch the live logs.** Several suites exercise code
that resolves paths relative to the CURRENT directory — `save_trades_log()`
appends to `logs/trade_history.csv`, the signal journal writes
`logs/signal_journal.csv` — and two of them used to `chdir` to the repo root
first. Harmless on a dev box with throwaway logs; on the VPS those are the live
files behind every report and behind ANALYSIS_LOG.md. `sandbox_cwd()` gives them
a disposable directory of the same shape, and `runall.sh` independently runs
each suite from a fresh temp cwd. Verified by checksumming `logs/*.csv` either
side of a full run.

The runner fails loudly if a suite dies before printing its own summary — it
previously counted PASS/FAIL lines only, so three stale suites reported "0 fail"
while silently skipping most of their cases.

**Run a coverage audit when adding a feature**, not just the suite. On
2026-08-26 an audit mapping each new feature to its tests found three with NO
coverage at all - the eight continuation factors, adaptive polling, and the
post-exit note logic - despite the suite being green at 946 cases. Green means
what is tested passes, never that everything is tested.

The same gap reappeared the same day: `_rank_burst` and `csv_schema` shipped
with no direct coverage until `test_schema.py` was written for them. That suite
checks, among other things, that repairing a stale header does not land a legacy
row's `taken` value under `opening_hit_rate` — the exact corruption the first
implementation of the repair would have caused.

**Tests that pin a live config value will break when you change it, by design.**
Nine did on 2026-08-26. The fix is not to loosen them: mechanism tests should
build their own config explicitly so they test the code, and intent tests should
assert the current decision with the reasoning attached. A test reading
`config.yaml` is testing the config file, not the code.

# WHEN I START WINNING

**Nothing in this section gets built until the strategy has strung together
winning days.** That is the entrance requirement, not a figure of speech.

Every item here makes the bot do MORE — more symbols, more trades, more size,
more exposure per idea. Each one amplifies whatever the strategy currently
does. Amplifying a negative expectancy just loses money faster, and does it
while making the cause harder to see, because more moving parts means fewer
sessions where any single change is attributable.

The gate is deliberately vague on purpose — "a stable edge" is a judgement,
not a threshold someone can rules-lawyer past on a good week. As a rough
shape: several consecutive profitable sessions, on a symbol pool and config
that were not changed between them, with `ops/session-metrics.py` showing the
`edge` figure (taken vs. do-nothing) positive across the run and not carried
by one outlier trade.

---

## 1. Low-float small caps on news, halt resumptions, biotech catalysts

Named 2026-09-02. **Requires Alpaca Algo Trader Plus (~$99/mo) — remind me of
this section when I subscribe.**

The highest-upside item on the list and the most likely to hurt, for the same
reason: these names actually move 20-40% instead of 1.5%.

WHY IT NEEDS PLUS. IEX carries ~2% of US volume and its coverage is *worst*
exactly on thin, news-driven names — so prices would be least reliable where
they matter most, and the bid-ask readings that already produced an "11.2%
spread" on a $230 large cap would be worse here, not better. Halt resumptions
additionally need `get_asset` trading-status polling that is not wired at all.

WHY IT IS NOT JUST MORE SYMBOLS. Every risk control currently in the codebase
is calibrated for names that move ~1%:

  - a -0.35% burst stop is inside the first tick on a halt resumption
  - `max_extension_from_open_pct: 1.0` refuses everything — a low-float on
    news is +8% before the first poll
  - `volatility_sizing` floors at 0.35x, which is still far too large
  - position sizing would hand a $0.80 stock 4,000 shares against a book that
    cannot absorb 400

It also runs directly against the ETF exclusions shipped 2026-09-02, which
removed instruments whose behaviour did not match the strategy's assumptions.
This adds a class with the same mismatch in the other direction.

SHAPE WHEN BUILT: a **separate strategy with its own sizing, stops, gates and
its own daily loss budget** — not symbols poured into the existing pool.
Merging them means one parameter set serving two incompatible distributions,
and the 1.5% names will dominate the fitting because there are more of them.

## 2. Incrementally increasing trade count as the day goes on

Named 2026-09-02. Scale the number of entries with how the day is actually
developing rather than committing the whole budget in the first ten minutes.

THE EVIDENCE FOR IT. 2026-09-02 took 22 entries between 09:31 and 09:38 and
hit the daily loss limit at 09:38:19 — the entire day's risk budget spent in
seven minutes, on one reading of one minute of tape. A day that opened badly
had no capacity left to participate in a recovery, and a day that opened well
had no way to press.

SHAPE: `max_daily_entries` becomes a schedule rather than a scalar — a cap per
half-hour block that widens when the session's own results justify it (realised
P&L positive, hit rate above the payoff-implied breakeven, regime not bearish)
and stays narrow otherwise. Note this is the inverse of the `loss_velocity`
warning: that one watches how fast the day is going wrong, this one watches
whether it has earned the right to do more.

CONFLICT TO RESOLVE FIRST: `regime_sizing` already scales entry SIZE on a
continuous read. A second mechanism scaling entry COUNT on a different read
can disagree with it. Decide which one owns "how much risk is on" before
building the second.

## 3. Pyramiding wins / scaling into a position

Named 2026-09-02. Add to a position that is working rather than taking full
size at entry.

WHY IT IS PARKED, and this is the substantive objection: **scaling in only
pays if winners run, and these do not.** Take-profit tiers are 0.75/1.0/1.25%
in the burst profile and 1.0/1.25/1.5% in the session profile; nothing on
2026-09-02 was held longer than 5m20s. Adding at +0.5% while selling a third
at +0.75% means buying and selling the same shares inside thirty seconds and
paying the spread twice for it. It also makes the average entry price WORSE on
winners (buying higher) while doing nothing at all on losers, which inverts
the asymmetry that makes pyramiding attractive in the first place.

Pyramiding is a rule for trades measured in hours, holding for multiples of R.
These are measured in minutes, holding for fractions of a percent.

PREREQUISITE, not a config change: **lengthen the holding period first.** If a
future version holds winners for 3-5% instead of 1.5%, revisit this — at that
point the arithmetic changes and it becomes a good idea. Until then it is a
cost with no matching benefit.

## 4. Raise the daily-loss ceiling

`trading.daily_loss_limit.ceiling_usd` is $1,000 and `pct_of_equity` is 1.0%
for paper testing. Both are deliberately capped so a growing account does not
silently authorise larger dollar losses — the account grows, the permitted
loss would grow with it, and nobody ever decided that.

When the edge is established: drop `pct_of_equity` to 0.75 (the number
actually intended for live risk) and raise `ceiling_usd` deliberately, as its
own decision, recorded here with the evidence that justified it.
