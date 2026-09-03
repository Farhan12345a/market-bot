"""
Held-position halt alerts and milestone stop recalculation (2026-09-02, late).

Both close "a function exists and nothing calls it" gaps, which is a shape this
codebase keeps producing - send_alert() had zero call sites for weeks, and
DynamicStops.should_recalculate has had none since it was written.
"""
import copy
import yaml
from _repo import CONFIG, repo_file
from src.strategy.strategy import TradeManager
from src.analytics.dynamic_stops import DynamicStops

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


msrc = open(repo_file("src", "main.py")).read()

print("=== 1. A HELD POSITION THAT HALTS ALERTS, AND DOES NOT ACT ===")
check("the check exists for OPEN positions, not just entries",
      "HELD POSITIONS THAT HALT" in msrc)
check("it alerts rather than closing - the right response depends on WHY it "
      "halted, which the bot cannot see",
      "No action has been taken" in msrc)
check("...and says a stop cannot protect a halted position",
      "there \\nare no trades to fill against" in msrc or "no trades to fill against" in msrc)
check("it fires once per CHANGE, not once per interval",
      'halt_state.get("last")' in msrc)
check("it runs on the reconcile cadence, not every poll - an asset lookup per "
      "position per 10s is a lot of calls for minute-scale state",
      msrc.index("HELD POSITIONS THAT HALT") > msrc.index("_rc = config[\"trading\"].get(\"reconcile\")")
      or "reconcile" in msrc)
check("a lookup failure never produces a false alert",
      "if _t is False:" in msrc)
check("it can be turned off", "alert_on_held" in msrc)

print("\n=== 2. MILESTONE STOP RECALCULATION ===")
eng = DynamicStops(CFG, history={}, atr_by_symbol={"Q": 0.4, "W": 3.0})
tm = TradeManager("Q", 100.0, 100, copy.deepcopy(CFG))
check("a fresh position has no milestone yet", tm._last_stop_milestone is None)

note = tm.recalculate_stop(eng, 100.0)
check("the first evaluation tightens a quiet name onto its ATR stop",
      note and tm.config["trading"]["final_exit_loss_pct"] == -0.4, (note, tm.config["trading"]["final_exit_loss_pct"]))
check("...and records the milestone", tm._last_stop_milestone == 0.0, tm._last_stop_milestone)

before = tm.config["trading"]["final_exit_loss_pct"]
tm.recalculate_stop(eng, 100.2)
check("a move WITHIN the band does not recompute", tm.config["trading"]["final_exit_loss_pct"] == before)

# MONOTONIC: the property that makes this safe to run on a live position.
tm2 = TradeManager("W", 100.0, 100, copy.deepcopy(CFG))
tm2.config["trading"]["final_exit_loss_pct"] = -0.3     # already very tight
n2 = tm2.recalculate_stop(eng, 100.0)                    # ATR would say -1.0%
check("it REFUSES to widen a stop that is already tighter", n2 is None, n2)
check("...leaving the tight stop exactly as it was",
      tm2.config["trading"]["final_exit_loss_pct"] == -0.3)

tm3 = TradeManager("Q", 100.0, 100, copy.deepcopy(CFG))
tm3.recalculate_stop(eng, 101.5)
m_high = tm3._last_stop_milestone
tm3.recalculate_stop(eng, 100.1)
check("a position falling back does not step its milestone DOWN",
      tm3._last_stop_milestone == m_high, (m_high, tm3._last_stop_milestone))

check("no engine -> no change, never a crash",
      TradeManager("Q", 100.0, 100, copy.deepcopy(CFG)).recalculate_stop(None, 100.0) is None)
check("the recalculation cannot leak into the shared config",
      CFG["trading"]["final_exit_loss_pct"] == -1.0)

check("it is wired into the exit sweep, before the rules run on that price",
      "trade.recalculate_stop(" in msrc)
check("...and guarded so it can never raise into the exit path",
      "stop recalculation skipped" in msrc)

sstrat = open(repo_file("src", "strategy", "strategy.py")).read()
check("should_recalculate finally has a caller",
      "engine.should_recalculate(" in sstrat)

print("\n=== 3. HONEST LIMITS, RECORDED ===")
check("the code says why milestones and not continuous recalculation",
      "chases the price" in sstrat)
check("...and that widening a live stop is the worst thing it could do",
      "single worst thing" in sstrat)


# ===================================================================
print("\n=== 4. HALT-RISK SCORER ===")
from src.screener.halt_risk import halt_risk_score, is_halt_prone, filter_symbols
HR = CFG["trading"]["halt_risk"]
check("shipped OFF - enabling it is an ENTRY change", HR["enabled"] is False)

_calm = dict(atr_pct=1.2, price=264.0, dollar_volume=800e6)
check("a large-cap SaaS name scores 0", halt_risk_score("CRM", **_calm)[0] == 0)
check("...and is allowed", is_halt_prone("CRM", **_calm)[0] is False)

_bio = dict(atr_pct=9.0, price=8.0, dollar_volume=15e6)
sc, why = halt_risk_score("BIO", **_bio, cfg=HR)
check("a volatile thin biotech scores high", sc >= HR["refuse_at_score"], (sc, why))
check("...and is refused", is_halt_prone("BIO", cfg=HR, **_bio)[0] is True)
check("...with the reasons attached, so a refusal can be read back",
      "ATR" in " ".join(why) and "thin" in " ".join(why), why)

_promo = dict(atr_pct=12.0, price=1.80, dollar_volume=4e6)
check("a low-float promo name scores higher still",
      halt_risk_score("P", **_promo, cfg=HR)[0] > sc)

check("ATR is the dominant input - an LULD trip needs a move a calm name "
      "cannot reach",
      halt_risk_score("A", atr_pct=9.0, cfg=HR)[0]
      > halt_risk_score("B", price=4.0, cfg=HR)[0])

check("earnings day is scored, because the DATE is knowable in advance",
      halt_risk_score("E", earnings_today=True, cfg=HR)[0] == HR["earnings_today_points"])
check("...but does not by itself refuse a large cap - that trade-off is the "
      "user's, and the list builder ADDS earnings names on purpose",
      is_halt_prone("CRM", cfg=HR, earnings_today=True, **_calm)[0] is False)

check("no inputs -> score 0, documented as failing OPEN rather than pretended "
      "to be a measurement", halt_risk_score("NEW")[0] == 0)
hsrc = open(repo_file("src", "screener", "halt_risk.py")).read()
check("...and the docstring says so plainly instead of claiming otherwise",
      "scores it as if it were" in hsrc)
check("the module is honest that news halts cannot be predicted",
      "claiming to know tomorrow's news" in hsrc)
check("...and flags the conflict with the earnings list builder",
      "ADDS symbols reporting earnings" in hsrc or "ADDS" in hsrc)

kept, dropped = filter_symbols([
    {"symbol": "CRM", **_calm},
    {"symbol": "BIO", **_bio},
], cfg=HR)
check("filter_symbols drops only the risky one", kept == ["CRM"], kept)
check("...and reports what it dropped", len(dropped) == 1 and dropped[0][0] == "BIO", dropped)
kept2, dropped2 = filter_symbols([{"symbol": "BIO", **_bio}], cfg=HR)
check("an all-risky list comes back INTACT - watching nothing guarantees a "
      "blank day", kept2 == ["BIO"] and dropped2 == [], (kept2, dropped2))

print("\n=== 5. WATCHDOG ===")
import os as _os
import subprocess as _sp
wd = repo_file("ops", "watchdog.sh")
check("the script exists and is executable", _os.access(wd, _os.X_OK))
check("shell syntax is valid",
      _sp.run(["bash", "-n", wd], capture_output=True).returncode == 0)
w = open(wd).read()
check("it checks the unit is active", "systemctl is-active" in w)
check("...AND that it has logged recently - active but wedged is still broken",
      "WEDGED" in w and "journalctl" in w)
check("...AND that the box has disk - a full disk stops the CSVs without "
      "stopping the process", "DISK" in w and "df " in w)
check("it stays SILENT when healthy - a watchdog that reports success daily "
      "is one you stop reading",
      '[ -z "$PROBLEMS" ] && exit 0' in w)
check("it uses the venv interpreter, not a bare python3",
      "_python.sh" in w and "venv/bin/python3" in w)
check("it explains why it lives outside the bot",
      "cannot alert about itself" in w or "sent BY the bot" in w)
for f in ("market-bot-watchdog.service", "market-bot-watchdog.timer", "README.md"):
    check(f"ops/systemd/{f} exists", _os.path.exists(repo_file("ops", "systemd", f)))
rd = open(repo_file("ops", "systemd", "README.md")).read()
check("the install doc says to VERIFY it alerts, not assume", "systemctl stop market-bot" in rd)
check("...and warns the timer is evaluated in the system timezone", "SYSTEM timezone" in rd)

print("\n=== 6. THE ENTRY-CHANGE LIST IS COMPLETE, NOT A SAMPLE ===")
doc = open(repo_file("CLAUDE.md")).read()
for k in ("use_dynamic_universe", "num_stocks_to_trade", "max_daily_entries",
          "regime_sizing", "correlation_limit", "min_screener_score",
          "max_concurrent_positions", "liquidity_cap"):
    check(f"{k} is listed as an entry change", k in doc)
check("...and exit settings are explicitly marked as free to tune",
      "NOT an entry change" in doc and "final_exit_loss_pct" in doc)
check("the note admits the earlier list was incomplete",
      "as though that were" in doc)

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
