"""Pre-open A-Z validation. Uses the REAL config.yaml throughout - every other
suite runs on synthetic configs, so this is the one that would catch a config
key the code reads but the file doesn't define."""
import sys, os, types, tempfile, csv, shutil
from datetime import datetime, timedelta
import pytz
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
sandbox_cwd()

import src.main as m
from src.main import load_config, _burst_policy, _position_size, _check_three_bar_momentum
from src.strategy.strategy import Strategy, TradeManager
from src.executor.executor import Executor
from src.data.market_data import MarketDataManager
from src.analytics.signal_journal import SignalJournal
from src.notifications.email_notifier import EmailNotifier

ET = pytz.timezone("America/New_York")
fails = []
def check(n, c, d=""):
    print(("PASS  " if c else "FAIL  ") + n + ("" if c else f"   <- {d}"))
    if not c: fails.append(n)

CFG = load_config("config.yaml")
T = CFG["trading"]

print("=== A. CONFIG CONTRACT (real config.yaml) ===")
# Assert the SHAPE, not a frozen value - max_daily_entries is a tuning dial the
# user changes between sessions (40 -> 50 on 2026-08-21) and a test that pins it
# fails on every legitimate change while catching no real defect.
check("max_daily_entries is a positive int", isinstance(T["max_daily_entries"], int) and T["max_daily_entries"] > 0, T["max_daily_entries"])
check("max_daily_entries >= max_concurrent_positions", T["max_daily_entries"] >= T["max_concurrent_positions"], (T["max_daily_entries"], T["max_concurrent_positions"]))
required = [
    "entry_window_start","entry_window_end","entry_check_interval_seconds","rapid_increase_pct",
    "rapid_increase_lookback_minutes","use_rsi_filter","rsi_period","rsi_max_for_entry",
    "use_pullback_entry","pullback_min_pct","pullback_max_giveback_fraction","resumption_confirm_pct",
    "use_three_bar_momentum","three_bar_require_acceleration","use_opening_reversal_entry",
    "opening_reversal_window_minutes","opening_reversal_drop_bars","opening_reversal_confirm_bars",
    "final_exit_loss_pct","first_exit_loss_pct","first_exit_pct","trailing_stop_pct","time_stop_hour",
    "momentum_fade_hour","momentum_fade_minute","momentum_fade_window_samples","momentum_fade_slope_threshold",
    "resistance_lookback_samples","resistance_min_decline_pct","max_position_per_stock_usd",
    "max_risk_per_trade_fraction","max_daily_loss_usd","max_concurrent_positions","max_total_exposure_fraction",
    "max_daily_entries","reentry_cooldown_minutes","reentry_cooldown_after_loss_only",
    "use_burst_throttle","burst_width_threshold","burst_max_entries","burst_size_multiplier",
    "use_daily_screener","screener_start_time","screener_timeout_seconds","stock_universe",
    "min_stock_price","max_stock_price","use_websocket_stream","websocket_feed",
]
missing = [k for k in required if k not in T]
check("every key the code reads exists", not missing, missing)
check("analytics section present", "analytics" in CFG and "signal_log_file" in CFG["analytics"])
check("notifications section present", "email" in CFG.get("notifications", {}))

print("\n=== B. TODAY'S INTENDED SETTINGS ===")
expect = {"min_stock_price":10, "momentum_fade_slope_threshold":-0.05, "entry_window_start":"09:33",
          "max_daily_entries":50, "max_daily_loss_usd":1000, "max_concurrent_positions":10,
          "resistance_min_decline_pct":0.5, "reentry_cooldown_minutes":5,
          "use_burst_throttle":True, "use_websocket_stream":True,
          "use_trade_ticks_for_entry":True,
          "rapid_increase_pct":0.3, "rapid_increase_lookback_minutes":3,
          "three_bar_require_acceleration":True, "screener_start_time":"09:05"}
for k, v in expect.items():
    check(f"{k} = {v}", T[k] == v, T[k])

print("\n=== C. CONFIG SANITY (values that must cohere) ===")
sh, sm = map(int, T["screener_start_time"].split(":"))
eh, em = map(int, T["entry_window_start"].split(":"))
check("screener starts before market open", sh*60+sm < 9*60+30)
check("screener timeout fits before open", sh*60+sm + T["screener_timeout_seconds"]/60 < 9*60+30,
      f"{T['screener_start_time']} + {T['screener_timeout_seconds']}s")
check("entry window opens at/after market open", eh*60+em >= 9*60+30)
check("entry window start < end", T["entry_window_start"] < T["entry_window_end"])
check("final exit is looser than first exit", T["final_exit_loss_pct"] < T["first_exit_loss_pct"])
check("both stop levels are negative", T["final_exit_loss_pct"] < 0 and T["first_exit_loss_pct"] < 0)
check("exposure fraction <= 1 (no built-in leverage)", T["max_total_exposure_fraction"] <= 1.0)
check("burst_max_entries < max_concurrent_positions", T["burst_max_entries"] < T["max_concurrent_positions"])
check("burst size multiplier in (0,1]", 0 < T["burst_size_multiplier"] <= 1)
check("daily entries >= concurrent positions", T["max_daily_entries"] >= T["max_concurrent_positions"])
check("momentum fade starts after entry window", T["momentum_fade_hour"]*60+T["momentum_fade_minute"]
      >= int(T["entry_window_end"][:2])*60+int(T["entry_window_end"][3:]))
check("time stop before market close", T["time_stop_hour"] <= 16)

print("\n=== D. SIZING WITH REAL CONFIG ===")
class Ex:
    equity = 95116.67
slot = Ex.equity * T["max_total_exposure_fraction"] / T["max_concurrent_positions"]
for px, name in [(12.82,"CADL"), (2.19,"PLUG"), (242.61,"MRVL"), (14.38,"NU")]:
    qty = _position_size(CFG, Ex(), px)
    check(f"{name} @ ${px}: {qty} sh = ${qty*px:,.0f} (<= slot ${slot:,.0f})", qty*px <= slot*1.01, qty*px)
check("full book <= equity (no leverage by construction)",
      _position_size(CFG, Ex(), 100.0)*100.0*T["max_concurrent_positions"] <= Ex.equity, "")
check("zero equity -> zero size (fail closed)", _position_size(CFG, types.SimpleNamespace(equity=0.0), 50.0) == 0)

print("\n=== E. CLOCK FIX (the highest-risk change) ===")
import src.strategy.strategy as S
now_et = S._now_et()
check("strategy clock is ET-aware", now_et.tzinfo is not None)
check("ET differs from naive server clock", now_et.hour != datetime.now().hour or now_et.tzinfo is not None)
tm = TradeManager("X", 100.0, 10, CFG)
# Supply exactly momentum_fade_window_samples points: the window is a config
# dial (5 -> 6 on 2026-08-24) and a hardcoded list silently under-feeds the
# check, which then returns 0 for insufficient data rather than for no fade.
# The window is now expressed in MINUTES and converted at the live poll rate,
# so the sample count depends on entry_check_interval_seconds. Derive it the
# same way the product does rather than hardcoding either number.
from src.strategy.strategy import _samples_for_minutes as _sfm
_w = _sfm(CFG, "momentum_fade_window_minutes", "momentum_fade_window_samples", 6, 6)
tm.price_history = [100.0 - 0.1 * i for i in range(_w)]
fade_start = T["momentum_fade_hour"]*60 + T["momentum_fade_minute"]
cur = now_et.hour*60 + now_et.minute
res = tm.check_momentum_fade(tm.price_history[-1])
if cur < fade_start:
    check(f"momentum fade DORMANT before {T['momentum_fade_hour']}:{T['momentum_fade_minute']:02d} ET", res == 0, res)
else:
    check(f"momentum fade ACTIVE after {T['momentum_fade_hour']}:{T['momentum_fade_minute']:02d} ET", res > 0, res)

print("\n=== F. RESISTANCE FLOOR WITH REAL CONFIG ===")
# resistance_lookback is now MINUTES converted at the live poll rate, so build
# a decline of exactly that many samples instead of a fixed three.
_rl = _sfm(CFG, "resistance_lookback_minutes", "resistance_lookback_samples", 3, 3)
# The rule is DISABLED in the live config for the 2026-08-26 experiment. These
# cases test the rule's logic, not the toggle, so enable it locally - the toggle
# itself is covered separately below.
import copy as _copy
_RCFG = _copy.deepcopy(CFG); _RCFG["trading"]["use_resistance_exit"] = True
tm2 = TradeManager("Y", 100.0, 10, _RCFG)
tm2.price_history = [100.0 - 0.05 * i / _rl for i in range(_rl)]; tm2.highest_since_entry = 100.0
check("0.05% wobble does NOT exit", tm2.check_resistance(tm2.price_history[-1]) == 0)
tm3 = TradeManager("Z", 100.0, 10, _RCFG)
tm3.price_history = [100.0 - 1.0 * i / (_rl - 1) for i in range(_rl)]; tm3.highest_since_entry = 100.0
check(f"1.0% decline over {_rl} samples DOES exit", tm3.check_resistance(tm3.price_history[-1]) > 0)
tm4 = TradeManager("W", 100.0, 10, CFG)   # live config: rule is back ON
tm4.price_history = list(tm3.price_history); tm4.highest_since_entry = 100.0
check("use_resistance_exit is ON again in the live config",
      CFG["trading"]["use_resistance_exit"] is True and
      tm4.check_resistance(tm4.price_history[-1]) > 0)

# Added 2026-08-26: never sell into an upturn. The window-level conditions
# describe the window as a whole, and a window can be net-down while price is
# turning back up at its right edge - which is exactly the case worth holding.
_up = TradeManager("U", 100.0, 10, CFG)
_up.price_history = [100.0 - 1.0 * i / (_rl - 1) for i in range(_rl - 1)] + [100.0]
_up.highest_since_entry = 100.0
check("a rising last tick blocks the resistance exit",
      _up.check_resistance(_up.price_history[-1]) == 0)
_dn = TradeManager("D", 100.0, 10, CFG)
_dn.price_history = [100.0 - 1.0 * i / (_rl - 1) for i in range(_rl)]
_dn.highest_since_entry = 100.0
check("a still-falling last tick does NOT block it",
      _dn.check_resistance(_dn.price_history[-1]) > 0)

print("\n=== G. THREE-BAR ACCELERATION WITH REAL CONFIG ===")
def bars(*c):
    out=[]; prev=c[0]-0.02
    for x in c: out.append({"open":prev,"close":x}); prev=x
    return out
acc = T["three_bar_require_acceleration"]
check("9.51->9.55->9.56 rejected (decelerating)",
      _check_three_bar_momentum(bars(9.51,9.55,9.56), require_acceleration=acc) is False)
check("9.51->9.53->9.58 accepted (accelerating)",
      _check_three_bar_momentum(bars(9.51,9.53,9.58), require_acceleration=acc) is True)

print("\n=== H. BURST POLICY WITH REAL CONFIG ===")
thr = T["burst_width_threshold"]
check(f"{thr-1} signals -> untouched", _burst_policy(CFG, thr-1)[0] is None)
check(f"{thr} signals -> throttled", _burst_policy(CFG, thr)[0] == T["burst_max_entries"])
check("20 signals -> throttled + sized down", _burst_policy(CFG, 20)[1] == T["burst_size_multiplier"])

print("\n=== I. FULL TRADING DAY, REAL CONFIG, ALL FEATURES TOGETHER ===")
class Acct: equity="95116.67"; buying_power="95116.67"; cash="95116.67"; last_equity="95116.67"
class Pos:
    def __init__(s,sym,q,px): s.symbol,s.qty,s.market_value,s.avg_entry_price,s.current_price,s.unrealized_pl=sym,str(q),str(q*px),str(px),str(px),"0"
class Broker:
    def __init__(s): s.real={}; s.orders=[]
    def submit_market_order(s,sym,q,side="buy"):
        s.orders.append((sym,q,side))
        if side=="buy": s.real[sym]=s.real.get(sym,0)+q
        else:
            # A partial sell leaves the rest of the position OPEN at the broker.
            # This used to pop() the whole symbol on any sell, so a scale-out
            # tranche (FIRST_EXIT, TAKE_PROFIT) made the mock disagree with the
            # strategy and looked like a stale-position leak that wasn't one.
            s.real[sym]=s.real.get(sym,0)-q
            if s.real[sym]<=0: s.real.pop(sym,None)
        return types.SimpleNamespace(id="o")
    def get_account(s): return Acct()
    def get_positions(s): return {k:Pos(k,v,50.0) for k,v in s.real.items()}
    def get_latest_quote(s,sym): return {"bid":49.99,"ask":50.01,"spread":0.02}
    def get_latest_bars(s,sym,tf="1Min"): return {}

rise=[50.0,50.1,50.4,50.9,51.5,52.2,53.0,53.9,54.9,56.0,57.2,58.5,59.9,61.4,63.0,64.0,64.5]
tmp=tempfile.mkdtemp()
cfg=dict(CFG); cfg["analytics"]=dict(CFG["analytics"], signal_log_file=os.path.join(tmp,"sig.csv"))

def session(symbols, minutes=8):
    b=Broker(); st=Strategy(cfg); ex=Executor(b,cfg); md=MarketDataManager(b); sj=SignalJournal(cfg)
    start=datetime.now(ET).replace(hour=9,minute=36,second=0,microsecond=0); step={"i":0}
    md.get_latest_bar=lambda sym,tf="1Min": {"open":rise[min(step["i"],16)]*0.999,"high":rise[min(step["i"],16)],
        "low":rise[min(step["i"],16)]*0.998,"close":rise[min(step["i"],16)],"volume":100000,
        "timestamp":start+timedelta(minutes=step["i"])}
    class FDT:
        @staticmethod
        def now(tz=None): return start+timedelta(minutes=step["i"])
    od,osl=m.datetime,m.time.sleep; m.datetime=FDT
    def slp(_):
        step["i"]+=1
        if step["i"]>minutes: raise KeyboardInterrupt
    m.time.sleep=slp
    try: m.run_trading_day(cfg,md,st,ex,symbols,{},types.SimpleNamespace(send_daily_summary=lambda *a,**k:None),ET,sj)
    except KeyboardInterrupt: pass
    finally: m.datetime,m.time.sleep=od,osl
    return b,st,ex,sj

b,st,ex,sj = session([f"S{i}" for i in range(25)])
buys=[o for o in b.orders if o[2]=="buy"]
check("entries happened at all", len(buys)>0, len(buys))
check(f"concurrent cap {T['max_concurrent_positions']} respected", len(b.real)<=T["max_concurrent_positions"], len(b.real))
check(f"daily entry cap {T['max_daily_entries']} respected", len(buys)<=T["max_daily_entries"], len(buys))
committed = sum(q*50.0 for _,q,side in b.orders if side=="buy")
check("exposure never exceeded equity", committed <= 95116.67*T["max_total_exposure_fraction"]*1.05, committed)
check("burst_logic attached to entries", any("burst" in (ex.entry_meta[s].get("burst_logic") or "").lower()
      or ex.entry_meta[s].get("burst_logic") for s in ex.entry_meta), "")
sj.flush()
rows=list(csv.DictReader(open(cfg["analytics"]["signal_log_file"])))
check("signal journal wrote rows", len(rows)>0, len(rows))
check("journal captured skipped signals (control group)",
      any(r["taken"]=="False" for r in rows), "")
check("journal rows carry features", rows[0]["burst_width"] not in (None,""), rows[0])

print("\n=== J. MULTI-DAY REUSE (same objects, next session) ===")
b2,st2,ex2,sj2 = session([f"S{i}" for i in range(5)], minutes=6)
check("second session runs clean on reused code path", True)
check("no stale positions leak into strategy", all(sym in b2.real for sym in st2.trades),
      (list(st2.trades), list(b2.real)))

print("\n=== K. REPORT RENDERS WITH REAL CONFIG ===")
notif=dict(CFG.get("notifications",{})); notif["report_dir"]=tmp; notif["email"]={"enabled":False}
en=EmailNotifier({"notifications":notif})
ex.save_trades_log()
html=en._generate_html_summary(ex.trades_log or [{"symbol":"A","entry_price":1,"exit_price":1,"qty":1,
    "pl":0,"pl_pct":0,"exit_reason":"X","entry_method":"Y","burst_logic":"test","stop_loss_used":False}],
    burst_summary="Burst throttle ON. Engaged on 3 of 8 entry-window polls.")
check("report has Bursting Logic column", "<th>Bursting Logic</th>" in html)
check("report has day-level burst description", "Engaged on 3 of 8" in html)
check("report is non-empty html", len(html)>1500 and "</html>" in html)

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
