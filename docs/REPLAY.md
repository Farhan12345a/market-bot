# Exit-rule replay: finding the right stop, breakeven and take-profit

How to answer *"what stop loss and take-profit percentages should this bot
actually use?"* from recorded data instead of from intuition or from running
one setting per week.

---

## Why not just test one config per week?

Because week-over-week comparison measures the weather, not the config.

| Session | P&L | Config |
|---|---|---|
| 2026-08-27 | **+$534** | ~unchanged |
| 2026-08-31 | **−$546** | ~unchanged |

That is a ~$1,080 swing from the market alone, on essentially the same
settings. A breakeven moving from +0.15% to +0.30% is worth maybe a few
dollars a trade. Run config A in a bull week and config B in a chop week and
you will conclude A won — and you will have measured the tape.

Two more problems with sequential live testing:

- **Sample size.** At ~20–40 trades/week and a per-trade spread of ~$70, the
  standard error on a weekly mean is ~$13. The 95% interval spans ±$25/trade.
  Most stop tweaks are smaller than that and are therefore invisible.
- **Combinatorics.** Stop × breakeven × take-profit at 4 values each is 64
  cells. One week per cell is **15 months**, by which point the early results
  are stale.

Replay fixes all three: every config is scored against the **same trades**,
so there is no regime confounding, and 64 configs take seconds rather than
15 months.

---

## The data this depends on

Two files, both written live by the bot (`src/analytics/trade_recorder.py`):

### `logs/trade_context.csv` — one row per trade

| Group | Columns |
|---|---|
| Identity | `trade_id`, `date` |
| Entry | `symbol`, `entry_time`, `entry_price`, `position_size`, `entry_method`, `size_multiplier` |
| Market | `spy_return`, `qqq_return`, `spy_vs_vwap`, `qqq_vs_vwap`, `market_breadth`, `regime` |
| Stock | `stock_vs_vwap`, `relative_volume`, `momentum`, `continuation_score`, `sector_strength`, `spread_pct` |
| Outcome | `mfe_pct`, `mae_pct`, `exit_time`, `exit_price`, `exit_reason`, `realized_pnl`, `realized_pnl_pct` |

Market and stock columns are captured **at the entry instant**. They are not
reconstructable afterwards — VWAP position, breadth and the regime label are
session state that no later bar fetch rebuilds faithfully.

A reading that could not be taken is written **blank, never zero**. These
columns become filter conditions ("SPY above VWAP AND rvol > 3x") and a false
zero silently moves trades into the wrong bucket.

### `logs/trade_paths.csv` — the price path, entry to exit

`trade_id`, `symbol`, `date`, `timestamp`, `price`, `gain_pct` — one row per
open position per poll (~every 10s).

**This is the file that makes the whole thing possible**, and nothing before
2026-09-02 recorded it.

`mfe_pct` and `mae_pct` record the best and worst excursions but **not which
came first**. These two trades produce identical rows:

```
peaked +1.2%, then dipped -0.4%    ->  mfe +1.2, mae -0.4
dipped -0.4%, then peaked +1.2%    ->  mfe +1.2, mae -0.4
```

Under a −0.5% stop the first is a winner and the second is a loser. Every
stop question is a question about **ordering**, including the central one:

> "What is the probability of reaching +1% **before** −0.5%?"

That is unanswerable from extremes. With the path it is a direct count.

---

## The tools

### `ops/replay.py` — score one config

```bash
python3 ops/replay.py                                  # the live config
python3 ops/replay.py --stop -0.5 --be 0.5/0.15
python3 ops/replay.py --tp 0.75:0.4,1.0:0.3,1.25:1.0 --trail 0.4
python3 ops/replay.py --since 2026-09-03 --json
```

Walks each trade's real path in time order, applies the rules, and reports
total, mean/trade, win rate, a 95% interval, and how it compares to what
actually happened.

**Rule order at each sample** mirrors the live strategy: protective exits
(hard stop, breakeven floor, trailing) are evaluated **before** profit
taking. A sample that trades through both a stop and a tier is a sample that
went against the position. The opposite order would let the replay bank
profits the live bot would not have taken — the easiest way to make a
backtest lie.

### `ops/grid.py` — sweep combinations

```bash
python3 ops/grid.py
python3 ops/grid.py --bes=0.5/0.05,0.5/0.15,0.5/0.30,0.75/0.15,none
python3 ops/grid.py --stops=-0.4,-0.5,-0.75      # note the '=' - see below
python3 ops/grid.py --by-regime
```

**Attach values that start with a minus sign using `=`** (`--stops=-0.4,-0.5`).
Written apart, argparse reads the leading `-` as another flag and refuses.

Deliberately **bad at naming a winner and good at showing the region**.

---

## How to read the output — this is the important part

### Read the marginal tables first

```
--- STOP (mean $/trade, averaged over all other settings) ---
  -0.5                         $+18.40  (20 combos) ####  <- best
  -0.4                         $+16.10  (20 combos) ###
  -0.75                        $+11.20  (20 combos) #
  -1.0                          $+4.30  (20 combos)
```

This asks *"is this stop good across the board?"* — a far more robust
question than *"is this exact triple the best?"*, and it needs much less data
to answer.

**A smooth plateau is real signal. An isolated spike is noise.** If −0.4,
−0.5 and −0.6 are all decent and −0.9 is clearly worse, that region is
trustworthy long before any individual cell reaches significance. If one cell
is excellent and its neighbours are terrible, that is a lucky draw, not a
discovery.

### Read the leaderboard second, if at all

Every cell carries its 95% interval, and the tool states plainly how many
configs are statistically indistinguishable from the top one:

```
VERDICT: 47 of 80 configs are NOT distinguishable from the top one at n=62 -
their intervals all overlap it. Do NOT pick the top row.
```

That message is the tool working correctly. A grid that names a champion at
n=30 is actively harmful.

### How much data is enough?

| Trades | What you can honestly conclude |
|---|---|
| < 50 | Nothing about individual cells. Marginal tables are a weak hint. |
| 50–200 | Plateau regions become readable. Still no single winner. |
| 200–400 | Real differences of ~$10/trade start to separate. |
| 400+ | A specific config can be defended. |

With 64 cells and 30 trades, the best-looking cell sits roughly 2.5 standard
errors above the middle **by chance alone** — about $32/trade of illusion,
against real effects of maybe $5–15. That is why the threshold is high.

At typical volume this is **3–6 active weeks**, not a few sessions. The daily
report prints the running count and what it currently supports.

---

## What replay cannot tell you

Stated plainly so the numbers are not over-trusted.

1. **Fills.** A counterfactual exit is scored at the recorded price. A real
   one crosses a spread and takes slippage. Rankings are unaffected; absolute
   dollar figures are optimistic.
2. **Knock-on effects.** A tighter stop frees a position slot and starts the
   re-entry cooldown earlier, changing which *later* trades exist at all.
   Replay holds the entry set fixed.
3. **Path resolution.** Paths are sampled every ~10s. A level crossed between
   samples fires at the next sampled price, so a single trade's replayed exit
   can be off by up to one sample.
4. **Entry rules.** Only exits are replayable. A new entry signal (pullbacks,
   a different momentum threshold) changes which trades exist and needs a
   live session. This is why entry experiments and exit tuning should not run
   in the same week.

---

## Expect the answer to be regime-dependent

The ideal stop in a trending tape is probably not the ideal stop in chop.
`--by-regime` splits the whole grid by the `regime` recorded at entry.

If the answer differs by regime, that is not a failure of the method — it is
a finding, and it argues for conditioning stops on regime rather than hunting
one global number. Check it before settling on a single config.

---

## Working example

Answering *"when continuation_score ≥ 85, SPY above VWAP, rvol > 3x, and the
stock is +0.4–0.8%, what fraction reach +1% before −0.5%?"*:

1. Filter `trade_context.csv` on those four columns.
2. Take the matching `trade_id`s.
3. For each, walk `trade_paths.csv` in timestamp order and record which of
   +1.0% / −0.5% is crossed first.
4. The ratio is the answer — from real ordering, not inferred from extremes.
