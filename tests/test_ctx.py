"""Run-context band: ticks + symbol counts at the top of every report."""
import sys, copy, types, tempfile, yaml
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.main as M
from src.notifications.email_notifier import EmailNotifier
CFG=yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

class Stream:
    def __init__(s,syms,gave_up=False): s._symbols=list(syms); s._gave_up=gave_up
class N:
    def __init__(s): s.run_context={}

print("=== A. CONTEXT CAPTURE ===")
syms=[f"S{i}" for i in range(59)]
n=N(); M._set_run_context(CFG, n, syms, Stream(syms[:14]))
c=n.run_context
check("total symbol count recorded", c["symbols_watched"]==59, c)
check("streamed count recorded", c["symbols_streamed"]==14, c)
check("REST count is the remainder", c["symbols_rest"]==45, c)
check("ticks flag recorded", c["trade_ticks"] is True, c)
check("price source is stream", c["price_source"]=="stream")
check("feed named", c["feed"]=="iex")

n2=N(); M._set_run_context(CFG, n2, syms, None)
check("no stream -> 0 streamed", n2.run_context["symbols_streamed"]==0)
check("no stream -> source REST", n2.run_context["price_source"]=="REST")
check("no stream -> ticks reported OFF (they cannot apply)", n2.run_context["trade_ticks"] is False)

noticks=copy.deepcopy(CFG); noticks["trading"]["use_trade_ticks_for_entry"]=False
n3=N(); M._set_run_context(noticks, n3, syms, Stream(syms[:28]))
check("ticks off -> flag False", n3.run_context["trade_ticks"] is False)
check("ticks off -> 28 streamed", n3.run_context["symbols_streamed"]==28)

print("\n=== B. DOWNGRADE ON FALLBACK (the 2026-08-21 case) ===")
n4=N(); st=Stream(syms[:14]); M._set_run_context(CFG,n4,syms,st)
check("starts as a streamed session", n4.run_context["price_source"]=="stream")
M._refresh_run_context(n4, st)
check("healthy stream -> unchanged", n4.run_context["price_source"]=="stream")
st._gave_up=True; M._refresh_run_context(n4, st)
check("gave up -> source says stream FAILED", n4.run_context["price_source"]=="REST (stream failed)", n4.run_context)
check("gave up -> streamed count zeroed", n4.run_context["symbols_streamed"]==0)
check("gave up -> everything on REST", n4.run_context["symbols_rest"]==59)
check("gave up -> ticks reported OFF", n4.run_context["trade_ticks"] is False)
check("total symbol count survives the downgrade", n4.run_context["symbols_watched"]==59)
M._refresh_run_context(n4, st)
check("idempotent across repeated polls", n4.run_context["price_source"]=="REST (stream failed)")
M._refresh_run_context(N(), st); check("empty context -> no raise", True)
M._refresh_run_context(n4, None); check("no stream -> no raise", True)

print("\n=== C. RENDERED AT THE TOP OF THE REPORT ===")
tmp=tempfile.mkdtemp(); cfg=copy.deepcopy(CFG); cfg["notifications"]["report_dir"]=tmp
en=EmailNotifier(cfg)
en.run_context={"symbols_watched":59,"symbols_streamed":14,"symbols_rest":45,
                "trade_ticks":True,"price_source":"stream","feed":"iex",
                "symbols_note":"cap 28 subscriptions"}
tr=[{"symbol":"A","entry_price":10,"exit_price":10.1,"qty":10,"pl":50.0,"pl_pct":1.0,
     "exit_reason":"TAKE_PROFIT","entry_method":"X","burst_logic":"","stop_loss_used":False}]
h=en._generate_html_summary(tr, label="Closing Report")
check("total symbol count shown", "59" in h)
check("streamed count shown", "14 of 59" in h, "")
check("REST count shown", "45 on REST" in h, "")
check("ticks state shown", "Trade ticks" in h and ">ON<" in h)
check("price source shown", "stream" in h)
check("subscription cap noted", "cap 28 subscriptions" in h)
check("band appears BEFORE the P&L section", h.index("Total symbols") < h.index("Realized P&amp;L"))
check("band appears before the trade tables", h.index("Total symbols") < h.index("Closed Trades"))

en.run_context={"symbols_watched":59,"symbols_streamed":0,"symbols_rest":59,
                "trade_ticks":False,"price_source":"REST (stream failed)","feed":"",
                "symbols_note":""}
h2=en._generate_html_summary(tr, label="Closing Report")
check("failed-stream session says so", "REST (stream failed)" in h2)
check("ticks shown OFF", ">OFF<" in h2)
check("says bar closes only", "bar closes only" in h2)

en.run_context={}
h3=en._generate_html_summary(tr, label="Closing Report")
check("no context -> band omitted entirely, no crash", "Total symbols" not in h3)
check("no context -> report otherwise intact", "Closed Trades" in h3 and "Realized P&amp;L" in h3)

print("\n=== D. PUSH SUMMARY ===")
en.run_context={"symbols_watched":59,"symbols_streamed":14,"trade_ticks":True,"price_source":"stream"}
t=en._plain_text_summary(tr, [])
check("push leads with the run config", t.startswith("[14/59 streamed, ticks ON, stream]"), t.split("\n")[0])
check("push still carries P&L", "P&L" in t)
en.run_context={}
t2=en._plain_text_summary(tr, [])
check("no context -> push unchanged from before", t2.startswith("P&L"), t2.split("\n")[0])

print("\n=== E. CONFIG FOR MONDAY ===")
t_=CFG["trading"]
check("cap set to 28 (one under the limit)", t_["stream_max_subscriptions"]==28, t_["stream_max_subscriptions"])
check("ticks stay ON for Monday", t_["use_trade_ticks_for_entry"] is True)
budget = t_["stream_max_subscriptions"] // 2
check("yields 14 streamed symbols", budget==14, budget)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
