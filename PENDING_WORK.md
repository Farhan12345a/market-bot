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
week of trades since this change exists. Only 2026-09-03 and 09-04 have traded
under it so far (weekend + Labor Day between then and now) - not yet a full week.

**Window started 2026-09-08**: Opening-Move Experiment scale-up, tracked
together as ONE variable - "opening burst breadth/size":
  - `opening_burst.max_positions: 7 -> 14` (the practical ceiling - matches
    `stream_max_subscriptions`, so every slot always has a live baseline price)
  - `opening_burst.size_multiplier: 0.5 -> 0.6` (a deliberate small step, not
    the 1.0 originally considered - see the comment in config.yaml)
  - Code change alongside it: `Executor.pre_entry_check` now exempts
    `is_opening_burst` entries from `max_concurrent_positions` (10), the same
    way `rate_limits.exempt_opening_burst` already exempts them from the
    per-minute caps - otherwise raising `max_positions` past 10 would have
    been silently capped at 10 by that check. Accepted consequence: a full
    burst can leave the normal session with ZERO new entries until enough
    burst positions close.
  - **Overlaps the still-running 2026-09-03 universe-price-band window**,
    which only has 2 clean trading days on it. Both are now changing at once
    from 2026-09-09 - flagged to the user 2026-09-08, proceeding at their
    explicit direction. Reading either window's `ops/session-metrics.py`
    result cleanly will need to account for the other one changing
    mid-stream, not just compare to "before 09-03".

**Same day, 2026-09-08, a THIRD variable added to the same window at the
user's explicit direction** (originally going to be held out - see prior
draft of this note - but the user chose to widen the pool now rather than
wait): `num_stocks_to_trade: 15 -> 20` + `stream_max_subscriptions: 14 -> 20 -> 22 -> 25`
(walked up twice more the same day at the user's direction), tracked together
as ONE variable - "traded/streamed pool widening" (Tier 5). Raised together
deliberately: `num_stocks_to_trade` alone would have added names the
streamed_only opening burst can never see (they'd just sit on REST), per
config.yaml's own dynamic-universe warning. Also fixes a latent shortfall
found while checking this: `stream_reserve_index_slots` takes 2 of the stream
budget for SPY/QQQ, so the OLD 14-symbol cap only ever streamed 12 tradeable
names live - already short of the opening burst's new 14-position budget
before this change. `num_stocks_to_trade` was deliberately NOT walked up
alongside the later 22 and 25 steps - past 20, `stream_max_subscriptions` is
now also its own live-boundary experiment (config.yaml's "TO TEST HIGHER"
plan: 20, then 25, then 29), decoupled from watchlist sizing. 25 - 2 reserved
= 23, still above num_stocks_to_trade (20) with room - every traded name
streams live, none fall to REST. Live
boundary is still unconfirmed above 14 subscribed symbols (see the comment on
`stream_max_subscriptions` in config.yaml) - watch the log at 09:26 on
2026-09-09; a bad guess fails loud and is caught by `_reduce_and_retry`
(tested generically in `tests/test_wsfail.py`, independent of the configured
cap), not by losing the session.

So as of 2026-09-08, THREE entry variables are moving at once across two
overlapping windows: universe price band (09-03), opening-burst breadth/size
(09-08), and pool widening (09-08). This is a deliberate, acknowledged
departure from "one at a time," made at the user's explicit direction because
they consider the Opening-Move Experiment the core bet of the project.
`ops/session-metrics.py` results from 2026-09-09 onward will need to account
for all three moving together, not be read as isolated single-variable tests.

**`num_stocks_to_trade` reverted 20 -> 15 on 2026-09-10**, closing that leg of
the "pool widening" window - the 2026-09-09 session's selection edge was
judged worse under the wider pool (skipped signals outperformed taken ones).
`stream_max_subscriptions` stayed at 25 (a data-coverage cap, not a selection
variable). See config.yaml's comment on `num_stocks_to_trade` for the full
account.

**Window RESTARTED 2026-09-17** (originally started 2026-09-14):
`opening_burst.min_move_to_spread_ratio: 2.0 -> 1.5`, run ALONE -
`min_move_pct` deliberately held at 0.3% so any change in opening-burst fill
rate/edge can be attributed to the spread gate alone. Motivated by
2026-09-11: AAPL's move peaked at +0.469% against a required 2x-spread
threshold that fluctuated 0.48%-1.40%, missing by as little as 0.011pp at
the closest check, while continuing to +2% after the window closed.

**Why restarted rather than left to finish on schedule (would have been
2026-09-21):** the account moved from the free IEX feed to Algo Trader Plus
(SIP, all US exchanges) on 2026-09-17, mid-window. The spread readings this
gate acts on come from a materially different, denser data source from that
date forward - 09-14 through 09-16 measured spreads on IEX, everything from
09-17 on measures them on SIP. Letting the original window run to 09-21 would
have compared a ratio's effect across two different quote qualities inside
one supposedly single-variable test. This setting itself did NOT change on
09-17 - still 1.5 - only the tracked start date did, so the days already
banked under 1.5 are not lost, just not counted toward this specific
comparison. `min_move_pct` is still held for now; user's explicit direction:
revisit it as a SEPARATE second variable once THIS window's results are in
(now ~2026-09-24), and hold `multifactor_rank` (item 11b) until this window
closes too, so nothing stacks.

---

## Idea: some form of concentration control inside the Opening-Move Experiment

Raised 2026-09-08, NOT implemented - a note for later consideration, not a plan.

The opening burst is explicitly BREADTH-first and explicitly exempt from
throttling (CLAUDE.md: "nothing that throttles simultaneous signals may be
applied here"). That was a correct call when the budget was 7 names from a
15-name pool. With the budget now 14 and the pool now 20, and the burst
ranking "biggest mover first" with no correlation awareness, a single
correlated sector move (the NOW/CRM/WDAY-on-XLK shape from 2026-09-02, which
is exactly what the NORMAL-window burst throttle exists to catch) could now
fill a much larger fraction of the 14-position budget with one bet wearing
several tickers, inside a three-minute window, before any of them has a
chance to prove itself out.

Two things already exist that partially cover this without contradicting
"no throttle": `max_positions_per_sector` already applies to burst entries
(they go through the same `pre_entry_check` gate - see test_guards.py
section 6) - so a full sector sweep IS already capped, just not below the
generic Tier-3 sector limit that also governs the rest of the day.

Worth thinking about, not worth building yet: a burst-specific, TIGHTER
sector cap (e.g. `opening_burst.max_positions_per_sector`, separate from the
session-wide one) so the mode keeps its "take everyone that qualifies"
intent for genuinely independent movers, while still capping how much of the
14-slot budget one correlated group can claim. This is deliberately NOT the
same shape as the normal-window `_burst_policy` throttle (which cuts COUNT
and SIZE together on ANY simultaneous cluster) - it would target only
sector correlation, which the mode does not yet protect against beyond what
the session-wide cap already gives it for free. Revisit once there is a week
of trades under the 14-position budget to see whether this actually happens,
rather than guessing at a threshold now.

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


## A real answer for bearish tape — option 1 SHIPPED 2026-09-27, ENABLED same day

`breadth_halt` (added 2026-08-30, later superseded by `regime_sizing`) stops new
LONG entries in a weak tape. That was always damage control, not a way to
profit - the deeper point from 2026-08-28 stands: `edge` was +1.03pp on both a
winning and a losing session, so selection adds value in both regimes, and
what was missing was something to be long OF on a day with no upside in it.

Option 1 from this note ("short the weak side") is now built:
`trading.short_strategy`, a config-gated mirror of the long side (same entry
threshold, sizing and risk limits, exit shape inverted) - see strategy.py's
`TradeManager.direction`, executor.py's `submit_entry_order(side=...)`/
`retry_unfilled_entries`/`close_orphaned_position`, and main.py's
`_short_regime_multiplier`/`_attempt_entry(side="short")`. The sign bugs this
note flagged as a prerequisite were fixed 2026-09-02 (test_signs.py) well
before this was built on top of them.

The `no_shorting` account setting this note predicted would need to be
"consciously undone" is exactly what main() now checks at startup
(`account.shorting_enabled`) and warns loudly about if short_strategy is
enabled in config but the Alpaca account itself still blocks it - that
account-level toggle is OUTSIDE this repo and has to be checked/changed
directly in the Alpaca account configuration; the code side is done but is not
sufficient by itself.

**HARD RULE, explicit user instruction 2026-09-27:** longs and shorts must
NEVER be active in the same regime window - enforced unconditionally in
run_trading_day (short_regime_size_multiplier forced to 0 whenever the long
multiplier is nonzero), not left to the two multiplier tables happening to
agree. In practice this means shorts only ever fire in a confirmed BEARISH
read; bullish/neutral/choppy are long-only exactly as before.

**Reported entirely separately from the tracked primary (long) P&L** - its own
report section (`_short_strategy_html`), excluded from the headline Total
P&L/win-rate/trade-count and from the push-notification summary, mirroring
how the extended-hours experiment (below) is also kept out of that same
headline. Explicit user instruction: "not accounted for for the actual P&L
that we are tracking which is in the primary long taking window."

**STILL OPEN, now that the mechanism exists:**
- Only 2 days of short-side observational evidence exist as of 2026-09-27
  (short_signal_journal.csv, started 2026-09-24) - both showed down-momentum
  continuing more often than reversing, but that is nowhere near the ~2-week
  bar this file's own gate sets elsewhere. The user's own call was to enable
  the mechanism now anyway, specifically BECAUSE it is walled off from the
  tracked P&L while evidence keeps accumulating in parallel - revisit whether
  the entry threshold/exit shape (a straight mirror of the long side, not
  independently tuned) actually fits short-side behavior once real trade data
  exists.
- No symmetric "provisional" short-side regime gate exists yet for the
  opening minutes (unlike the long side's bearish-only burst-close read) -
  harmless today because the hard mutual-exclusion rule above already closes
  the one gap this left (both sides defaulting to 1.0/"no opinion" before
  check_time), but worth a dedicated provisional short gate if opening-minute
  short entries turn out to matter.
- Options 2 (inverse ETF long) and 3 (breadth as an entry condition) were not
  pursued - option 1 was the direction chosen.

**2026-09-29 UPDATE - the 2026-09-28 fix was NECESSARY BUT NOT SUFFICIENT.
ORPHAN_RECONCILE recurred WORSE, not better, the day after shipping it. Real
root cause identified and FIXED the same day - see below.**

2026-09-29: 27 ORPHAN_RECONCILE exits (up from 15 on 09-28), netting
**-$567.84**, against +$211.12 from every other short exit that day - the
short strategy's real edge (+$211) is being more than eaten alive by this
one mechanism (net day: -$356.72 across 90 short tranches / 36 positions).
19 distinct symbols hit it, several (COIN, MXL, IONQ) hit it TWICE in the
same day, growing each time (COIN: 36 shares orphaned at 13:45, then a FRESH
18-share entry at 14:17, then 72 shares orphaned at 14:36).

**Actual root cause, traced through today's service-log.txt (COIN as the
worked example, same shape confirmed on MXL/IONQ/others):**

09-28's fix (re-check broker position right after cancel, before the forced
retry) is CORRECT but only closes the millisecond-scale race at that one
instant. It does not help when a forced-retry order fills SLOWLY, in many
small pieces, over 30-90+ seconds - which is exactly what these particular
symbols do (they are the SAME symbols the opening-burst fill-rate check
already knows are slow/illiquid: AAON, BRKR, CDNS, DDOG, FORM, GRAL, MRNA,
P, TEM, TWST, VIAV, VICR all appear on BOTH today's "never filled" opening-
burst list AND today's ORPHAN_RECONCILE list). Today's COIN trace: entry
confirmed 18 shares at 13:33:11, retried at 13:33:24 (my fix's re-check
correctly found 0 held at that instant, so no race caught, forced retry
correctly submitted) - then SIX separate "entry price corrected" events
between 13:33:30 and 13:35:08 as the retry order filled in pieces.

**The actual gap - a QUANTITY reconciliation mechanism already existed and
was already correctly wired (executor.py's `refresh_account_snapshot` ->
`on_entry_qty_corrected` -> strategy.py's `correct_entry_qty`, built
2026-08-24 for a LONG partial-fill case, HOOD), but had two long-only/
shrink-only assumptions baked in from before shorts existed:**

1. `refresh_account_snapshot` (executor.py ~line 363, pre-fix) skipped the
   reconciliation call entirely whenever `held <= 0` - a negative broker qty
   (any short) was read as "something is wrong, don't touch it" and never
   even reached `correct_entry_qty`. Shorts got ZERO quantity reconciliation,
   ever.
2. `correct_entry_qty` (strategy.py, pre-fix) explicitly refused to GROW the
   tracked qty at all - "a broker count HIGHER than tracked usually means an
   exit has been submitted but not yet settled, and trusting that number
   would resurrect shares the strategy has already sold." True for the exit-
   in-flight case this was built for, but exactly backwards for a slow
   multi-tranche ENTRY that lands more shares than intended before any exit
   has fired - which is what actually happened to COIN/MXL/IONQ/etc today.

FIRST_EXIT/TRAILING_STOP compute their sell qty from the STRATEGY's tracked
qty, so with quantity reconciliation silently disabled for shorts, a "full"
exit sold only the originally-intended amount and left the real excess held
at the broker, genuinely unprotected. Once the strategy believed the symbol
was flat, a LATER signal on the same symbol opened a fresh, unrelated
position on top of the still-real leftover - compounding until the periodic
reconcile noticed the mismatch and force-closed it via ORPHAN_RECONCILE,
correctly, as a safety net. The safety net firing 27 times in one day was
a symptom, not the disease.

**FIXED 2026-09-29 (executor.py's `refresh_account_snapshot`, strategy.py's
`correct_entry_qty`):**
- The `held <= 0: continue` skip is now `held == 0: continue` - only a
  truly flat broker reading is skipped; shorts get reconciled exactly like
  longs.
- Added a defer-not-refuse guard: a symbol with an exit still pending
  (`self._pending_exit_verify`) is skipped for THIS poll only, since the
  broker's count is genuinely ambiguous in that exact window (a sell that
  hasn't settled yet looks identical to a position that grew). This is
  where the "exit may be in flight" protection now actually lives, instead
  of `correct_entry_qty` refusing to grow unconditionally forever.
- `correct_entry_qty` now reconciles on MAGNITUDE (`abs(actual_qty)`) in
  BOTH directions - growing and shrinking both flow through the same
  proportional-scaling logic that previously only ran on shrink. A new
  guard refuses the correction outright (logged as an error, left for the
  orphan/reconcile path) when the broker's SIGN disagrees with the
  position's own tracked direction - that is a real long/short mismatch,
  not ordinary fill drift, and this function should not paper over it.

New tests: test_safety.py sections B2-B4 and C2 (both directions for both
long and short, the sign-mismatch refusal, the exit-in-flight deferral
working end to end via a real `refresh_account_snapshot` call against a
fake broker, and a direct reproduction of the COIN shape proving a full
exit now sells the TRUE corrected quantity instead of leaving a remainder).
Full suite green (2802 pass, 0 fail).

**2026-09-30, final review pass before deploy - ONE MORE real gap found and
fixed.** Explicit user request to look through the logic one more time
before giving a greenlight. Re-checked the qty-correction loop's own
`ENTRY_CONFIRM_GRACE_SECONDS` gate (inherited unchanged from the original
2026-08-24 mechanism, not something introduced by the 09-29 fix) against
2026-09-29's actual COIN trace: the broker already showed -23 held (vs 18
tracked) by 13:35:08 - only 117 seconds after the 13:33:11 entry, still
inside the 120s grace window. A FULL exit (GAP_EXIT, FINAL_EXIT) firing
that early would have sold only the stale tracked amount regardless of the
09-29 fix, because qty correction would still have been sitting out the
clock - the fix only helped COIN specifically because its full close
(TRAILING_STOP) happened to fire at 13:39:02, well after grace expired.

**Root cause of the gate being wrong for this loop:** `ENTRY_CONFIRM_GRACE_
SECONDS` exists for a genuinely different problem - a symbol this bot
tracks as open but that is still ABSENT from the broker's position list
entirely (the earlier `unconfirmed` logic in the same function, keyed off
`self._open_symbols - broker_symbols`). The qty-correction loop only ever
runs over `positions.items()` - symbols the broker is ALREADY reporting a
real quantity for - so "the list is lagging" does not apply to it at all;
the reported qty is real the instant it is reported. The grace check on
this specific loop was inherited/misapplied from the nearby-but-distinct
concern, not a deliberate design choice for the qty case.

**FIXED:** removed the `ENTRY_CONFIRM_GRACE_SECONDS` check from the qty-
correction loop specifically. The earlier `unconfirmed`-symbol grace check
(the one it actually protects) is untouched. New test: test_safety.py C3,
reproducing the exact gap - a symbol entered 0 seconds ago now still
reconciles its qty immediately rather than waiting up to 120s. Verified
with 2 consecutive fresh full-suite runs after this change: 2814 pass, 0
fail both times.

**2026-09-30 follow-on, explicit user request: "everything needs to be
replicated... that we had for the shorts, like we had for the longs. The
same issues cant continuously be coming up." Audited the rest of the
codebase for the same class of stale long-only assumption. Found and fixed
two more, both real:**

1. **`reconcile_against_broker` (executor.py) had no way to tell a
   legitimately-tracked short from an unexpected one.** It only ever
   received symbol NAMES, not sides, so ANY negative broker qty on a
   tracked symbol read as `"SHORT position of X - this bot never opens
   shorts"` - true before short_strategy existed, a FALSE ALARM on every
   single reconcile poll a legitimate short was open once it went live.
   This fired a real `_AL.degraded()` alert each time the set of open
   shorts changed, and (traced directly from 2026-09-29's own logs) is why
   COIN appeared to flip between "not tracking it" and "never opens
   shorts" messages across consecutive polls - neither the genuine
   untracked-orphan case nor the healthy-tracked-short case were being
   told apart. Fixed by adding a `short_symbols` parameter (the set of
   symbols the caller tracks as short, from `TradeManager.direction`) -
   a short matching what it's tracked as now reports nothing, and the
   TRUE remaining mismatch case (tracked as one side, broker shows the
   other - a real sign flip) is now caught for the first time instead of
   silently passing through unreported.
2. **`flatten_all_positions` (executor.py, 16:00 time stop) logged an
   ERROR every time it closed ANY short, unconditionally** - "The bot
   never opens shorts deliberately, so this position is evidence of a
   separate bug." True when short_strategy is off; alarmist and wrong once
   it's a deliberate feature, since a short still open at the time stop is
   completely ordinary end-of-day behavior. Now checks
   `short_strategy.enabled` and logs at INFO with no bug-claim when it's
   on, preserving the original ERROR + bug-claim exactly as before when
   it's off (the still-real "how did this position exist at all" question
   for that case).

New tests: test_risk_tier2.py R4b (reconcile_against_broker's
short_symbols parameter, both directions of mismatch, the false-positive
fix verified directly) and test_signs.py section 10 (flatten_all_positions'
log level/message verified by capturing actual logger calls, both
short_strategy on and off). Full suite green (2809 pass, 0 fail).

Explicitly checked and found NOT to need changes (audited, not assumed):
`Strategy.can_enter`'s `qty > 0` (qty here is always a positive share
count regardless of side, side is a separate parameter - not a sign
check), `check_exit`'s per-rule `qty > 0` (every exit rule already returns
a direction-agnostic positive magnitude via `TradeManager.direction`
internally), `_attempt_entry`'s `qty <= 0` sizing gate (already side-aware,
checks `short_regime_size_multiplier` vs `regime_size_multiplier` by
`side`), and `reconcile_existing_positions`' qty checks (already computes
`side = "short" if raw_qty < 0 else "long"` correctly).

**2026-09-28 — first live day, two bugs found, FIXED 2026-09-29 (see the
2026-09-29 update above - INCOMPLETE, the same class of bug recurred worse):**

Net +$225.02, 18 positions/58 tranches, 14W/4L, all short (regime was bearish
09:34-13:05 ET, so the mutual-exclusion rule correctly kept longs off all
day). Full breakdown given to the user in conversation and via the daily
chart artifact. Two real problems surfaced:

1. **Entry-retry cancel race creates untracked, double-filled positions.**
   `retry_unfilled_entries` (executor.py ~line 1037) cancels the original
   marketable-limit order and immediately submits a forced wide-limit retry
   without confirming the cancel actually landed before the original could
   fill. On 15 of today's ~22 short entries (COIN, MRNA, MXL, RKLB, RMBS,
   VIAV, VICR, VKTX, ALAB, DDOG, BE, GRAL, P, QCOM, AVGO) this raced: the
   "cancelled" original order filled anyway, on top of the retry, leaving a
   same-size second position the strategy layer never tracked at all - no
   stop, no trailing, no breakeven, nothing watching it - until the periodic
   `reconcile_against_broker` found it (sometimes 5+ minutes later) and
   closed it via `ORPHAN_RECONCILE` at whatever price was showing. Confirmed
   from service-log.txt: e.g. COIN entered 18 short at 09:35:40, retry fired
   at 09:35:52 ("cancelled working order c3eaaedf (0/18 filled)"), the
   strategy's own tracked 18 shares fully exited normally by 09:38:05, and
   RECONCILE at 09:40:28 still found the broker holding **-36** (double) with
   the bot "not tracking it" - closed at 09:45:33, -$102.21. These 15
   ORPHAN_RECONCILE exits netted **-$155.58** against the day's +$225.02 - the
   day was profitable in spite of this, not because it wasn't happening. This
   is a structural risk-management gap (positions with zero live stop-loss
   for minutes at a time), not a P&L curiosity.

   **FIX (executor.py's `retry_unfilled_entries`, ~line 1032):** after
   calling `cancel_open_orders`, re-fetch the broker's live position for the
   symbol before ever submitting the forced retry. If it already shows a
   fill matching the intended direction (`expect_sign * qty > 0`), the
   "cancelled" order won the race - re-arm tracking (`retried=True`, fresh
   `ts`) instead of submitting a second order, and let the ordinary
   held>0 path on the next poll confirm and clear it, exactly like any other
   fill. Does not fully eliminate the race in theory (Alpaca's cancel is not
   synchronous), but closes the window this incident actually walked
   through. Covered by tests/test_phantom_exit.py sections 12b-12e (race on
   both long/short, and a non-regression check that a genuinely still-
   unfilled entry still gets its forced retry).
2. **`trade_paths.csv`'s `gain_pct` is not direction-aware.** main.py's
   per-poll path sample (~line 3359) computed `(price-entry)/entry`
   unconditionally - correct for longs, inverted for shorts. Doesn't affect
   real trading (actual exit P&L is computed correctly elsewhere via
   `TradeManager.direction`), but it corrupted the daily chart artifact's
   per-position path line for every short on 2026-09-28.

   **FIX:** multiply by `TradeManager.direction` (already available via
   `strategy.trades.get(symbol)` at the sample site). One-line change, no
   effect on any trading decision - this field is analytics-only. Also
   corrected in `daily_viz/build_data.py` for the already-published
   2026-09-28 chart (that script isn't part of this repo, so it needed its
   own fix regardless of the live-bot one).

Also fixed, same day: `max_daily_entries` was ONE counter shared by opening
burst + normal longs + shorts, charged at order **submission**, not fill. On
2026-09-28 it capped out at 09:45:45 ET - 28 of the 50 slots went to
opening-burst/rapid-increase entries that never filled at all, starving the
rest of the session (including the whole extended-hours window) of slots
that would have gone to real trades. **FIX:** `entries_triggered` is now
refunded (`max(0, entries_triggered - 1)`) whenever a submitted entry is
phantom-dropped - either via the exit-side `PHANTOM_EXIT` guard or via
`retry_unfilled_entries`'s own abandon path - so the budget only ever counts
entries that actually became positions. `max_daily_entries` raised 50 -> 200
alongside this (config.yaml) since it's no longer sized for the old
count-everything-including-phantoms model; 200 is a runaway-loop backstop,
not a real constraint given max_concurrent_positions/correlation_limit/
reentry_cooldown/rate_limits already bound real throughput far below it.

**2026-09-29 follow-on, same conversation - extended-hours rate + quality:**

Two more explicit user requests, both shipped:

- `trading.extended_hours_experiment.max_entries_per_hour: 20` - a SEPARATE
  cap from `max_daily_entries`, scoped to `entry_window_label == "extended"`
  only. Tracked in `extended_hourly_state` (resets on the wall-clock hour via
  `_extended_hourly_due`), refunded on phantom drop the same way
  `entries_triggered` is. `extended_entries_today` is the cumulative,
  never-reset day total, logged in the "Daily session complete" line - this
  is "the number to keep watching" per the user's instruction. **Started at
  20/hour** - the busiest FULL DAY on record is ~37 fills across 6.5 hours,
  so this is meant to bite only during genuine clusters, not ordinary
  pacing. Report back after a real day of extended-hours data (2026-09-28
  had zero, due to the bug above) before deciding whether to move it.
- `short_candidates` now ranked best-first via `_rank_burst` (the SAME
  continuation-score mechanism the long side's burst throttle already used)
  before the entry decision loop - previously the short side had NO ranking
  at all. Combined with the hourly cap: when a scarce hour's remaining slots
  can't cover everyone in a poll, the best-scored candidates get attempted
  first, so quality decides who gets the last slots rather than list order.
  Deliberately RANKING, not a hard minimum-score gate - see `_rank_burst`'s
  own docstring for why (a floor would remove trades on the strength of
  weights that are still one day of evidence; ranking only reorders a cut
  that would have been arbitrary anyway).
- Explicitly NOT done: a live minimum-`cf_score` entry filter for extended
  hours. That changes WHICH signals convert to trades (Tier 1/2), and
  CLAUDE.md's entry-change protocol wants its own held measurement window
  before something like that ships, not a same-day bolt-on alongside the
  rate cap - bundling both would make it impossible to tell which one moved
  tomorrow's numbers.

Regime note for reading tomorrow's extended-hours sample: the mutual-
exclusion rule means a regime flip mid-afternoon (needs 6 consecutive
confirming reads at the 300s afternoon cadence = 30 minutes minimum once
past 10:00 ET, vs. as fast as ~18-20 seconds during the 09:30-09:40 fast-poll
window) will stop new shorts and switch to longs from that point on, still
inside the same extended-hours bucket. A smaller short count on a flip day
is regime working correctly, not a rate-cap or quality-filter artifact.

**2026-09-29, same conversation - shorts join the headline P&L:**

Reversal of the original 2026-09-27 instruction. Explicit user request:
"I want to enable shorts, have it be part of the total P&L... Include it in
the extended hours as well." Shipped in `email_notifier.py`:

- The headline Total P&L/win-rate/trade-count filter changed from
  `side != "short" and entry_window != "extended"` to just
  `entry_window != "extended"` - primary-window shorts (and opening_burst,
  unchanged) now count in the real total, same as longs. Applied identically
  in `_generate_html_summary` and `_plain_text_summary` (push notification),
  so the two never disagree - this was the whole reason the original filter
  was ever duplicated in two places.
- **Extended hours is UNCHANGED and still fully excluded from the headline,
  regardless of side** - explicit user instruction, restated: "nothing at
  all is going to be included in the total P&L from the extended time
  period." Only WHAT'S EXCLUDED changed (side dropped from the condition),
  not the fact that `entry_window == "extended"` is excluded.
- `_short_strategy_html` kept as a dedicated side-specific breakdown
  (explicit user request: "a separate section... exactly how you do it for
  the long trading") - reworded so it no longer claims BOTH its categories
  are excluded from the headline, since category 1 (9:30-10:15) no longer
  is. Category 2 (extended) still is.
- `_extended_hours_html` now shows BOTH longs and shorts together (it used
  to redirect shorts to the Short Strategy section instead) - a genuine
  "how did the whole extended window do" glance. This means an extended-
  hours short now appears in BOTH sections (by window here, by side in
  Short Strategy) - deliberate, not a double-count of any total, since
  neither section's own total feeds the headline.
- Added a `Side` column to the master Closed Trades table and a small
  longs/shorts split line under the summary grid - needed now that longs
  and shorts are mixed together in what used to be a longs-only view.

**2026-09-29 - Regime Timeline in the report:**

New section, explicit user request: a colored 9:30-16:00 strip showing
which regime (bullish/bearish/neutral/choppy) was in force when, so a
reader can see at a glance when the tape favored longs vs shorts without
reading the log.

- `main.py`: `regime_timeline` list, appended to (and `logs/regime_timeline.
  json` rewritten) only when the CONFIRMED label actually changes - not
  every poll, riding on `_regime_multiplier`'s own hysteresis rather than
  being a second, noisier source of truth. Reset to empty and stamped with
  today's date at the start of every `run_trading_day` call, so a report
  built before the first read (or on a day regime_sizing never fires) shows
  nothing rather than yesterday's stale timeline.
- `email_notifier.py`: `_regime_timeline_html` reads that file and renders
  a table of proportionally-widthed `<td>` cells (not CSS gradients/flex -
  the one horizontal-bar technique that renders consistently across email
  clients including Outlook), colored per label, with a gray "no read yet"
  lead-in for anything before the first recorded transition. Shown on every
  send (including Midday Status), unlike `performance_timeline_html` -
  a partial day's regime history is still informative, not confusing.
- Not yet done, worth considering later: exporting `regime_timeline.json`
  into `logs/daily/<date>/` alongside the other per-day exports, so a
  historical day's regime timeline is analyzable after the fact rather than
  only visible in that day's own report. Small addition to
  `ops/export-daily-logs.sh` if wanted.

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
   gate (`opening_burst.min_move_to_spread_ratio`, 2.0 -> 1.5 on 2026-09-14
   after costing AAPL and others real fills on 2026-09-11): refuses a
   move that isn't at least 1.5x its own bid-ask spread, operationalizing the
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

**UPDATE 2026-09-17: the options-IV line below is stale.** The account moved
from the free tier to Algo Trader Plus ($99/mo) this date specifically for the
SIP equity feed - but Alpaca's own pricing page also lists real-time OPRA
options data on that same plan (indicative-only on free). So the door this
item closed is now open. Not pursuing it today - wiring options IV into the
screener is a new predictive input, i.e. a Tier-1-adjacent entry change in its
own right, and belongs behind the same "stop and say so" / one-variable
discipline as everything else in this list, on top of needing its own design
work (which options, which expiry, how staff/API cost scales). Recorded here
so "not available" doesn't keep getting treated as still true.

**Options implied volatility / expected move.** The `S x IV x sqrt(T/365)`
expected-move calculation needs an options chain with ATM IV, ideally 0DTE/1DTE.
~~Alpaca sells options market data on paid tiers only. Not available.~~ NOW
AVAILABLE (real-time OPRA, Algo Trader Plus) - see the 2026-09-17 update above.
Not yet designed or built.

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

**Caveat on pre-market data specifically — PARTLY STALE as of 2026-09-17.**
Written when this account was on the IEX feed, roughly 2% of consolidated
volume, which made pre-market RVOL and range weak, noisy signals rather than
the strong ones the literature describes for consolidated data. The account is
now on SIP (all US exchanges, Algo Trader Plus) - the thin-venue half of this
caveat no longer applies. Pre-market liquidity itself is still genuinely
thinner than the regular session regardless of feed, so "weak relative to the
regular session" still holds; "weak because one venue" does not. Worth
revisiting whether pre-market RVOL/range deserve more weight now that they are
measured against the real market rather than ~2% of it - with real SIP
pre-market data, not a guess.

**The larger point, which matters more than the missing inputs.** A model that
predicts |move| predicts VOLATILITY, not direction, and `score_stock`'s own
docstring already reads "Score a stock 0-100 for likelihood of volatility in
first 30 min." The entry signal then fires on `rapid_increase` — a stock that IS
moving. Volatility is therefore already selected for twice. On 2026-08-26, 12 of
22 positions went the wrong way immediately; those were not quiet stocks failing
to move, they were stocks moving against a long-only book. **The gap is
direction, and direction is the hard half.** More volatility modelling buys more
of what the bot already has.

---

## 11. Follow-ups from the 2026-09-17 SIP upgrade — waiting on real data, not code

The account moved from the free (IEX) tier to Algo Trader Plus (SIP, all US
exchanges, $99/mo) on 2026-09-17, specifically to fix the opening burst's
chronic fill-failure pattern (09-15 and 09-16 both submitted 5 openers and
filled only 1 - see git log around that date). `websocket_feed` and
`stream_max_subscriptions` are already switched (commit ee81c1d). These three
are NOT done alongside it, and deliberately not code changes today - each
needs real SIP session data that does not exist yet, or is a Tier-1 entry
change (or both), so per this file's own "one entry variable at a time, held
for a week" rule they wait.

**a. Remeasure the spread-gate math.** `min_move_to_spread_ratio` (currently
1.5, itself an active measurement window - see the top of this file) and the
"median bid-ask ~0.126%" figure baked into several opening_burst comments were
both measured against IEX's own (thinner, likely wider) quotes. Once a few
SIP sessions have run, recompute the real median spread (spread_pct is already
recorded per signal in the journal - no new instrumentation needed, just
data) and compare against the number the current ratio assumes. Do not change
the ratio's value until that comparison exists.

**b. Re-test `multifactor_rank`.** Shipped off (still `false`) after its first
live test, on sparse IEX data, ranked a +0.2% mover above a +3.0% one -
`continuation_score` renormalises over missing factors, and at 09:31 on thin
IEX data most factors WERE missing. cf_score is recorded per signal regardless
of the flag, so the evidence has kept accumulating. Denser SIP data may reduce
how often factors are missing at decision time, which would reduce the
renormalization problem that got it turned off - worth checking whether that's
actually true from real SIP cf_score data before considering turning it on,
and turning it on is its own single-variable week when that happens, not
bundled with anything else.

**c. `opening_burst.streamed_only: true` and the "REST is ~15min delayed"
assumption, generally.** New finding, not previously tracked: Alpaca's Algo
Trader Plus pricing page lists real-time latency with no delay caveat, versus
the free tier's explicit "15 minute delay via API." If REST calls are no
longer stale on this plan, several assumptions built around that staleness -
`streamed_only` refusing non-streamed opening-burst candidates chief among
them, since it changes WHICH candidates qualify - may be more conservative
than necessary. Two steps before touching anything: (1) verify this empirically
(compare a REST-sourced price against a simultaneous stream/tick price over a
real session - do not trust the pricing page alone, this project measures),
and (2) if confirmed, `streamed_only` is an entry-signal change and needs the
standing go-ahead plus its own held week, not a quick flip.

**d. The SIP upgrade's own first session (09-17) DISCONFIRMS the coverage
theory - 0/5 opening-burst fills again, same as the 09-15/09-16 pattern the
upgrade was bought to fix.** Feed coverage itself checked out fine (22-25/25
symbols had baselines vs. IEX's 0/15 that week), so thin quoting was ruled
out. What actually happened, traced through `Executor._submit_forced_entry_retry`
(executor.py:981): the forced retry reprices off a FRESH quote at the moment
of retry rather than the original decision price, and on TXG the fresh ask
~12s later had already moved so far that the resulting 2.0%-widened limit
(`retry_slippage_pct`, config.yaml:1352) landed nowhere near the decision
price (e.g. $76.56 decision -> $89.61 retry limit). The retry band itself
priced correctly off what it was quoted; the quote it was quoted had already
run away. This points at the first 15-30s of the session specifically - the
opening auction has resolved but continuous two-sided quoting from market
makers has not yet stabilized, so displayed asks can swing hard tick to tick
independent of feed quality. SIP fixes coverage, not this.

The untested lever, previously tabled pending SIP data and now looking like
the actual candidate: whether `retry_unfilled_entries`' `grace_seconds`
(currently 12, executor.py:731) is too short specifically for the opening
burst - giving the quote more time to settle before the forced retry fires,
rather than forcing a retry into a still-unsettled price 12s after the
original order. Not yet touched: `grace_seconds` isn't a signal-selection
knob (not in any of the Tier 1-5 lists above) but it changes fill/abandon
outcomes on trades already decided, so treat it with the same discipline -
one variable, held for a week, compared via `ops/session-metrics.py` - rather
than tuning it off one day's TXG print. Needs the standing go-ahead before
touching it (see this file's entry-change rule at the top / CLAUDE.md).

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

## 5. 2026-09-30 — regime gating tightened to directional-only, Tier 4

Explicit user request, verbatim: "I want to have no trades occurring at all
when the regime is choppy. Wait until the regime is bullish to do long or
it's bearish to do shorts, but nothing should be executed at all whether
[the] regime is choppy or neutral."

**What changed**: `trading.regime_sizing.neutral_multiplier` and
`choppy_multiplier` went 0.5 -> 0.0 (config.yaml). Longs now trade ONLY on a
confirmed BULLISH read. Nothing else needed to change - `short_strategy`
already had `neutral_multiplier: 0.0` and `choppy_multiplier: 0.0` (shorts
were already bearish-only, by the hard mutual-exclusion rule plus their own
table), so this was a one-sided fix: bring the long side's gating up to the
same all-or-nothing standard the short side already had.

**Mechanism reused, not built**: `mult == 0` was already a distinct,
logged stand-down at the entry-skip site (main.py, ~line 2788 - "the regime
says stand down" vs. a zero-share sizing rounding error), because
`bearish_multiplier: 0.0` already relied on it. Extending that same path to
neutral and choppy required no code change, only the two config numbers.

**This is a Tier 4 entry change** (`regime_sizing`, including `chop` and the
multipliers - see CLAUDE.md). It silently changes the sample: every trade
that would have gone out at 0.5x size in a neutral or choppy read simply will
not exist going forward. Two multipliers moved together rather than one at a
time - normally against the "one entry variable at a time" discipline - but
they are one coherent policy decision (directional-only trading), not two
independent tunings, and the user's instruction was explicit and covered
both in the same sentence.

**What this predicts, to check against next week's data**: fewer total
trades (today, 2026-09-30, had a mostly-choppy session per the Regime
Timeline - a meaningful fraction of today's 97 trades would not have fired
under this rule), and the long side should stop absorbing the chop losses
that `_chop_reading`'s own docstring describes (2026-08-28: 19 of 30
positions peaked under +0.5% and lost $484 together). The predicted
trade-off is fewer opportunities taken during the (historically common)
neutral/choppy stretches in exchange for not paying the 08-28-style cost.
Revisit after a week of trades under this config change, per the standard
`ops/session-metrics.py` comparison-to-prior-week methodology.

**Tests updated**: `tests/test_short_strategy.py`'s G1 section (hard mutual
exclusion) asserted "neutral/choppy leave longs nonzero" as a documented
consequence of the OLD config - rewritten to check both long AND short
multiplier per label against the new all-zero-outside-its-own-direction
policy. `tests/test_regime.py` and `tests/test_integration_0902.py`'s live
config coherence checks (`bullish > neutral >= bearish`, ordering-only,
non-strict at the bottom) still pass unchanged since bearish was already 0.

## 6. 2026-09-30 — Regime Timeline: visible ticks, not just hover tooltips

Explicit user request, in response to the 09-29/09-30 Regime Timeline
screenshots: "make this timeline more descriptive. Add ticks throughout the
bar so i know the exact time the regime was and when it was changing."

The original bar (`email_notifier._regime_timeline_html`, shipped
2026-09-29) only exposed each segment's start time via an HTML `title`
attribute - a desktop hover tooltip, invisible on the phone screenshots the
user was actually reading from.

Two additions, both kept as plain `<td>`/border/text (no `position:absolute`
or flex on the bar itself) for the same Outlook-compatibility reason the
original docstring gives for using a `<td>`-width bar instead of a CSS
gradient:
  - a 1px `border-left` on every bar cell after the first, so each
    transition is a visible line ON the bar, not only a color change;
  - a chronological text line underneath - `09:41 BULLISH -> 11:15 BEARISH
    -> 13:02 CHOPPY -> ...` - one entry per transition, in order. Rejected
    alternative: positioning each time label under its own segment via
    `position:absolute`, which breaks down on segments too narrow to hold
    their own text and is exactly the technique the bar itself avoids for
    Outlook.

Tests: `tests/test_extended_hours.py` extended with checks for the new
`border-left` tick styling and the new chronological text line, alongside
the existing coverage for colors/legend/tooltip/stale-data handling.

## 7. 2026-09-30/10-01 — ORPHAN_RECONCILE, take three: the late-fill blind spot

Found while investigating why ORPHAN_RECONCILE fired 20 times on 2026-09-30
(worse than 09-28's 15) despite commit 912342d (item in section above) being
live before that morning's open. 912342d fixed a DIFFERENT bug - this is a
third, separate mechanism in the same failure family.

**The bug.** `Executor.retry_unfilled_entries` already had a post-cancel
re-check before giving up on an entry (added 2026-09-14, after FOUR/NBIS/
VRT/AAOI/CIEN/AXTI all orphaned the same way) - cancel the stuck order,
immediately re-check the broker, and only abandon tracking if the re-check
still shows nothing. That catches a MILLISECOND-scale cancel-vs-fill race.
It does not catch Alpaca's paper-trading simulator filling an order
50-260s AFTER the cancel+recheck already ran and the bot walked away - a
delay the user identified independently on 2026-09-27, unrelated to any
config lever. Traced concretely on IONQ, 2026-09-30: abandoned (tracking
fully wiped) at 13:30:32; the periodic reconcile (~300s cadence) did not
discover the broker actually holding -460 shares until 13:45:14 - 15
minutes of a completely dark, unmanaged position, force-closed at whatever
price was showing. 9 of that day's 14 "never filled" opening-burst symbols
(BE, BRKR, ILMN, IONQ, NBIS, SMTC, TEM, TWST, TXG) show this exact
signature, which also reframes weeks of "why is the opening-burst fill
rate 0%" - some fraction of those were never truly 0%, just invisibly
filling late.

**The fix.** `Executor._recently_abandoned` (symbol -> ts/qty/side/
decision_price) plus `check_late_fills`, called from
`refresh_account_snapshot` every poll (reusing the positions dict already
fetched that poll, no extra broker call) whenever there is something to
watch. Populated at the ONE site that actually wipes tracking in
`retry_unfilled_entries` (the give-up-after-retry path; the separate
"forced retry could not even be submitted" path is NOT populated here,
correctly - no order is in flight there to fill late). A matching-direction
fill inside `late_fill_watch_seconds` (new key under
`marketable_limit_entries`, default 240s - past the reported 260s worst
case, short of reconcile's own ~300s cadence) is handed to a new
`on_late_fill_confirmed` callback, wired in main.py to
`Strategy.confirm_entry`, re-opening the position with a normal exit
profile instead of leaving it for the next reconcile sweep's blind
force-close. Past the window, nothing changes - ORPHAN_RECONCILE remains
the backstop, same as before this fix, now catching genuinely-stuck cases
instead of routine late fills.

**Known limitation, accepted rather than solved.** A late-confirmed fill
always gets the DEFAULT exit profile, never whatever `config_override` the
original attempt intended (e.g. opening-move's tighter exits) - Executor's
own bookkeeping (`_pending_entry_verify`/`_recently_abandoned`) only ever
carried qty/side/decision_price, never which profile a symbol's entry was
meant to use. Fixing that would mean plumbing the profile choice through
Executor, which currently has no reference to it at all. Still strictly
better than the status quo (no exit profile for up to 15+ minutes).

Tests: `tests/test_safety.py` section D (9 checks): population at the give-
up site, confirmation on a matching late fill (long and short), a
mismatched sign correctly NOT confirming, window expiry without falsely
claiming a fill, a raising callback not propagating, the
refresh_account_snapshot wiring, the config default/override, and the
main.py wiring using `side=` as a keyword (a positional wire-up would
silently corrupt every re-opened trade's exit profile, since
`confirm_entry`'s 4th positional argument is `config_override`, not
`side`).

## 8. 2026-10-01 — Open Positions report: Side column, and a real P&L sign bug it surfaced

Explicit user request: add a long/short column to the email reports. The
main "Closed Trades" and "Extended Hours" tables already had one
(`side_label = "SHORT" if t.get("side") == "short" else "LONG"`, same
pattern reused here); `_open_positions_html` (the mid-session Open
Positions table) did not.

Adding it surfaced a real, separate bug while building it:
`_open_position_rows` (main.py) computed every open position's unrealized
P&L as a raw `(current - entry) * qty`, with no direction term - correct
for a long, backwards for a short, where a FALLING price is the gain. Any
open short sat in the mid-session report showing the exact opposite sign
of its real live P&L for as long as it stayed open. Same class of bug
already found and fixed in `trade_paths.csv` (2026-09-28) and
`_position_size`, just never caught here until the Side column made it
worth checking. Fixed: `pl`/`pl_pct` now multiply by `trade.direction`
(+1 long, -1 short), and each row carries its own `side`.

Tests: `tests/test_sched.py` section L - a long and a short at matched
distances from entry, confirming the long's P&L is the plain move and the
short's is correctly POSITIVE when price fell (previously would have
been negative), plus the new `side` field and the rendered Side column
in both the data and the HTML.

## 9. 2026-10-01 — take_profit_tiers 1.0/1.25/1.5 -> 0.5/0.75/1.0

Explicit user request ("yes lets try this out"), following
`ops/replay.py`/`ops/grid.py` evidence from the same conversation: a
smooth marginal gradient across 302 long trades over all 8 recorded days,
two different lower-tier sets both clearly ahead of two higher ones
(0.5/0.75/1.0 at +$0.67/trade and 0.4/0.6/0.8 at +$0.62/trade, vs
0.75/1.0/1.25 at +$0.32 and the prior live 1.0/1.25/1.5 - the WORST of
the four - at +$0.15/trade). `ops/grid.py`'s own honest verdict: "80 of 80
configs not distinguishable from the top one at n=302" - not proven, but
a plateau across two different lower-tier sets rather than one lucky
spike, which the tool's own docs call trustworthy before any cell reaches
significance on its own.

Exit-side (`take_profit_tiers` is explicitly on CLAUDE.md's "tune these
freely" list), so no Tier-gated entry-measurement-window discipline
applies, but it still touched a wide ripple of hardcoded tier values
across `test_tiers.py`, `test_be.py`, `test_collide.py`, `test_safety.py`,
`test_0902b.py`, `test_timeline.py` and `preflight.py` - all updated.

**Three side effects surfaced and deliberately NOT fixed, flagged in the
tests that found them instead:**
  - `regime_sizing.chop.take_profit_tiers` (0.4/0.7/1.0, Tier 4, untouched)
    was built to sit strictly below the session ladder at every tier;
    its top tier (1.0) is now merely TIED with the session's new top
    tier, not below it. Chop's distinctiveness at the top has narrowed.
    Not lowered here - that would be changing a Tier 4 setting nobody
    asked to change, on no evidence of its own.
  - `breakeven_trigger_pct` (0.5, untouched) now sits exactly AT the new
    tier1 instead of strictly below it - the margin that used to exist
    between "floor arms" and "a tier could fire" is now zero, not
    negative. Not tightened here on the same reasoning.
  - `first_exit_loss_pct` (-0.7, untouched): tier1 (0.5) is now a smaller
    move than the first stop-loss's magnitude (0.7). Not a timing
    conflict (a price cannot be both up and down at once), just a
    different reward/risk shape than before - and the replay/grid run
    that justified the new ladder walked real paths against this exact,
    unchanged stop, so the combination (not the tiers in isolation) is
    what actually tested well.

All three are documented at the test that previously asserted the old,
now-superseded relationship, not silently loosened.

## 10. 2026-10-01 — reentry_cooldown_after_loss_only: true -> false

Explicit user request, following a direct question about whether 97
trades by midday could be spread out more. Checked whether fast same-
symbol re-entries are actually worse, using all 8 recorded days of
`trade_history.csv` grouped by (symbol, date) and split on the gap since
that symbol's own last exit:

```
re-entered the SAME symbol <5 min after its last exit:   n=30  mean -$5.81
re-entered the SAME symbol >=5 min after its last exit:  n=93  mean +$2.06
```

Split further by whether the prior exit was a win or a loss, it is
almost entirely one thing: **28 of the 30 fast re-entries followed a
WIN** (mean -$5.89, total -$164.88) - because
`reentry_cooldown_after_loss_only: true` meant a winning exit was never
subject to any cooldown at all. "Slow (>=5 min) after a loss" (already
gated by the cooldown) was the single best bucket measured, +$4.86/trade,
n=57. The single-day evidence that originally justified carving wins out
of the cooldown (UBER/CHWY/CMG all profitable on re-entry that one day)
did not hold up against the broader sample.

Flipped the config flag; `Executor.reentry_cooldown_remaining`'s branch
logic needed no code change; it already implements both behaviors
correctly on this one setting; see its own docstring, updated to match.

Side benefit, not the primary reason: this also partially addresses the
"can entries be spread out more through the day" question from the same
conversation - a cooldown after a win makes the bot wait longer before
re-entering that symbol, which slows how fast `max_entry_attempts_per_
symbol_per_day` gets used up on any one name, pushing some attempts later
in the day rather than front-loading them. Not pursued as a dedicated fix
on its own; market-structure volatility clustering early in the session
is real and not something to fight by holding back good signals.

**Tests, requested explicitly ("tested end to end")**: the existing test
suite had NO test exercising this branch's actual win/loss logic through
the real `submit_exit_order` path before this change - every hit was
either a synthetic fixture or a bare assertion pinning the live config
value. Added to `tests/test_0902b.py`: four checks running a real
`Executor` through `submit_exit_order` for both a winning and a losing
exit, under both the new live setting (false) and the old one (true, as
a deep-copied override) - confirming the win-exemption is gone under the
new default AND that the old behavior still works correctly if the flag
is ever reverted. Caught one real test-fixture bug while writing it: the
fake broker initially reported 0 shares held, which routed the "exit"
through the PHANTOM-drop path instead of a real exit and made every
variant pass for the wrong reason (phantom_cooldown_remaining, checked
first, is unconditionally >0 regardless of win/loss) - fixed by having
the fake broker actually report the position as held. Also updated
`test_socket.py` (added a rendering check for the "after any exit" label,
alongside the existing "after losses only" one), `test_wsfail.py`, and
`test_0902b.py`'s own phantom-cooldown section (corrected a comment that
had the wrong mechanism for why that specific check is unaffected by this
flag - phantom cooldown short-circuits before the loss-only branch is
ever reached, it is not that phantoms count as losses).

## 11. 2026-10-01 — signal_journal.csv gets a `regime` column

Explicit user request: "the market looks like it's gonna be very choppy
the next upcoming days and not making any trades at all might be bad...
keep a measure somehow of the date and the way symbols are trading in a
choppy regime for the next few days." With longs gated to bullish-only
and shorts to bearish-only (this same week's regime-gating change), every
signal refused for a regime stand-down previously vanished from the
record with no way to tell "the regime refused this" apart from any other
internal rejection (`skip_reason` only ever said
`rejected_by_pre_entry_checks`), let alone see what the symbol actually
did afterward.

**What changed**: added `regime` to `JOURNAL_FIELDS` (the regime label in
force at signal time) and wired `regime=regime_state.get("label")` into
both the long and short normal-window `signal_journal.record()`/
`short_signal_journal.record()` calls - both journals reuse the same
`SignalJournal` class, so the schema change applies to both with the one
edit. Not wired into the opening-burst mechanism's own journal calls
(`_run_opening_move_exp` doesn't receive `regime_state` at all, and
threading it through for a currently-disabled mode wasn't worth the
added surface).

This is purely observational - no entry logic changed, nothing about
WHICH trades happen. Every signal, taken or refused, already carries
`pct_15min`/`pct_30min` forward returns regardless of `taken`/
`skip_reason` - the `regime` column is what turns "what would a choppy
regime's refused signals have done" into a question this file can
already answer: filter `signal_journal.csv`/`short_signal_journal.csv` on
`regime in (choppy, neutral)` and read the forward-return columns
directly, the same way the file's existing design already treats every
other refused signal as its own control group.

**Appended at the END of JOURNAL_FIELDS**, matching the precedent set by
`cf_sector_strength`/`cf_sector_etf` - `repair_header` remaps by name, and
a column inserted mid-schema is exactly the shape that made the signal
journal's header rot unreadable on 2026-08-26. A new `JOURNAL_FIELDS_
HISTORY` entry records the pre-regime schema for older rows.

Also updated two standalone copies of this exact field list that must
stay in sync by hand (`ops/session-metrics.py`, `ops/analyze-journal.py`
- caught by `test_schema.py`'s existing "schema matches src exactly"
check, which failed immediately until both were updated).

**Tests**: `tests/test_journal.py` section G - the field round-trips
through a real `SignalJournal` write+read cycle, is recorded even for a
refused signal (the actual point), and both main.py call sites are
confirmed to pass it. `tests/test_schema.py` updated: the existing
"without declared history, an older generation misreads" counterfactual
now explicitly pins the sector-column (inserted) generation rather than
whatever is structurally last, since appending (what `regime` does)
doesn't reproduce that fault - a NEW, separate demonstration was added
showing exactly that difference (an appended column needs no declared
history for a row's OTHER fields to keep reading correctly; only the
appended column itself is honestly blank for an older row that never
recorded it).

**What this does NOT do**: it does not change the chop/neutral exclusion
itself, propose a partial-size compromise, or re-enable any trading
during chop. It is pure instrumentation, so the user has real data (not
a re-litigated guess) to decide what to do if the next several sessions
are dominated by chop.

## 12. 2026-10-01 — ORPHAN_RECONCILE, take four: the EXIT side's own gap

First full trading day running every 2026-09-30 fix (late-fill watch,
regime gating, reentry cooldown, take-profit ladder). ORPHAN_RECONCILE
fired 69 times - worse than any prior day (09-29's 28 had been the
previous worst) - for -$853.49, against a total day P&L of -$741.61 (the
rest of the day's trading was actually net +$111.88). This is a
DIFFERENT mechanism from everything fixed the day before - the late-fill
watch covers the ENTRY side specifically; this is an EXIT-side gap that
was never touched.

**The bug.** `strategy.confirm_exit` commits a qty_remaining reduction
the instant an exit order is SUBMITTED, not once it fills - the same
optimistic-then-reconcile pattern `submit_entry_order` already uses for
entries (see that docstring). If an exit gets cancelled and superseded by
a DIFFERENT exit condition before it ever actually fills - an entirely
normal, expected path (a marketable-limit order sitting unfilled, then
the next poll's check_exit finds a different rule now also qualifies) -
the superseding exit computes "everything remaining" from the
already-decremented qty_remaining, not from what the broker actually
holds. `Executor.submit_exit_order` already re-reads the broker's live
qty and corrects DOWNWARD when it holds LESS than tracked (a known,
already-fixed case, 2026-09-01's CRM incident) - there was no equivalent
correction for the broker holding MORE, which is exactly what happens
here.

Traced exactly on MXL: `FIRST_EXIT_-0.7%` submitted a sell for 12 of 38
shares at 13:43:00; cancelled, still 0/12 filled, 4 seconds later when
`TRAILING_STOP` superseded it; TRAILING_STOP computed its own "remaining"
as 38-12=26 (already assuming the 12 were gone), submitted that, and
later escalated to a market order for 26 when IT also didn't fill fast
enough. The broker genuinely sold 26 and still held 12 - a residual the
bot's own bookkeeping had already written off as closed. Discovered
~5 minutes later by the periodic reconcile, force-closed via
ORPHAN_RECONCILE with no stop-loss ever having run on it. This exact
sequence (a partial exit superseded by a different rule before its fill
confirms) is common enough - FIRST_EXIT in particular is meant to be an
early, tentative scale-out, exactly the kind of exit likely to get
overtaken by a stop or trail moments later - that it plausibly explains
most of the 69.

**The fix.** `submit_exit_order` now mirrors its own existing shrink-
correction in the other direction: when this exit is a FULL exit
(`not is_partial_exit(reason, qty, qty_before)` - qty_before is the
qty_remaining Strategy believed was left at the moment this exit fired)
and the broker's live count is GREATER than what's being asked, sell the
broker's true live quantity instead of the stale "remaining" count. A
genuinely partial exit (FIRST_EXIT itself, a take-profit tier that
deliberately leaves shares running) is left untouched either way - this
only ever widens a FULL exit, never inflates a deliberate partial into
an unintended full close.

**Tests**: `tests/test_0902b.py` section 17 (4 checks) - the exact MXL
scenario reproduced end to end through a real `Executor.submit_exit_order`
call (corrects 26 -> 38); the existing shrink-correction confirmed
unaffected (still corrects down when the broker holds less); a genuine
partial exit confirmed NOT inflated even when the broker holds more;
and the correction confirmed inert without `qty_before` to compare
against (nothing to judge "full vs partial" from). Full suite re-run
clean (2860 pass, 0 fail) after the addition.

**Follow-up audit, 2026-10-02 (explicit user request to look for anything
else this might have missed).** Checked how many of 09-30's 69 orphans
actually show the exact MXL shape (two DIFFERENT exit reasons racing):
only 21 of 69 have a second, distinct exit_reason logged on the same
position beforehand. The other 48 show only ONE exit reason (e.g.
`FINAL_EXIT_-1.0%`, `BREAKEVEN_STOP`) before the orphan - not the two-
rule-supersede sequence traced on MXL.

This does not mean the fix misses them. The fix operates on the general
condition (`qty >= qty_before` - a full exit - and the broker holds more
than that), not on "two different reasons were involved" specifically.
Traced why: main.py always passes `qty_before=trade.qty_remaining` at
the moment of the call, and `Strategy.check_exit` computes a full exit's
own qty from that SAME `qty_remaining` - so for ANY full-exit call
through the normal path, `qty == qty_before` by construction, and
`is_partial_exit` correctly reads it as full regardless of how
`qty_remaining` got stale (a different rule superseding, as on MXL; a
same-reason resubmission; or any other path not yet individually traced).
The fix fires on that general condition, so it should cover the other 48
too, not just the MXL-shaped subset - but this is reasoning from the code,
not independently re-confirmed against each of the other 48 line by line
given time constraints. The real test is tomorrow's live orphan count
once deployed.

Also checked `retry_unconfirmed_exits` (the OTHER place that forces a
qty onto the broker) and `flatten_all_positions` (the 16:00 sweep) for
the same class of staleness - both already compute their forced quantity
from a FRESH `broker.get_positions()` read, not from tracked state, so
neither needed the same fix. Nothing else found in this pass.

`max_entry_attempts_per_symbol_per_day: 4 -> 3`, same date, explicit
user request to reduce daily trade count without touching the symbol
list. See config.yaml's own comment for why this is NOT backed by a
fresh attempt-number re-analysis: 09-30's own trade sample is too
contaminated by that day's orphan bug (an incorrectly early/forced close
can free a symbol's re-entry cooldown sooner than a correct exit would
have) to read cleanly. Revisit the attempt-number question once a few
days of post-fix data exist.

## 13. 2026-10-02 — SHORT EXITS WERE SELLING, NOT COVERING, SINCE THE SHORT STRATEGY SHIPPED

Found while chasing why 10-02 still had 46 `ORPHAN_RECONCILE` events after
the 10-01 exit-qty fix (#12) was confirmed live and firing correctly. Traced
PRIM: a 19-share SHORT opened at 15:21, a `TRAILING_STOP` "exit" at 15:33
logged `"Limit order submitted: PRIM 19 SELL @ 77.74"` with a POSITIVE P&L
(+$4.27) despite the exit price being ABOVE the entry price - backwards for
a short, where a higher exit price is a loss. By 15:35 the broker held -38 -
exactly double the original -19.

**Root cause.** `Executor.submit_exit_order` defaults to `side="sell"`
(closing a long) and only covers a short (`side="buy"`) when the CALLER
says so. `main.py`'s normal exit call site - the one every `check_exit`
result (FIRST_EXIT, TRAILING_STOP, TAKE_PROFIT, BREAKEVEN_STOP, GAP_EXIT,
MOMENTUM_FADE, RESISTANCE, FINAL_EXIT) flows through on every poll - never
passed `side=` at all. So every normal exit on a SHORT position submitted
a SELL, which doesn't close a short, it ADDS to it. `check_exit` has no
"side" key in its return dict to begin with (confirmed by reading the
whole method) - the side was always supposed to come from the trade
itself, and nothing wired it through.

This has been live since `short_strategy.enabled` went to `true`
(2026-09-27) - every short exit through the normal path has been growing
the short, not closing it, for the better part of a week. The reason this
didn't show up as universal runaway shorting on every position: many
shorts got closed correctly anyway, either by `flatten_all_positions`'
16:00 sweep (reads the broker's own fresh position, side-agnostic, already
correct) or by `close_orphaned_position` once a mismatch persisted long
enough to trip the reconcile gate - which is itself the ORPHAN_RECONCILE
noise this was found while investigating. The ones that got caught by
neither, like PRIM, just kept doubling until something closed them.

**The fix.** `main.py`'s normal exit call site now passes
`side=("buy" if trade is not None and trade.side == "short" else "sell")`
- `trade` (`strategy.trades.get(symbol)`) is only `None`-unreachable when
`exit_info` is falsy anyway, since `check_exit` only returns a result for
a symbol that's in `self.trades`, so this always has a real `trade.side`
to read from for a genuine exit. `submit_exit_order` already derives
`is_cover = (side == "buy")` from this same parameter internally, so
everything downstream (limit price band direction, P&L sign) auto-
corrects once `side` itself is right - no other code needed to change.

**Tests**: `tests/test_short_strategy.py` section I - two source-level
checks (this file's established pattern for this kind of call-site wiring,
see section F/G4) confirming the exact `side=` expression is present and
is part of the SAME `submit_exit_order` call as `qty_before`, not some
unrelated one. Full suite re-run clean (2862 pass, 0 fail) after the
addition.

**Not yet independently measured**: how much of 09-27 through 10-02's
short-side P&L was corrupted by this (shorts silently growing instead of
being managed by their own stop/target logic until EOD flatten caught
them) - the live orphan count and tomorrow's short-side P&L are the real
test now that this is fixed and deployed.

## 14. 2026-10-05 — full end-to-end verification of #13, and a second short-specific bug found auditing for siblings

Explicit user request after #13 landed: prove it's actually fixed, start to
finish, every scenario, simulate it if needed - "please I don't want
anymore errors."

**End-to-end simulation, not just a source-string check.** Section I
(2026-10-02) only confirmed the right line of code exists at the call site.
Added section J in `tests/test_short_strategy.py`: a `LiveFillBroker` double
that actually moves its held quantity on every fill, driven through the
real `Strategy`/`TradeManager`/`Executor` mechanism across a full two-leg
exit chain (a partial exit, then a second rule superseding it - the exact
FIRST_EXIT -> TRAILING_STOP shape traced on MXL/PRIM/etc.). Run twice: once
with `side` derived from `trade.side` (today's fix) and once with `side`
hardcoded to `"sell"` (the exact pre-10-02 bug). The fixed run closes the
broker to EXACTLY 0. The buggy run reproduces the doubling mechanism from
first principles - broker ends at -100, matching -50 doubling to -100 -
independently confirming the forensic reconstruction done on 10-02 from the
live logs, without relying on that reconstruction being correct. A LONG
chain run through the same harness confirms no regression - still sells,
still closes cleanly.

**A second, separate short-specific bug found in the same audit pass.**
`Executor.retry_unconfirmed_exits` (the safety net that forces a market
order when a marketable-limit exit sits unfilled past `grace_seconds`) read
`held = int(float(position.qty))` with no `abs()` - every OTHER qty read in
this file already takes the magnitude (`submit_exit_order`'s `live_qty`,
`close_orphaned_position`'s `qty`, `flatten_all_positions`' `qty`); this one
was the exception. For a short, `position.qty` is negative. Simulated it
directly: a short's stuck cover for 26 shares that never filled at all
(broker still holds the full -26) computed `filled_so_far = qty_before(26)
- held(-26) = 52`, which is `>= intended_qty(26)` - the "this order already
did its job, nothing to force" branch fired on a position that had not
moved AT ALL, silently dropping it from tracking with no market order ever
forced. This is exactly the WLY-shaped failure (2026-09-03) this method
exists to prevent, just for the one side its own test coverage never
exercised. Confirmed via direct simulation before and after - the broker
is left at -26 (unprotected) before the fix, 0 (forced market cover) after.

**Fix**: one-line `abs()` added to the `held` read. Three new tests added
in section K: never-filled (forces the full shortfall), partially-filled
(forces only the remainder, not a second full order that would flip the
position into an accidental long), and fully-filled (left alone, no
spurious order).

**CLAUDE.md updated**: the "every order call site must derive side= from
the position" rule now also names this `abs()` requirement explicitly,
since it's the same root cause (a short's signed quantity handled as if it
were a long's) in a different function.

Full suite: 2875 pass, 0 fail (91 in `test_short_strategy.py` alone, up
from 78). Neither of today's findings is visible in Friday's logs or any
log collected so far - they were found by audit and simulation, not by
tracing a live incident, which is the point of doing this pass at all.

## 15. 2026-10-06 — the emailed report's "a lot of N/A": signal_pct and entry_rsi were never threaded through

User complaint, direct: the daily emailed report has "a lot of NA... in a
lot of the columns and rows." Checked every column's non-blank rate in
`trade_history.csv` across 5 recent days (09-29 through 10-05) - every
field was fully populated except two, which were **0/N populated on every
single day checked**: `signal_pct` and `entry_rsi`.

**`signal_pct`**: `record_entry_meta()` never accepted it as a parameter at
all, despite `signal_pct` being available at literally every
`_attempt_entry` call site in `run_trading_day` the whole time - just never
threaded the last few feet of the call chain (`_attempt_entry` ->
`submit_entry_order` -> `record_entry_meta` -> `entry_meta` ->
`trade_history.csv`). The opening-burst call site was the sole exception -
it already passed it correctly, which is how this went unnoticed: nobody
ever happened to look at an opening-burst row specifically next to a normal
one.

**`entry_rsi`**: wired correctly end to end, but its only source
(`rsi_values`) is only ever computed when `use_rsi_filter` is on - which has
been `False` this entire time, so `symbol_rsi` was always `None` by the time
it reached the entry call site. Not a reporting bug so much as a reporting
BLIND SPOT: the RSI filter being off should mean "RSI doesn't decide
anything," not "RSI is never even looked up."

**Fix**: `record_entry_meta`/`submit_entry_order` gained a `signal_pct`
parameter, threaded through from the 4 real call sites that were missing it
(normal long, short, pullback-resumption - opening-burst already had it).
`entry_rsi` now has a fresh-fetch fallback right before the entry order
goes out when `symbol_rsi` is `None`, mirroring exactly how `exit_rsi` is
already fetched fresh at exit time (same try/except-never-blocks shape) -
deliberately does NOT touch `use_rsi_filter` or `rsi_max`, so no entry
DECISION changes, only what gets recorded about one already made.

Three hand-rolled fake-Executor test doubles (`tests/test_opening.py` x2,
`tests/test_bursts_separate.py`) had their own `submit_entry_order` stub
signatures and needed `signal_pct=None` added to match - caught immediately
by the full suite, not a surprise later. New regression coverage in
`tests/test_phantom_exit.py` section 35 (11 checks) confirms both fields
reach `entry_meta` end to end and that every real call site passes
`signal_pct`. Full suite: 2885 pass, 0 fail.

## 16. 2026-10-06 — trading.conviction_gate: a day-level size scalar for signal QUALITY, not direction (OFF by default, shadow-mode-first)

Built in direct response to 2026-10-05's loss: regime read bullish (both
indices above VWAP) for the entire session, longs were the only side gated
open under the existing directional gating, and the day still lost
-$142.93 at a 31% win rate. Traced why: SPY/QQQ barely moved all day
(+0.02% to -0.04%, crossing zero repeatedly) and the day's ENTIRE signal
pool (`signal_journal.csv`, taken or not) averaged a NEGATIVE forward
return (mean `pct_15min` -0.094%) before any selection even happened - a
technically-bullish, genuinely weak tape. `regime_sizing` cannot see this:
it only reads price vs VWAP, which says nothing about follow-through. This
is exactly the gap the user's "Market Environment Cheat Sheet" conversation
flagged as missing (the range-bound/quality category) - a day can be
labeled correctly by direction and still be a bad day to trade at full
size.

**Mechanism** (`_compute_conviction_read` in `src/main.py`): pools
`pct_15min` across every signal (taken or not) from the trailing
`lookback_days` COMPLETED sessions under `logs/daily/` - exactly
`ops/session-metrics.py`'s "all signals" column (the do-nothing benchmark),
read live instead of after the fact. Today is always excluded (its own
signals' forward returns aren't known yet). Below `weak_threshold_pct` ->
label `"weak"`, multiplier `weak_size_multiplier`; otherwise `"normal"`,
multiplier `1.0`. Composes into `_position_size` as a third independent
multiplicative factor alongside `regime_size_multiplier` and
`loss_tier_multiplier` - same pattern, same place, same "always 1.0 when
inert" convention.

**Ships OFF (`enabled: false`), and even when enabled, ships in
`shadow_mode: true`** - computed and logged every day (`CONVICTION GATE:
...` in the service log) but the multiplier returned to the caller stays
`1.0` regardless, so nothing about real sizing changes. This follows
CLAUDE.md's own Tier 4 rule to the letter: anything that can silently
shrink the sample gets a week of observation before it's allowed to touch a
real order, the same discipline `regime_sizing`'s chop table and
`short_strategy` were both built under before going live. **Do not set
`shadow_mode: false` without first reviewing a week of logged reads against
what actually happened those days** - this has exactly zero days of
real-world validation as of this writing.

Tests: `tests/test_conviction_gate.py`, 18 checks - the off/no-op case, a
synthetic weak trailing pool (shadow vs live mode), a synthetic normal
pool, no-data-at-all (must not manufacture a false "weak"), confirms
today's own date is never read even when present on disk, and two
source-level checks tying the composition and the session-reset into
`_position_size`/`run_trading_day`. Full suite: 2903 pass, 0 fail.

**Next step, explicit**: let shadow mode run for the CLAUDE.md-standard
week, then bring the logged reads back for a real go/no-go conversation
before ever flipping `shadow_mode: false` - this entry itself is not that
go-ahead.

## 17. 2026-10-06 — max_entry_attempts_per_symbol_per_day reverted 3 -> 4; investigated the 3-day negative-edge streak

`max_entry_attempts_per_symbol_per_day: 3 -> 4`, explicit user request,
reverting the 10-02 change. Not a correction - 2026-10-05's own data showed
the cap at 3 genuinely binding (804 of 4307 signals skipped for hitting it,
the single largest filter that day, well ahead of burst_throttle's 829),
confirming it was doing exactly what the 10-02 change asked for. This is a
volume preference reversal, not new evidence against the cap.
`qqq_list_top_n`: user asked to cut it to 2 - already at 2 since 09-30
(`8283e0d`), no change needed, told the user so rather than silently
no-op'ing.

**What caused the 3-day negative edge streak (10/1, 10/2, 10/5)?** Checked
every config.yaml commit between 09-27 and 10-01. The most likely single
cause: `2ad17fd` (2026-09-30 16:56 UTC, landing between 09-30's close and
10-01's open) set `regime_sizing.neutral_multiplier` and `.choppy_multiplier`
from 0.5 to 0.0, on explicit user request at the time ("I want to have no
trades occurring at all when the regime is choppy... nothing should be
executed at all whether regime is choppy or neutral"). Before: longs could
still fire at half size in neutral/choppy windows. After: longs ONLY fire
on a confirmed-bullish label - the exact window 10-05's own investigation
(item 16 above) showed can be "bullish" by the VWAP-crossing definition
while the underlying tape is flat/directionless. Mechanism: narrowing entry
eligibility to ONLY the bullish label concentrates entries into exactly the
label this session proved can mean "weak, technically-positive, no real
follow-through" - whereas before, some neutral windows (which aren't
inherently worse, just unclassified either way) were also eligible and may
have diluted that concentration.

**Caveat, stated plainly**: this is the best-evidenced single hypothesis
from timing + mechanism, not a proven cause. Only 3 trading days of
post-change data exist (10-01, 10-02, 10-05 - the 09-30 session itself
still ran under the OLD 0.5/0.5 multipliers and had positive edge,
+0.229pp), and 2 of those 3 days were also still running the orphan/
exit-side bugs fixed later the same week (fixed 10-01 and 10-02
respectively) - though note the edge metric itself is computed from
signal-time forward returns, not realized trade P&L, so it should be
independent of those bugs specifically.

**Tips for fixing, in order of how much is already built**:
1. **The conviction gate (item 16, this file)** is the most direct answer -
   it exists specifically to catch "technically bullish, actually weak"
   days like 10-05 without re-opening neutral/choppy entries, which the
   user explicitly asked to close off for other reasons. Shadow mode first,
   per that item's own notes.
2. Re-testing a small positive `neutral_multiplier` (e.g. 0.25, leaving
   `choppy_multiplier` at 0.0) as its own one-week held Tier 4 experiment -
   this would directly test whether neutral specifically was wrongly
   excluded, separate from choppy. Not done - would reopen a door the user
   explicitly asked to close 09-30, so flagged as an option, not actioned.
3. Do nothing yet and watch for a 4th negative day - 3 is suggestive, not
   yet proof of a persistent regression rather than a rough week.

## 18. 2026-10-06 — neutral_multiplier experiment actioned; the chart artifact gets a persistent "Active Experiments" banner

Option 2 from item 17 actioned on explicit user request:
`regime_sizing.neutral_multiplier: 0.0 -> 0.25` (choppy stays 0.0). See
config.yaml's own comment on the key for full reasoning - this entry is
just the artifact-side half.

**The daily chart artifact (`daily_viz/index.html` + `meta.json`) now has a
persistent "Active Experiments" banner**, requested explicitly so a held
change doesn't disappear into a comment nobody rechecks: `meta.json` takes
an optional `"experiments"` array of `{label, started, detail}`; the page
renders them in a visible amber-bordered box right under the narrative,
every day they're present, not just the day they start. **The daily
routine must now carry this forward**: keep the `neutral_multiplier`
experiment entry in `meta.json`'s `experiments` array every day until the
one-week comparison (vs the three 0.0-neutral days - 10-01, 10-02, 10-05)
is actually done and reported back to the user, THEN remove it (or set
`"experiments": []`) once concluded - don't just keep copying it forward
out of habit after it's resolved, and don't drop it early either.

## 19. 2026-10-06 — ATR floor (shipped, live) and catalyst watch (shipped, observation-only)

Both from the "stocks in play" conversation, both explicit user go-ahead
("let's add both").

**`trading.atr_floor`** (enabled, live): refuses an entry whose ATR doesn't
clear `min_atr_pct` (0.5%) - the complement of `volatility_sizing`, which
only ever scales a too-WIDE name down. Reuses the exact ATR reading
`_volatility_multiplier` already computes (`engine.atr_by_symbol`), placed
in `_attempt_entry` right where `halt_check` already sits, same fail-open
convention. Tier 2 (changes which signals survive, not the signal itself).
8 tests in `tests/test_risk_tier2.py` section R3b.

**`trading.catalyst_watch`** (enabled, OBSERVATION ONLY): counts overnight
headlines per watchlist symbol via Alpaca's own News API
(`AlpacaBroker.get_overnight_news`, Benzinga-sourced, free with the
account already in use - no new vendor or credential) and journals
`catalyst_news_count` on every signal_journal row - the same "measure
first" path the regime column took before anything acted on it. One
network call per session, never per-symbol. Does NOT filter, rank or size
anything - that would be a Tier 1 change (changes the signal itself) and
needs real evidence first, which does not exist yet (zero days of data as
of this writing). 16 tests in `tests/test_catalyst_watch.py` (mocked
NewsClient, no real network calls in CI) plus wiring checks in
`tests/test_journal.py` section H.

**Important limitation, told to the user directly**: this session (Claude)
is not present when the bot actually runs pre-market on its own schedule -
the news fetch has to be a fully automated API call the bot makes itself,
which is what `get_overnight_news` is. There is no mechanism for Claude to
personally supply "live" news at runtime; Alpaca's News API is the
automated substitute.

**Next step, explicit**: let catalyst_news_count collect for at least the
2-week bar this codebase has used for every other new observational factor
(short_signal_journal, the regime column) before concluding whether it
predicts anything here - check with `ops/analyze-journal.py`-style rho
analysis once enough days exist, the same way `cf_exhaustion` and the
other continuation factors were evaluated.

Full suite: 2931 pass, 0 fail.

## 20. TODO (not started) — "clean levels": premarket/prior-day H/L as stop references

User request, explicitly deferred to a todo rather than built now. Current
stops are purely percentage-based (`first_exit_loss_pct`, `trailing_stop_pct`,
etc.) - the "stocks in play" framework instead argues for risk defined
against actual price levels (premarket high/low, prior day's high/low,
VWAP - VWAP is the one of these already in use). This is a bigger fork
than a config tweak: it would mean a second, level-based stop mechanism
alongside or instead of the percentage ladder, needs its own design
conversation (which level, for which exit tier, how it interacts with
the existing dynamic-stop tightening) before any code gets written. Tier 1
under CLAUDE.md if it changes what counts as a stop-out (changes the
signal/exit logic), not a quick add.

**Will it be profitable - honest answer, asked directly by the user.** No
way to know in advance, and I said so rather than promise it. What can be
said: percentage stops and level-based stops aren't competing claims about
which is "right" - they're different ways of encoding the same underlying
belief about where a trade is wrong, and for a bot holding positions for
minutes, not hours, the gap between the two is usually small (a 0.7% stop
and "below the premarket low" often land within a few cents of each other
intraday). The most likely real benefit isn't bigger average stops, it's
fewer BAD stops - the ones where a percentage figure gets clipped by
ordinary noise sitting right at a level that would have held. That is a
testable claim, not a certainty: it would need the same one-variable,
held-week discipline as any other exit change (exits are replayable
per CLAUDE.md, so this one doesn't even need to wait for live data -
`ops/replay.py` could compare the two stop styles against recorded price
paths once levels are computed). Worth running that replay comparison
BEFORE writing any live code, since the answer might come back "no real
difference," which would settle this without ever touching `_attempt_entry`.

## 21. 2026-10-06 — first live trading day since all of items 13/16/19's fixes/features deployed

**The short-exit fix (item 13) is now confirmed under real load, not just absent load.** 10 short positions traded today (first bearish-regime window since the fix went live) - every one closed in exactly ONE leg (TRAILING_STOP/FINAL_EXIT/GAP_EXIT), no multi-leg chains, no doubling, zero ORPHAN_RECONCILE. Monday (10-05) had zero shorts at all, so zero orphans that day was inconclusive by construction - today is the real confirmation.

**Catalyst watch (item 19) confirmed working end to end**: `CATALYST WATCH: 6 of 28 watchlist symbols have overnight news` logged at session start - the Alpaca News API integration works in production, not just in mocked tests.

**ATR floor (item 19) saw zero rejections today** - not a bug, checked: `DYNAMIC STOPS enabled - ATR for 179 symbol(s)` confirms the engine had data; every candidate that reached the check already cleared 0.5% ATR, consistent with a momentum screener naturally selecting already-volatile names. Floor may simply rarely bind given what this screener selects for - watch over more days before concluding it's inert vs. just not load-bearing yet.

**The day itself was rough**: -$510.67, 84 positions, 29% win rate, and unlike 10-05 the payoff ratio wasn't there to compensate (avg win $14.50 vs avg loss $14.80, roughly even - not the 1.6x cushion 10-05 had). Longs -$283.62 (103 tranches), shorts -$227.04 (10 tranches, see above - mechanically clean, just not profitable today).

**The edge streak is now 4 consecutive negative days**: 10-01 -0.194pp, 10-02 -0.169pp, 10-05 -0.166pp, 10-06 -0.270pp. This is the first day under the `neutral_multiplier: 0.25` experiment (item 18), and edge got WORSE, not better - though the experiment is sized for neutral-regime trades specifically (8 trades today, -$13.88, roughly breakeven, far too small a sample on its own), while the bulk of today's loss was in BULLISH-regime trades (104 of the pooled trade_context rows), a different bucket the experiment doesn't touch. One day does not contradict the experiment's premise, but also doesn't yet support it - keep watching, don't conclude from day 1.

Chart artifact updated with today's narrative and the still-active experiment banner (day 1 of the planned week).
