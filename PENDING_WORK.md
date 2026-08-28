# Pending work

Open items, most actionable first. Each has enough context to pick up cold.

---

## 0. ~~MONDAY: keep or remove the take-profit?~~ — RESOLVED 2026-08-21: **KEPT**

**Verdict: KEEP.** The 2026-08-21 session answered it, and answered against my
own recommendation.

Take-profit fired **7 times for 7 winners, +$150.63** - a third of the day's
$455 gross profit. My argument for removing it was built on 2026-08-20, when
only 3 of 25 trades ever reached +1%. On 2026-08-21, 7 of 22 did. The
difference was the $10 price floor and the 09:35 start putting the bot into
names that actually move, which the earlier evidence could not have predicted.

It was not free. It clipped the two best trades of the day:

| Symbol | Sold half at | The other half exited at |
|--------|--------------|--------------------------|
| HOOD   | +0.87%       | **+4.66%**               |
| SOFI   | +1.09%       | **+3.46%**               |
| MARA   | +0.07%       | -1.24% (take-profit saved this one) |

**That trade-off is what the tiers now address** (shipped 2026-08-24): 33% at
+1.0%, 40% at +1.25%, all remaining at +1.5%. Early certainty on the first two
thirds, a third left free to run.

The MARA row also exposed a real bug: it fired at a genuine +0.07% because the
exit rules were still measuring against the SIGNAL price rather than the fill.
Fixed by Strategy.correct_entry_price.

**No further action.** Do not set use_take_profit: false.

---

## 0-OLD. Original take-profit decision procedure (superseded, kept for context)

**This is the one item with a deadline.** `use_take_profit` was switched ON for
the 2026-08-21 session as a deliberate one-day experiment, at the user's
request, and the user asked to be told if it backfires so it can come out on
Monday.

```yaml
use_take_profit: true      # <- set to false to remove
take_profit_pct: 1.0
take_profit_fraction: 0.5
```

**The evidence going in argues against it.** On 2026-08-20, only 3 of 25 trades
ever reached +1%: MARA +$121, RIOT +$59, LYFT +$44. Those three *were* the
day's profit. Selling half of each at +1% would have clipped precisely the
winners while doing nothing at all for the other 22 trades. The strategy's
problem is a 23-24% win rate, not winners running too far - and a scale-out
lowers the payoff ratio, which raises the breakeven win rate. It is being run
anyway because one day of real data beats an argument, and because the MFE
column now makes the counterfactual measurable rather than arguable.

**How to judge it Monday, in order:**

1. In the report, count exits with reason `TAKE_PROFIT`. **Zero fires = no
   information gained, not a pass** - re-run it another day or drop it.
2. For each one, compare `mfe_pct` against the +1.0% trigger. MFE well above
   1.0% means the trimmed half was sold cheap; MFE at ~1.0% means it caught the
   actual peak and earned its keep.
3. Compare the day's payoff ratio to the 1.73x of 2026-08-20. A fall is the
   expected cost; the question is whether the win rate rose enough to pay for it.

**Verdict expected: REMOVE**, unless (2) shows MFE clustering near 1.0%.
Set `use_take_profit: false`. That single key restores the previous behaviour
exactly - verified by test, no other change needed.

---

## 0b. ~~Earnings + QQQ lists: endpoints unverified~~ — VERIFIED WORKING 2026-08-21

**The Nasdaq earnings endpoint works from the VPS.** First live run, 09:20 ET:
9 BMO rows for today and 40 AMC rows for yesterday, filtered to 5 candidates,
ranked, and BJ / BEKE / BKE added (56 watched -> 59). The 403s were a dev-
container artifact, exactly as suspected. QQQ correctly declined to contribute
(1/3 up-votes: gap +0.58% up, but 5d -2.75% and price below its 5d average).

**One weakness this exposed.** All three earnings picks scored an IDENTICAL
Movability of 26.2 and RVOL of exactly 1.00x, so the ranking was decided almost
entirely by the gap term. Two causes:

- `_get_volatility_percentile` is a 5-bucket ladder (10/30/50/75/95), not a
  percentile. 26.2 = 75 x 0.35, i.e. all three merely landed in the same
  1.5-2.5% ATR bucket. The heaviest-weighted term (35 pts) discriminates far
  less than its name suggests. Replace with a true rank across the candidate set.
- RVOL at exactly 1.00x for all three is the pre-2026-08-20 bug's signature.
  It may be legitimate pre-market (no intraday volume history yet), or the same
  fault in a different path. Check the next few sessions before trusting it.

Neither is harmful - the picks were reasonable - but the ranking is weaker than
the log makes it look. Original write-up below.

## 0c. Earnings + QQQ lists: original unverified note (2026-08-21)

Both lists shipped with full unit coverage, but the **network calls have never
succeeded from this container** - the dev proxy returns 403 for api.nasdaq.com,
invesco.com and even Yahoo, exactly as it does for the WebSocket. The VPS is a
different network and runs yfinance fine in production, so this is most likely
a dev-container artifact - but it is *unproven*, same status as the stream was.

**First run to check, in the 09:20-09:30 ET window:**

```
grep -E "Earnings calendar|EARNINGS LIST|QQQ trend|QQQ LIST|List augmentation" \
  <(journalctl -u market-bot --since today)
```

- `Earnings calendar 2026-08-24 (BMO): N rows -> M usable symbols` - working.
- `Earnings calendar unreachable ... ProxyError/HTTPError` - the endpoint is
  blocked or has moved. Fall back to a keyed provider (Finnhub and FMP both
  have free tiers with a proper `earnings-calendar?from=&to=` route), or
  disable with `use_earnings_list: false`. Nothing else breaks either way.
- `QQQ is not trending up` is a **normal** message, not a failure - the list is
  gated on the index and will be skipped on plenty of days.

Also worth an eye: `src/screener/qqq_constituents.txt` is a static list dated
2026-08-21. Nasdaq reconstitutes each December. Re-check it quarterly.

---

## 1. ~~Pre-market gap is always 0.0%~~ — FIXED 2026-08-20

**Status: FIXED.** `_get_recent_gap` now measures yesterday's close against the
CURRENT price (latest minute bar, falling back to the quote midpoint), so it works
before the bell. The old today's-open path remains as a fallback for post-open runs.
Verified live: 9 of 9 symbols produced non-zero gaps spanning 0.21%–15.44%, against
0 of 3 on the pre-market run that morning.

Note the function returns a float, not None — `score_stock` does `min(25, gap * 5)`
and would break on None. "No data" and "no gap" therefore still both score 0; that
distinction was in the original plan and was dropped as not worth touching scoring for.

Original write-up below.

**Found:** 2026-08-20, first pre-market screen.

`StockScreener._calculate_gap` asks for two daily bars (yesterday's close, today's
open) and returns `0` when it can't get both:

```python
if symbol not in bars or len(bars[symbol]) < 2:
    return 0
```

Pre-market, today's daily bar doesn't exist yet, so **every symbol scores a 0.0%
gap**. Moving the screener to 09:05 (commit `4110eca`) silently disabled the gap
component.

Observed that morning: MRVL, CADL and CMG all showed `Gap: 0.0%` and landed on an
identical `Score: 44.0`; only 3 stocks were selected vs 8 the day before. The day
prior, MRVL had shown `Gap: 11.2%`.

**Fix:** compute gap as *current pre-market price vs yesterday's close* rather than
*today's official open vs yesterday's close*. This is the better measure anyway —
it reflects where the stock is actually trading now, not a single opening print.
Yesterday's close comes from the daily bar that does exist; the pre-market price
comes from the latest minute bar or `broker.get_latest_quote()`.

Keep the current behavior as a fallback for when no pre-market print exists, and
return `None` rather than `0` so "no data" is distinguishable from "no gap" —
right now they score identically.

**Impact if left:** small. The screener's picks are merged with the 50-name
`stock_universe`, so ~53 symbols get watched regardless; only the extra few and
their ordering are affected.

**Worth testing rather than assuming:** MRVL had the highest gap on 2026-08-19
(11.2%) and was the second-worst symbol (-$328). Gap may be a *negative* signal at
these thresholds. Check the signal journal before weighting it more heavily.

---

## 0a. NEXT: breakeven floor instead of a wider trailing stop

**Do this before deciding whether 1.35% was right.** It solves the same problem
the wider trail was meant to solve, without the side effect that makes the
wider trail dangerous.

**The problem.** On 2026-08-24, DASH, ADBE and ADSK each reached +0.5-0.6% and
were then cut by the 0.75% trailing stop for small losses, never reaching the
+1.0% take-profit tier. That is the trail and the tiers fighting each other.

**Why widening the trail is the wrong fix.** A trail only fires above the -1.0%
final exit if the peak clears a threshold:

| trailing_stop_pct | peak must exceed |
|-------------------|------------------|
| 0.75%             | -0.25%           |
| 1.35%             | **+0.35%**       |
| 1.50%             | +0.51%           |

At 1.35%, five of that day's nine trailing exits lose the trail entirely and
fall to -1.0%. Three of them never traded above entry at all - for those the
0.75% trail was acting as a stop TIGHTER than the final exit and was saving
money (HOOD left at -0.42%, not -1.0%). Modelled pessimistically the day goes
from -$119 to about -$317.

**The fix.** Compose a floor with the existing trail rather than replacing it:
effective stop = max(trailing_level, breakeven_floor).

```yaml
breakeven_trigger_pct: 0.5   # once up this much...
breakeven_floor_pct: 0.0     # ...the stop never goes below entry
                             # a small positive value covers the exit spread
```

Then DASH/ADBE/ADSK exit at ~breakeven instead of -0.15/-0.19/-0.24%, AND the
three that never went green keep the tight 0.75% trail that protected them.
Restore `trailing_stop_pct: 0.75` at the same time.

**Set breakeven_trigger_pct from the mfe_pct column, not from this document.**

---

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

## 0d. TODAY (after the close): fix the burst ranking

See 0b. Two concrete changes:

1. `_get_volatility_percentile` returns one of five hardcoded values
   (10/30/50/75/95) by ATR band. Make it a TRUE percentile: score every
   candidate's ATR%, then rank each against that day's distribution. This is
   the 35-point term - it should separate names, not bucket them.
2. Establish whether RVOL at exactly 1.00x pre-market is legitimate (no
   intraday volume yet) or the 2026-08-20 `end=today` bug in another path. If
   the former, exclude RVOL from the PRE-OPEN ranking rather than letting every
   candidate carry an identical 0 - a term that never varies is noise with a
   weight attached.

Also: drop ETFs from the tradeable list. `max_stock_price: 300` already blocks
QQQ at $711 by accident, but an ETF at $80 would sail through, and an index
fund cannot burst the way a single name does. A simple `exclude_symbols` list,
or a flag on known ETFs, is enough.

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

## 1-MON. AFTER MONDAY'S RUN: settle the subscription-counting question

**Ask this the moment Monday's session ends.** It is worth double the streamed
coverage and it takes one config change to answer.

`stream_max_subscriptions: 28` assumes bars and trades each count as one
subscription, so with ticks on only **14 symbols** stream. If Alpaca actually
counts UNIQUE SYMBOLS, 28 symbols could stream with ticks and half the capacity
is being wasted.

**How to tell.** In Monday's log, find:

    PriceStream opening iex connection for 14 symbols (28 subscriptions: bars + trades)

- No `symbol limit exceeded` and bars flowing -> the cap works, nothing proven
  yet about which model is right.
- Then next session set `stream_max_subscriptions: 56` (= 28 symbols x 2). If
  it still connects, Alpaca counts unique symbols and the cap can stay there.
  If the 405 returns, the conservative reading was right - revert to 28.

Either way the failure is now caught in ~2s by name rather than after 120s of
silence, so testing it costs a couple of seconds, not a session.

**Also review Monday:** whether ticks are worth their halved coverage at all.
The measured prize was ~$116/day of entry slippage on 2026-08-20 and most of
that is the 15-minute REST delay, not sub-bar timing. `use_trade_ticks_for_entry:
false` doubles reach immediately.

---

## 1a. MONDAY 2026-08-24: poll interval — **BLOCKED, gate not met**

**Status as of 2026-08-24 08:46 ET: NOT starting this.** The work is gated on
the WebSocket stream being confirmed working, and it is not.

- 2026-08-21: the stream connected in 436ms and was then rejected with
  `symbol limit exceeded (405)` for 59 symbols. Zero bars, full session on REST.
- 2026-08-24: the subscription cap (28 subs / 14 symbols) ships today, but this
  session has not run yet. The gate stays unresolved until its log is read.

Dropping entry_check_interval_seconds to 5-10s on REST would mean roughly 700
calls/min and immediate rate-limiting - which would degrade the REST path the
bot is currently depending on entirely. **Re-evaluate after a session in which
bars actually flow.**

Step 1 below (time-based windows) is worth doing on its own merits and is NOT
gated - but it is an exit-logic change and does not belong in a deploy made
minutes before an unattended session. Next quiet window.

Original write-up follows.

## 1a-OLD. poll interval + time-based windows

Do these two together, in this order. The second is a prerequisite, not a
nice-to-have.

**(a) Convert sample counts to time durations FIRST.**
`momentum_fade_window_samples` and `resistance_lookback_samples` are counts of
polls, not spans of time. At today's 60s poll they mean 5 min and 3 min. Drop
the poll to 5s without changing them and they silently become **25 seconds and
15 seconds** - both exit rules become 12x more sensitive, re-creating exactly
the hair-trigger behaviour fixed on 2026-08-20. Express both as minutes and
derive the sample count from the live poll interval.

**(b) Then lower `entry_check_interval_seconds`** from 60 to 5-10.

Gated on the stream: at 59 symbols and a 5s poll this is ~700 REST calls/min,
which will rate-limit. It is only safe once bars arrive by WebSocket push.

**Stream status as of 2026-08-21 09:22 ET: still unverified** - the connection
attempt happens at the open. If it fell back to REST, do NOT do (b) at all;
(a) is still worth doing on its own merits.

---

## 1b. Record max favorable excursion (MFE) — do this BEFORE the breakeven stop

**Why first:** there is currently no way to measure how much unrealized profit
gets round-tripped. `TradeManager` tracks `highest_since_entry` in memory and
throws it away at exit, so "this trade was up 0.8% before it stopped out at -1%"
is invisible in every log, CSV and report. Any breakeven-stop threshold picked
today would be guesswork.

**Fix:** persist peak-since-entry onto the trade row in
`Executor.submit_exit_order`, as both a price and a % of entry:

- `mfe_pct` — max favorable excursion, the best unrealized gain reached
- `mae_pct` — max adverse excursion, the worst drawdown reached (`TradeManager`
  would need to start tracking a `lowest_since_entry` alongside the existing high)

Add both to `trade_history.csv` and the HTML report. Then the question becomes
answerable directly: of the 52 losing trades on a day like 2026-08-19, how many
were up more than 0.5% at some point first? That number sets the breakeven
threshold instead of intuition setting it.

---

## 1c. Breakeven stop — once a position is up X%, never let it become a loser

**The gap, precisely.** `trailing_stop_pct: 0.75` exits at `peak x 0.9925`. For
that level to sit at or above entry, the peak must first reach **+0.76%**:

| Peak   | Trailing stop exits at |
|--------|------------------------|
| +0.30% | **-0.45%**             |
| +0.50% | **-0.25%**             |
| +0.76% | 0.00%                  |
| +1.00% | +0.24%                 |
| +2.00% | +1.24%                 |

Below +0.76% the trailing stop sits BEHIND the entry price and can never fire
before `final_exit_loss_pct` (-1.0%) does. The average winner on 2026-08-19 was
**+0.74%** — below that threshold. The trailing stop is therefore inert across
the band where most trades actually live. It is not broken; the leash (0.75%) is
simply longer than the typical move (0.74%).

**Fix:** a breakeven floor, composed with the existing trail rather than
replacing it. Effective stop becomes `max(trailing_level, breakeven_floor)`.

```yaml
breakeven_trigger_pct: 0.5   # once up this much...
breakeven_floor_pct: 0.0     # ...the stop never goes below this (0 = entry price)
                             # a small positive value covers the spread on exit
```

Keeping the 0.75% trail matters: tightening it to 0.4% would also close the dead
zone but would strangle the runners, cutting a trade like NU (peaked ~+2.8%) far
earlier. The floor fixes the low end without touching the high end.

**Set breakeven_trigger_pct from the MFE data, not from this document.**

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

## 2. WebSocket stream — CONNECTS FINE, was OVER-SUBSCRIBED (2026-08-21)

**The network was never the problem.** First live run reached
`connected to wss://stream.data.alpaca.markets/v2/iex` in 436ms. The 403s from
the dev container were a container artifact, exactly like api.nasdaq.com.

**What actually failed:** `error: symbol limit exceeded (405)`. Alpaca's free
IEX feed caps subscriptions per connection; 59 symbols were requested. The
failure mode is nasty - the socket opens, reports connected and healthy, and
then delivers ZERO bars forever. The watchdog took the full 120s to notice and
fell back to REST for the whole session.

**Fixed, awaiting its first live run:** `stream_max_subscriptions: 30`, with
bars and trades counted as separate subscriptions (so 15 symbols with ticks on).
The screener's picks and the day's earnings adds get the slots; everything else
uses REST, which is what all 59 did before the stream existed.

**Still unknown, and it decides the config:** whether Alpaca counts UNIQUE
SYMBOLS or SUBSCRIPTIONS. If unique symbols, `use_trade_ticks_for_entry` is free
and 30 symbols can stream. Next session's log answers it - if 15 symbols
subscribe with no 405, try raising the cap and watch for the error to return.

**Judgement call to make with that answer:** 15 symbols with tick precision, or
30 with bar-close only? The measured prize was ~$116/day of entry slippage on
2026-08-20, and most of that is the 15-minute REST delay rather than
sub-bar timing - which argues for BREADTH (30 bars-only) over precision.

Original write-up below.

## 2b. Original: turn on the WebSocket stream

**Status:** `use_websocket_stream: true` and `rapid_increase` moved to 0.3%/3min
together, both live in config. Entry ticks (`use_trade_ticks_for_entry`) are on too.
What remains is the live verification described below - the connection has still
never succeeded from any host.

**Extra reason this matters, measured 2026-08-20:** 11 of 20 entries filled worse
than the price their signal fired at, costing $115.94 on a day that lost $239.00 -
roughly half the loss. That gap is the data lag priced in dollars. Entry slippage is
now logged per trade ("entry price corrected X -> Y (+Z% slippage)"), so the stream's
effect is directly measurable: expect average slippage to fall from ~0.3% to under
0.05%, the residual being the bid-ask spread which no data feed can remove. Within two minutes of
the open the log either shows `PriceStream connected` with bars flowing, or an
explicit ERROR saying it got zero bars and is falling back to REST.

**Unverified:** the live connection has never succeeded from any host. The dev
container got HTTP 403 at the WebSocket upgrade (before credentials are sent, so
network-level refusal, not entitlement) while REST returned 200 with the same
keys. The VPS is a different network and may be fine.

**Pair with it, same day:**
```yaml
rapid_increase_lookback_minutes: 3   # from 5
rapid_increase_pct: 0.3              # from 0.5 - MUST move together
```
Both, or neither. Shortening the window while holding the threshold demands a
*faster* move, which selects harder-running stocks — and the 2026-08-19 data says
that is backwards (signal >= 1.0%: 6 symbols, 1 winner, -$531; signal < 1.0%: 14
symbols, 4 winners, -$181). 0.5% over 5 min is roughly 0.3% over 3 min.

Shortening the window is also pointless while prices are REST-delayed ~15 minutes:
entry timing is dominated by feed lag, not window width.

---

## 3. Add a ceiling to the rapid-increase signal

Not a higher floor — a ceiling. Skip entries where the move has **already run
past ~1.5%**.

Evidence (2026-08-19 opening burst):

| Symbol | Signal | P&L    |
|--------|--------|--------|
| MRVL   | 2.04%  | -$144  |
| FCEL   | 1.71%  | -$118  |
| UPST   | 1.22%  | -$134  |
| OPEN   | 1.13%  | -$168  |
| NU     | 0.84%  | **+$279** |
| CMG    | 0.84%  | +$67   |

A stock up 2% in five minutes has already made the move; the -1% stop then sits
right where the natural pullback lands. Config as
`rapid_increase_max_pct` (0 = disabled).

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

## 5. Notifications still not working

Email has never delivered. DigitalOcean blocks outbound SMTP (587) — confirmed
`Network is unreachable` / `TimeoutError`. The daily report is saved to
`logs/reports/trading-report-YYYY-MM-DD.html` regardless, so nothing is lost, but
nothing is pushed either.

**Written up in full: `ops/NOTIFICATIONS.md`**, with a ready-to-send support
ticket in `ops/DO_SUPPORT_TICKET.md`. Short version: the ticket is worth filing
but is commonly declined, and it does nothing for texts; the fix that actually
works is to send over HTTPS instead of SMTP (Resend for email, Pushover for
push). Waiting on an API key — the code change is one pass once there is one.

Worth more than the daily report, which is already on disk: an alert when the
bot ISN'T RUNNING at 09:25 ET. Silence currently looks exactly like a healthy
morning.

---

## 6. Tuning values deliberately NOT changed

- `rapid_increase_pct: 0.5` — raising it is backwards, see item 3.
- `resistance_lookback_samples: 3` — superseded by `resistance_min_decline_pct`,
  which fixes the actual defect (no magnitude floor) rather than just requiring
  more consecutive ticks.

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

## 9. Sector confirmation — SHIPPED 2026-08-26. Market regime still open.

**Sector confirmation is built** (`src/analytics/sectors.py`), at ZERO weight in
`continuation_weights`. It is computed, journalled as `cf_sector_strength` and
`cf_sector_etf`, and decides nothing until the ledger shows it keeping its sign
across sessions.

What it answers: is capital flowing into the whole group, or is this a
single-stock event? A miner up 3% on a day the whole mining complex is up 3% has
shown nothing about itself — it scores 50 against its sector while scoring highly
against SPY. `relative_strength` cannot see that distinction; this can.

It also reports **watchlist concentration** at session start. On 2026-08-26 seven
of fourteen watched symbols mapped to the crypto complex (MARA RIOT CLSK CIFR
WULF COIN HOOD). That is one bet held seven times, arriving through the
watchlist rather than through a poll — the same correlation the burst throttle
exists to limit, at a layer the throttle never sees. Every one of those seven
lost money that session.

**The blocker was removed, not worked around.** This item used to be blocked on
the WebSocket budget: REST is ~15 minutes delayed on the free tier, so a sector
read over REST would answer "was the sector moving a quarter of an hour ago".
The resolution is that there was budget all along — `num_stocks_to_trade` is 15
against `stream_max_subscriptions` of 28. SPY and the day's sector ETFs are now
subscribed alongside the watchlist, LAST in priority so a benchmark can never
displace a tradeable name. A 14-symbol watchlist needs 6 benchmarks: 20 of 28.

### The finding that came out of this, which matters more

**SPY was never subscribed to the stream.** Only the watchlist was. So
`get_latest_bar("SPY")` always fell through to REST — the ~15-minute-delayed
path that `market_data.get_latest_bar`'s own docstring warns about.

Which means **every `excess_vs_spy_pct` ever recorded compared a LIVE symbol
move against a DELAYED market move**, and `cf_rel_strength` — weighted **+0.20**,
the joint-largest weight in the continuation score — is built directly on that
comparison.

On 2026-08-26 `cf_rel_strength` measured **rho -0.344** against 15-minute forward
returns: the opposite sign to its weight. A stale benchmark is a strong
candidate for why, though not a proven one — the alternative is that relative
strength genuinely mean-reverts at this horizon.

**This is now testable rather than arguable.** With SPY streamed, the next
sessions produce `excess_vs_spy_pct` computed from two live series. If
`cf_rel_strength` flips to a positive rho, staleness was the cause and the +0.20
weight was defensible all along. If it stays negative, the factor is genuinely
backwards and the weight must go. Either answer is worth having; neither was
reachable while the benchmark was minutes behind the thing it benchmarked.

**Market regime** (SPY/QQQ/IWM up, VIX down) is still not built. QQQ and IWM are
now cheap to add for the same reason the sector ETFs were — spare subscription
slots — but VIX is not a tradeable equity and does not arrive over this feed.

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

## flatten_all_positions sells shorts deeper (found 2026-08-28, NOT fixed)

`Executor.flatten_all_positions` calls `submit_exit_order`, which is hardcoded
to `side="sell"`. On a long that closes the position; on a short it doubles it.
The 16:00 time stop uses this path, so any short the bot is holding at 16:00
gets bigger instead of closing.

Not fixed on 2026-08-28 because the market was 60 minutes from opening and the
fix touches the live exit path on the day after the first winning session.
`ops/flatten-now.py` was moved to Alpaca's side-aware `close_all_positions`
instead, which covers the manual case.

The bot never intends to open a short, so this only bites on positions that got
there via the phantom-position path. That makes it low-frequency and expensive
when it hits - CRWD alone was -$605 unrealized.

Fix: give `submit_exit_order` the position sign, or have `flatten_all_positions`
call `broker.trading_client.close_position(symbol)` per symbol. Add a test with
a negative-qty position asserting the order side is BUY.

## reconcile_existing_positions drops the position sign (found 2026-08-28, NOT fixed)

`src/main.py`, in `reconcile_existing_positions`:

    qty = int(abs(float(position.qty)))

A short is adopted as a long of the same size. Every downstream rule then runs
backwards: on 2026-08-28 CRWD -39 @ 212.74 against a ~228 market was read as
+7.2% profit and would have fired a take-profit SELL, which shorts 39 more. And
because `submit_exit_order` cancels working orders for the symbol first, that
sell would have cancelled the buy-to-cover queued against it.

Not fixed on the day - the market was 50 minutes out and this is the startup
path for every position the bot holds. Worked around by flattening premarket
(`ops/flatten-now.py --premarket`) so nothing is left to adopt.

Fix: keep the sign, and either manage shorts properly or refuse to adopt them -
log loudly and leave them for a human. Refusing is probably right, since the bot
has no intent to be short and a position that got there is already evidence of a
different bug. Test: adopt a negative-qty position, assert it is not treated as
a long.


## What the premarket flatten actually cost (2026-08-28, resolved)

~$57 across all six positions, not the $1,800-2,000 feared at the time.

Equity, not cash, is the measure. Cash fell 107,725.18 -> 93,883.54, which is
short-covering (buying back CRWD/OKTA/MTCH spends ~$18,800; selling the three
longs returns ~$4,200) and says nothing about P&L. With no positions open,
equity is cash:

    cash before                                        107,725.18
    + longs   MSTR 135.03 + NVDA 1,818.80 + SOFI 2,267.78   +4,221.61
    - shorts  CRWD 8,906.04 + MTCH 164.01 + OKTA 8,936.35  -18,006.40
                                                      = 93,940.39
    equity after                                        93,883.54
                                                      = -56.85

The scare came from reading the IEX ask (CRWD 237.26 against a 228.36 mark) as
the price we would pay. Wrong twice. IEX is ~2% of consolidated volume, so its
premarket book is thin and its quotes stale - fine for the sanity check the
quote is used for, useless as a price prediction. And a marketable LIMIT fills
at the best available offer, not at the limit: 242.01 was a ceiling, not a cost.

Keep the 2% padding. It bought certainty of fill for roughly $57.
