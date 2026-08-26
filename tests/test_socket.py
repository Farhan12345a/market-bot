"""Single-socket guard, connection-limit retry, re-entry column."""
import sys, logging, time, copy, tempfile, yaml
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.data.stream as ST
from src.data.stream import PriceStream, ALPACA_WS_LOGGER
from src.notifications.email_notifier import EmailNotifier
CFG=yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

def mk():
    ps=PriceStream("k","s",feed="iex",subscribe_trades=True,max_subscriptions=28)
    ps._run_forever=lambda: None
    ps._restart_connection=lambda: setattr(ps,"_restarts",getattr(ps,"_restarts",0)+1)
    return ps

print("=== A. ONE SOCKET PER PROCESS ===")
ST._ACTIVE_STREAM=None
a=mk(); a.start(["A","B"])
check("first stream claims the slot", ST._ACTIVE_STREAM is a)
b=mk(); b.start(["C","D"])
check("second start displaces the first", ST._ACTIVE_STREAM is b)
check("the displaced stream was stopped", a._stop_requested.is_set())
check("the new stream is running", not b._stop_requested.is_set())
b.stop()
check("stop releases the slot", ST._ACTIVE_STREAM is None)
c=mk(); c.start(["E"]); c.start(["F"])
check("restarting the SAME stream does not stop itself", not c._stop_requested.is_set())
c.stop()
d_=mk(); d_.start(["X"]); ST._ACTIVE_STREAM=None; d_.stop()
check("stop when not the active stream -> no raise, no clobber", ST._ACTIVE_STREAM is None)

print("=== B. CONNECTION LIMIT IS RETRIED, NOT FATAL ===")
ST._ACTIVE_STREAM=None
ps=mk(); ps.start([f"S{i}" for i in range(20)])
lg=logging.getLogger(ALPACA_WS_LOGGER)
lg.error("error: connection limit exceeded")
for _ in range(60):
    if getattr(ps,"_restarts",0) >= 1: break
    time.sleep(0.1)
check("connection-limit triggers a RETRY, not a give-up", getattr(ps,"_restarts",0)>=1, getattr(ps,"_restarts",0))
check("stream is NOT marked as given up", ps._gave_up is False)
check("grace period reset for the retry", ps._connection_attempts>=1, ps._connection_attempts)
for i in range(ST.CONNECTION_LIMIT_RETRIES + 2):
    lg.error("error: connection limit exceeded"); time.sleep(2.4)
    if ps._gave_up: break
check(f"gives up after {ST.CONNECTION_LIMIT_RETRIES} attempts", ps._gave_up is True, ps._connection_attempts)
ps.stop()

print("=== C. FATAL vs RETRYABLE ===")
ST._ACTIVE_STREAM=None
ps2=mk(); ps2.start(["A"])
lg.error("error: symbol limit exceeded (405)")
for _ in range(60):
    if ps2._gave_up: break
    time.sleep(0.1)
check("symbol limit is still FATAL (retrying would never help)", ps2._gave_up is True)
check("symbol limit does not consume retry attempts", ps2._connection_attempts==0, ps2._connection_attempts)
ps2.stop()
check("retryable list holds only connection-limit", ST.RETRYABLE_STREAM_ERRORS==("connection limit exceeded",))
check("connection-limit removed from the fatal set",
      not any("connection limit" in k for k in ST.FATAL_STREAM_ERRORS))

print("=== D. RE-ENTRY COLUMN ===")
tmp=tempfile.mkdtemp(); cfg=copy.deepcopy(CFG); cfg["notifications"]["report_dir"]=tmp
en=EmailNotifier(cfg)
T=[{"symbol":"MARA","timestamp":"2026-08-24T09:36:00","entry_price":10,"exit_price":9.9,"qty":10,
    "pl":-20.0,"pl_pct":-1.0,"exit_reason":"FINAL_EXIT_-1.0%","entry_method":"X","burst_logic":"",
    "stop_loss_used":True},
   {"symbol":"MARA","timestamp":"2026-08-24T09:42:00","entry_price":10,"exit_price":10.2,"qty":10,
    "pl":40.0,"pl_pct":2.0,"exit_reason":"TAKE_PROFIT","entry_method":"X","burst_logic":"",
    "stop_loss_used":False},
   {"symbol":"HUT","timestamp":"2026-08-24T09:38:00","entry_price":20,"exit_price":20.1,"qty":5,
    "pl":5.0,"pl_pct":0.5,"exit_reason":"TRAILING_STOP","entry_method":"X","burst_logic":"",
    "stop_loss_used":False}]
lab=en._reentry_labels(T)
check("first trade in a symbol is '1st'", lab[id(T[0])]=="1st", lab[id(T[0])])
check("second trade is '2nd' with the gap", lab[id(T[1])]=="2nd, +6m", lab[id(T[1])])
check("a different symbol starts at 1st", lab[id(T[2])]=="1st", lab[id(T[2])])
check("ordinals", (en._ordinal(1),en._ordinal(2),en._ordinal(3),en._ordinal(4),en._ordinal(11))==("1st","2nd","3rd","4th","11th"))
h=en._generate_html_summary(T, label="Closing Report")
check("Re-entry column header present", "<th>Re-entry</th>" in h)
check("re-entry label rendered", "2nd, +6m" in h)
check("cooldown shown in the run-context band is absent without context", "Re-entry cooldown" not in h)
en.run_context={"symbols_watched":59,"symbols_streamed":14,"symbols_rest":45,"trade_ticks":True,
                "price_source":"stream","feed":"iex","symbols_note":"",
                "reentry_cooldown_minutes":5,"reentry_cooldown_after_loss_only":True}
h2=en._generate_html_summary(T, label="Closing Report")
check("cooldown value in the header band", "5 min" in h2)
check("cooldown scope stated", "after losses only" in h2)
check("column count still matches the header row",
      h2.count("<th>")==h2[:h2.index("</thead>")].count("<th>"), "")
bad=[{"symbol":"A","timestamp":"not-a-date","pl":0},{"symbol":"A","timestamp":"also-bad","pl":0}]
lb=en._reentry_labels(bad)
# The rows sort by timestamp STRING, so which of the two lands first is
# whatever sorts first - the point is that both get an ordinal, neither
# gets a fabricated gap, and nothing raises.
check("unparseable timestamps -> ordinals, no fabricated gap, no raise",
      sorted(lb.values())==["1st","2nd"] and not any("+" in v for v in lb.values()), lb)
check("empty trade list -> empty labels, no raise", en._reentry_labels([])=={})

print("=== E. CONFIG ===")
t=CFG["trading"]
check("cooldown stays at 5", t["reentry_cooldown_minutes"]==5, t["reentry_cooldown_minutes"])
check("still losses-only", t["reentry_cooldown_after_loss_only"] is True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
