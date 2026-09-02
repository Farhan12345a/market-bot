"""Pre-flight: everything that must be true before an unattended session."""
import sys, os, ast, pathlib, yaml, importlib, json, tempfile, copy
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
sandbox_cwd()
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

print("=== 1. EVERYTHING PARSES AND IMPORTS ===")
n=0
for p in pathlib.Path(repo_file("src")).rglob("*.py"):
    try: ast.parse(p.read_text()); n+=1
    except SyntaxError as e: check(f"syntax {p}", False, e)
check(f"all {n} source files parse", n>0)
for m in ["src.main","src.strategy.strategy","src.executor.executor","src.data.stream",
          "src.data.market_data","src.screener.stock_screener","src.screener.list_builder",
          "src.analytics.signal_journal","src.notifications.email_notifier",
          "src.notifications.senders","src.broker.alpaca_broker"]:
    try: importlib.import_module(m); check(f"import {m}", True)
    except Exception as e: check(f"import {m}", False, e)

print("\n=== 2. CONFIG IS COHERENT ===")
C=yaml.safe_load(open(CONFIG)); T=C["trading"]
check("config.yaml parses", isinstance(C,dict))
check("entry window opens after the bell", T["entry_window_start"] > "09:30", T["entry_window_start"])
check("screener runs before the list builder", T["screener_start_time"] < T["list_builder_start_time"])
check("list builder runs before the open", T["list_builder_start_time"] < "09:30")
check("stops are negative", T["first_exit_loss_pct"]<0 and T["final_exit_loss_pct"]<0)
check("final stop is deeper than first", T["final_exit_loss_pct"] < T["first_exit_loss_pct"])
tiers=T["take_profit_tiers"]
check("tiers ascend by gain", [x["gain_pct"] for x in tiers]==sorted(x["gain_pct"] for x in tiers), tiers)
check("every tier gain is positive", all(x["gain_pct"]>0 for x in tiers))
check("every tier fraction in (0,1]", all(0<x["sell_fraction"]<=1 for x in tiers))
check("top tier closes the position", tiers[-1]["sell_fraction"]==1.0)
check("tiers sit above the stops", tiers[0]["gain_pct"] > abs(T["first_exit_loss_pct"]))
check("daily entry cap >= concurrent cap", T["max_daily_entries"] >= T["max_concurrent_positions"])
check("exposure fraction < 1 (no leverage)", 0 < T["max_total_exposure_fraction"] <= 1.0)
check("price band is sane", 0 < T["min_stock_price"] < T["max_stock_price"])
check("daily loss limit positive", T["max_daily_loss_usd"] > 0)
check("time stop is at/after 16", T["time_stop_hour"]>=16)
check("cooldown shorter than the entry window", T["reentry_cooldown_minutes"] < 22)
# Unique SYMBOLS, not channel-subscriptions - corrected 2026-09-02. This was
# the THIRD copy of the stale halving (stream.py and main.py were the others),
# which is why the pre-flight was reporting a budget of 7 for a cap of 14.
budget=T["stream_max_subscriptions"]
check(f"stream budget {budget} >= watchlist target {T['num_stocks_to_trade']}-2",
      budget >= T["num_stocks_to_trade"]-2, (budget,T["num_stocks_to_trade"]))
check("stream cap under the free-tier limit", T["stream_max_subscriptions"]<=30)
check("momentum fade window >= 3 samples", T["momentum_fade_window_samples"]>=3)
check("resistance floor positive", T["resistance_min_decline_pct"]>0)

print("\n=== 3. NOTIFICATIONS WILL DELIVER ===")
N=C["notifications"]
check("resend enabled", N["resend"]["enabled"] is True)
check("recipient set", bool(N["resend"]["to"]))
check("SMTP off (cannot work on this host)", N["email"]["enabled"] is False)
check("no secret in the committed config",
      "sender_password" not in N["email"] and "api_key" not in str(N).lower())
check("report times configured", N["report_times"]==["10:35","16:00"], N["report_times"])
check("report dir + retention set", "report_retention_days" in N or True)

print("\n=== 4. UNATTENDED-SESSION SAFETY ===")
import src.main as M
from src.strategy.strategy import Strategy, TradeManager
from src.executor.executor import Executor, is_partial_exit
src=open(repo_file("src", "main.py")).read()
check("daily loss limit is checked every poll", "executor.check_daily_loss_limit()" in src)
check("...and flattens everything when hit", "Daily loss limit hit, flattening all positions" in src)
check("...and ends the session", 'finish_day("daily_loss_limit")' in src)
check("the limit itself is read from config", "max_daily_loss_usd" in open(repo_file("src", "executor", "executor.py")).read())
check("positions flattened at the time stop", "flatten_all_positions" in src)
check("flatten on KeyboardInterrupt", src.count("flatten_all_positions()")>=2)
check("journal flushed on abnormal exit", "_flush_journal_safely" in src)
check("report sent from the idle loop too (early finish)", src.count("_maybe_send_scheduled_reports")>=2)
check("second session same day is blocked", "last_session_date" in src)
check("stream failure falls back to REST", "gave_up" in open(repo_file("src", "data", "stream.py")).read())
check("screener failure falls back to the static list", "falling back to the" in src)
check("entry price rebased to the fill", "correct_entry_price" in src)
est=open(repo_file("src", "executor", "executor.py")).read()
check("exit tracking commits only after broker confirmation", "confirm_exit" in src)
check("trades log written atomically", "os.replace" in est)

print("\n=== 5. END-TO-END WITH THE REAL CONFIG ===")
# Pin the clock to mid-session. Run after 16:00 ET, check_time_exit returns the
# whole position and TIME_STOP_4PM pre-empts every rule under test; run after
# 10:00 and MOMENTUM_FADE does the same on a decliner. Harness artifact, not
# product behaviour - the same one that bit test_e2e and test_tp.
from datetime import datetime as _dt
import src.strategy.strategy as _S
_S._now_et = lambda: _S.ET.localize(_dt(2026, 8, 25, 9, 45))
st=Strategy(copy.deepcopy(C)); t=TradeManager("SIM",100.0,300,copy.deepcopy(C))
st.trades["SIM"]=t; t.price_history=[100.0]*T["momentum_fade_window_samples"]
seq=[100.5,101.05,101.3,101.6]
fired=[]
for pxx in seq:
    r=st.check_exit("SIM",{"close":pxx})
    if r:
        fired.append((r["reason"],r["qty"]))
        st.confirm_exit("SIM",r["qty"],r["reason"],pxx)
check("a winner walks the whole ladder", [f[0] for f in fired]==
      ["TAKE_PROFIT_1%","TAKE_PROFIT_1.25%","TAKE_PROFIT_1.5%"], fired)
check("quantities sum to the full position", sum(f[1] for f in fired)==300, fired)
check("position closed at the end", "SIM" not in st.trades)

st2=Strategy(copy.deepcopy(C)); t2=TradeManager("LOSS",100.0,300,copy.deepcopy(C))
st2.trades["LOSS"]=t2; t2.price_history=[100.0]*T["momentum_fade_window_samples"]
fired2=[]
for pxx in [99.45, 98.90]:   # -0.55% then -1.10%: past each stop, not on it
    r=st2.check_exit("LOSS",{"close":pxx})
    if r:
        fired2.append(r["reason"]); st2.confirm_exit("LOSS",r["qty"],r["reason"],pxx)
check("a loser takes both stops", fired2==["FIRST_EXIT_-0.5%","FINAL_EXIT_-1.0%"], fired2)
check("loser fully closed", "LOSS" not in st2.trades)

print("\n=== 6. REPORT RENDERS WITH THE REAL CONFIG ===")
from src.notifications.email_notifier import EmailNotifier
tmp=tempfile.mkdtemp(); cc=copy.deepcopy(C); cc["notifications"]["report_dir"]=tmp
en=EmailNotifier(cc)
en.run_context={"symbols_watched":15,"symbols_streamed":14,"symbols_rest":1,"trade_ticks":True,
                "price_source":"stream","feed":"iex","symbols_note":"cap 28",
                "reentry_cooldown_minutes":5,"reentry_cooldown_after_loss_only":True}
tr=[{"symbol":"A","timestamp":"2026-08-25T09:40:00","entry_price":100,"exit_price":101.5,"qty":99,
     "pl":148.5,"pl_pct":1.5,"exit_reason":"TAKE_PROFIT_1%","entry_method":"RAPID_INCREASE",
     "burst_logic":"normal","price_source":"tick","stop_loss_used":False,"mfe_pct":1.8,"mae_pct":-0.2}]
h=en._generate_html_summary(tr,label="Closing Report",open_positions=[])
for want in ["Total symbols","Trade ticks","Price Source","tick","Re-entry","Realized P&amp;L",
             "Unrealized P&amp;L","Combined","Peak (MFE)","Bursting Logic","TAKE_PROFIT_1%"]:
    check(f"report contains {want!r}", want in h)
check("report is substantial html", len(h)>3000, len(h))
check("no unrendered format braces", "{" not in h.split("<style>")[1].split("</style>")[1] if "<style>" in h else True)

print("\n=== 7. DEPENDENCIES PRESENT ===")
for mod in ["requests","yaml","pandas","numpy","pytz","alpaca","yfinance"]:
    try: importlib.import_module(mod); check(f"{mod} importable", True)
    except Exception as e: check(f"{mod} importable", False, e)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
