# Pending work

Open items, most actionable first. Each has enough context to pick up cold.

---

## 0. **MONDAY: decide whether to KEEP or REMOVE the take-profit** (2026-08-24)

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

## 1a. MONDAY 2026-08-24: poll interval + time-based windows

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

## 2. ~~Turn on the WebSocket stream~~ — ENABLED for 2026-08-21, NOT YET VERIFIED

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

Not in the repo — they live in the session scratchpad and will be lost when the
container is reclaimed. **284 cases as of `55a6dd9`**, covering: broker-lag position
reconciliation, three-bar acceleration, report save/retention, WebSocket
routing/fallback/watchdog, burst throttle, signal journal, take-profit scale-out,
the earnings/QQQ list builder, and an A–Z pass against the real `config.yaml`.

Two harness traps worth knowing before re-running them: any suite that calls
`check_exit` must pin `strategy._now_et`, or a run after 16:00 ET returns
`TIME_STOP_4PM` for every position and masks the rule under test; and a mock
broker must *decrement* on a sell rather than dropping the symbol, or every
partial exit looks like a stale-position leak. Worth moving into `tests/` if any of this is to be
maintained.
