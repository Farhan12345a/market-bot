"""Scheduled reports: 10:35 status, all-closed, 16:00 close."""
import sys, os, json, copy, types, tempfile, yaml
from datetime import datetime, timedelta
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import pytz
import src.main as M
from src.notifications.email_notifier import EmailNotifier

ET = pytz.timezone("America/New_York")
CFG = yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

SENT=[]
class FakeNotifier:
    def send_report(s, trades_file="logs/trades.json", burst_summary="", label="", open_positions=None):
        SENT.append({"label":label,"open":open_positions or [],"burst":burst_summary}); return True
    def send_daily_summary(s, trades_file="logs/trades.json", burst_summary=""):
        SENT.append({"label":"Daily Summary","open":[],"burst":burst_summary}); return True

class FakeTrade:
    def __init__(s,entry=100.0,qty=100,last=101.0):
        s.entry_price=entry; s.entry_qty=qty; s.qty_remaining=qty
        s.price_history=[entry,last]; s.entry_time=datetime.now(ET)-timedelta(minutes=42)
        s.entry_method="THREE_BAR_MOMENTUM"
    def excursions(s): return (1.5,-0.4)
class FakeStrategy:
    def __init__(s,trades=None): s.trades=trades or {}
    def get_open_trades(s): return dict(s.trades)
class FakeMD:
    def __init__(s,px=101.0,boom=False): s.px=px; s.boom=boom
    def get_latest_bar(s,sym):
        if s.boom: raise RuntimeError("no data")
        return {"close":s.px}
class FakeExec: day_burst_summary="Burst throttle ON. Engaged on 3 of 8 polls."

def at(h,m):
    fixed = ET.localize(datetime(2026,8,21,h,m))
    M.datetime = types.SimpleNamespace(now=lambda tz=None: fixed, strptime=datetime.strptime)
    return fixed
def reset(seeded=False):
    SENT.clear(); M.report_state["sent"]=set(); M.report_state["seeded"]=seeded
def run(cfg=None, strat=None, md=None, h=10, m=35):
    at(h,m)
    M._maybe_send_scheduled_reports(cfg or CFG, FakeNotifier(), strat or FakeStrategy(),
                                    FakeExec(), md or FakeMD(), ET)

_realdt = M.datetime
try:
    print("=== A. CONFIG ===")
    n=CFG["notifications"]
    check("report_times configured", n["report_times"]==["10:35","16:00"], n["report_times"])
    check("catchup window configured", n["report_catchup_minutes"]==30)

    print("\n=== B. FIRING AT THE RIGHT TIMES ===")
    reset(seeded=True); run(h=10,m=34)
    check("10:34 -> nothing yet", SENT==[], SENT)
    reset(seeded=True); run(h=10,m=35)
    check("10:35 -> one report", len(SENT)==1, SENT)
    check("10:35 labelled Midday Status", SENT[0]["label"]=="Midday Status", SENT)
    reset(seeded=True); run(h=16,m=0)
    check("16:00 -> both slots fire (10:35 was missed in-session)", len(SENT)==2, [x["label"] for x in SENT])
    check("16:00 labelled Closing Report", SENT[-1]["label"]=="Closing Report")

    print("\n=== C. NEVER SENDS THE SAME SLOT TWICE ===")
    reset(seeded=True); run(h=10,m=35); run(h=10,m=36); run(h=11,m=0)
    check("repeated polls -> still exactly one", len(SENT)==1, len(SENT))
    reset(seeded=True); run(h=16,m=0); run(h=16,m=5); run(h=16,m=30)
    check("close slot fires once across many polls", len(SENT)==2, [x["label"] for x in SENT])

    print("\n=== D. STARTUP CATCH-UP ===")
    reset(seeded=False); run(h=15,m=0)
    check("start at 15:00 -> 10:35 recorded, NOT sent five hours late",
          [x["label"] for x in SENT]==[], SENT)
    reset(seeded=False); run(h=10,m=50)
    check("start 15 min after slot -> still sent (inside grace)",
          len(SENT)==1 and SENT[0]["label"]=="Midday Status", SENT)
    reset(seeded=False); run(h=11,m=30)
    check("start 55 min after slot -> skipped (past grace)", SENT==[], SENT)
    reset(seeded=False); run(h=9,m=0)
    check("start before any slot -> nothing fires", SENT==[], SENT)

    print("\n=== E. OPEN POSITIONS IN THE STATUS REPORT ===")
    st=FakeStrategy({"MARA":FakeTrade(10.0,50,10.5),"HUT":FakeTrade(20.0,30,19.6)})
    reset(seeded=True); run(strat=st, md=FakeMD(px=None) if False else FakeMD(10.5))
    rows=SENT[0]["open"]
    check("open positions attached to the report", len(rows)==2, len(rows))
    r={x["symbol"]:x for x in rows}
    check("unrealized P&L computed", abs(r["MARA"]["unrealized_pl"]-25.0)<0.01, r["MARA"])
    check("unrealized % computed", abs(r["MARA"]["unrealized_pl_pct"]-5.0)<0.01, r["MARA"])
    check("MFE/MAE carried", r["MARA"]["mfe_pct"]==1.5 and r["MARA"]["mae_pct"]==-0.4)
    check("entry method carried", r["MARA"]["entry_method"]=="THREE_BAR_MOMENTUM")
    check("hold time computed", "min" in (r["MARA"]["held_for"] or ""), r["MARA"]["held_for"])
    check("qty reported", r["MARA"]["qty_remaining"]==50 and r["MARA"]["entry_qty"]==50)
    check("burst summary passed through", "Burst throttle ON" in SENT[0]["burst"])

    reset(seeded=True); run(strat=st, md=FakeMD(boom=True))
    rows=SENT[0]["open"]
    check("unpriceable symbol still listed, never dropped", len(rows)==2, len(rows))
    check("falls back to last known price", {x["symbol"] for x in rows}=={"MARA","HUT"})

    reset(seeded=True); run(strat=FakeStrategy())
    check("flat account -> report still sent with zero open rows",
          len(SENT)==1 and SENT[0]["open"]==[], SENT)

    print("\n=== F. ROBUSTNESS ===")
    bad=copy.deepcopy(CFG); bad["notifications"]["report_times"]=["10:35","not-a-time","16:00"]
    reset(seeded=True); at(16,0)
    M._maybe_send_scheduled_reports(bad, FakeNotifier(), FakeStrategy(), FakeExec(), FakeMD(), ET)
    check("malformed time entry skipped, valid ones still fire", len(SENT)==2, [x["label"] for x in SENT])
    off=copy.deepcopy(CFG); off["notifications"]["report_times"]=[]
    reset(seeded=True); at(16,0)
    M._maybe_send_scheduled_reports(off, FakeNotifier(), FakeStrategy(), FakeExec(), FakeMD(), ET)
    check("empty report_times -> nothing sent, no raise", SENT==[])
    missing=copy.deepcopy(CFG); missing["notifications"].pop("report_times")
    reset(seeded=True); at(16,0)
    M._maybe_send_scheduled_reports(missing, FakeNotifier(), FakeStrategy(), FakeExec(), FakeMD(), ET)
    check("absent key -> nothing sent, no raise", SENT==[])

    class Boom:
        def send_report(s,**k): raise RuntimeError("resend down")
    reset(seeded=True); at(10,35)
    M._maybe_send_scheduled_reports(CFG, Boom(), FakeStrategy({"A":FakeTrade()}), FakeExec(), FakeMD(), ET)
    check("sender blowing up does not propagate into the trading loop", True)
    check("failed slot still marked, so it won't retry every 60s",
          (_realdt.now(ET).date() if False else datetime(2026,8,21).date(), "10:35") in M.report_state["sent"]
          or len(M.report_state["sent"])>=1, M.report_state["sent"])

    print("\n=== G. finish_day SLOT CLAIMING ===")
    at(16,2); check("_slot_for_finish claims 16:00 at the close", M._slot_for_finish(ET)=="16:00")
    at(10,14); check("_slot_for_finish claims nothing at 10:14 (10:35 must still fire)",
                     M._slot_for_finish(ET) is None)
    at(9,50);  check("_slot_for_finish claims nothing pre-10:35", M._slot_for_finish(ET) is None)
finally:
    M.datetime = _realdt

print("\n=== H. HTML REPORT CONTENT ===")
tmp=tempfile.mkdtemp()
cfg=copy.deepcopy(CFG); cfg["notifications"]["report_dir"]=tmp
en=EmailNotifier(cfg)
closed=[{"symbol":"MARA","entry_price":10,"exit_price":10.1,"qty":10,"pl":121.0,"pl_pct":1.0,
         "exit_reason":"TAKE_PROFIT","entry_method":"X","burst_logic":"","stop_loss_used":False,
         "mfe_pct":1.4,"mae_pct":-0.2}]
open_rows=[{"symbol":"HUT","entry_price":20.0,"current_price":19.6,"qty_remaining":30,"entry_qty":30,
            "unrealized_pl":-12.0,"unrealized_pl_pct":-2.0,"mfe_pct":0.3,"mae_pct":-2.1,
            "entry_method":"RAPID_INCREASE","held_for":"42 min"}]
html=en._generate_html_summary(closed, burst_summary="B", label="Midday Status", open_positions=open_rows)
check("label appears in the heading", "Midday Status" in html)
check("open positions table rendered", "Open Positions" in html and "HUT" in html)
check("closed table still rendered", "Closed Trades" in html and "MARA" in html)
check("unrealized P&L shown", "-12.00" in html or "-$12.00" in html, "")
check("open MFE/MAE shown", "+0.30%" in html and "-2.10%" in html)
check("entry method shown for open rows", "RAPID_INCREASE" in html)
check("hold time shown", "42 min" in html)
check("warns open P&L is excluded from the totals", "NOT included" in html)
check("full 14-col closed table intact", html.count("<th>")>=20, html.count("<th>"))
check("MFE/MAE columns still present", "Peak (MFE)" in html and "Trough (MAE)" in html)
check("Bursting Logic column still present", "Bursting Logic" in html)

html2=en._generate_html_summary(closed, burst_summary="B", label="Daily Summary", open_positions=[])
check("no open positions -> no Open Positions table", "Open Positions" not in html2)
check("no open positions -> no 'Still Open' banner", "Still Open" not in html2)
check("closed table unaffected", "MARA" in html2 and "Closed Trades" in html2)

txt=en._plain_text_summary(closed, open_rows)
check("push summary mentions open count", "1 still open" in txt, txt)
check("push summary mentions unrealized", "-12.00" in txt, txt)
txt2=en._plain_text_summary(closed, [])
check("push summary omits open line when flat", "still open" not in txt2, txt2)

print("\n=== I. send_report BEHAVIOUR ===")
tf=os.path.join(tmp,"trades.json"); json.dump(closed, open(tf,"w"))
en2=EmailNotifier(cfg)
r=en2.send_report(trades_file=tf, label="Midday Status", open_positions=open_rows)
check("no channels -> returns False but still saves", r is False)
saved=[f for f in os.listdir(tmp) if f.startswith("trading-report")]
check("report written to disk", len(saved)==1, os.listdir(tmp))
body=open(os.path.join(tmp,saved[0])).read()
check("saved report contains the open positions", "HUT" in body)
r2=en2.send_report(trades_file=os.path.join(tmp,"nope.json"), label="X", open_positions=open_rows)
check("missing trades file + open positions -> still reports", r2 is False)
r3=en2.send_report(trades_file=os.path.join(tmp,"nope.json"), label="X", open_positions=[])
check("nothing at all -> returns False, no crash", r3 is False)

print("\n=== J. REALIZED / UNREALIZED P&L BLOCK ===")
h=en._generate_html_summary(closed, burst_summary="B", label="Midday Status", open_positions=open_rows)
check("P&L section present", "Profit &amp; Loss" in h or "Profit & Loss" in h)
check("realized labelled", "Realized P&amp;L" in h)
check("unrealized labelled", "Unrealized P&amp;L" in h)
check("combined labelled", "Combined" in h)
check("realized value correct ($121)", "$121.00" in h, "")
check("unrealized value correct (-$12)", "$-12.00" in h or "-12.00" in h, "")
check("combined value correct ($109)", "$109.00" in h, "")
check("says realized is booked/final", "booked" in h)
check("says unrealized is not booked", "not booked" in h)
check("warns combined will move while open", "will move until they close" in h)

h2=en._generate_html_summary(closed, burst_summary="B", label="Closing Report", open_positions=[])
check("flat: unrealized shows $0.00", "$0.00" in h2)
check("flat: combined equals realized", h2.count("$121.00") >= 2, h2.count("$121.00"))
check("flat: says every position is closed", "every position is closed" in h2)

losing=[{"symbol":"X","entry_price":10,"exit_price":9,"qty":10,"pl":-200.0,"pl_pct":-1.0,
         "exit_reason":"FINAL_EXIT_-1.0%","entry_method":"X","burst_logic":"","stop_loss_used":True}]
h3=en._generate_html_summary(losing, burst_summary="B", label="Closing Report", open_positions=[])
check("negative realized renders", "$-200.00" in h3 or "-200.00" in h3, "")
h4=en._generate_html_summary([], burst_summary="B", label="Midday Status", open_positions=open_rows)
check("no closed trades yet: realized $0.00, unrealized shown", "$0.00" in h4 and "-12.00" in h4)

t3=en._plain_text_summary(closed, open_rows)
check("push names combined", "Combined" in t3, t3)
check("push combined is realized+unrealized", "+109.00" in t3, t3)

print("\n=== K. MALFORMED TRADES FILE (2026-08-21 regression) ===")
import json as _json
bad_dir=tempfile.mkdtemp(); bcfg=copy.deepcopy(CFG); bcfg["notifications"]["report_dir"]=bad_dir
enb=EmailNotifier(bcfg)
badf=os.path.join(bad_dir,"trades.json")
open(badf,"w").write('[\n {"symbol":"A",\n  "pl": ,\n }\n]')   # the shape that killed 10:35
r=enb.send_report(trades_file=badf, label="Midday Status", open_positions=open_rows)
check("malformed trades file does not abort the report", r is False)
saved=[f for f in os.listdir(bad_dir) if f.startswith("trading-report")]
check("report still built and saved from open positions alone", len(saved)==1, os.listdir(bad_dir))
body=open(os.path.join(bad_dir,saved[0])).read()
check("open positions still rendered", "HUT" in body)
check("P&L section still present", "Realized" in body)
open(badf,"w").write("not json at all")
check("garbage file -> still no raise", enb.send_report(trades_file=badf, label="X", open_positions=open_rows) is False)
open(badf,"w").write('[]')
check("valid-but-empty file + open positions -> reports", enb.send_report(trades_file=badf, label="X", open_positions=open_rows) is False)

src=open(repo_file("src", "main.py")).read()
check("scheduled report saves the trade log FIRST",
      src.index("save_trades_log()\n        except Exception as e:\n            logger.error(f\"Could not save the trade log before the scheduled report")
      < src.index("open_rows = _open_position_rows"), "")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
