# Pending work

Open items, most actionable first. Each has enough context to pick up cold.

---

## 1. Pre-market gap is always 0.0% — regression from moving the screener earlier

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

## 2. Turn on the WebSocket stream, and pair it with a shorter lookback

Currently `use_websocket_stream: false` — shipped off deliberately so its first
session wouldn't be confounded with the other fifteen commits.

**To enable:** flip to `true`. Nothing else needs changing. Within two minutes of
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

Options: Pushover (user has used it before; needs a User Key and a new
application API token), or an HTTPS email API (Resend/SendGrid/Mailgun) which
sidesteps the port block entirely since it's just HTTPS.

---

## 6. Tuning values deliberately NOT changed

- `rapid_increase_pct: 0.5` — raising it is backwards, see item 3.
- `resistance_lookback_samples: 3` — superseded by `resistance_min_decline_pct`,
  which fixes the actual defect (no magnitude floor) rather than just requiring
  more consecutive ticks.

---

## Test suites

Not in the repo — they live in the session scratchpad and will be lost when the
container is reclaimed. 176 cases as of `1487f3b`, covering: broker-lag position
reconciliation, three-bar acceleration, report save/retention, WebSocket
routing/fallback/watchdog, burst throttle, signal journal, and an A–Z pass against
the real `config.yaml`. Worth moving into `tests/` if any of this is to be
maintained.
