"""Stream failure detection: named errors instead of silent 120s timeouts."""
import sys, logging, time, yaml
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.data.stream as ST
from src.data.stream import PriceStream, _StreamErrorWatcher, ALPACA_WS_LOGGER, FATAL_STREAM_ERRORS
CFG=yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

print("=== A. ERROR WATCHER ===")
w=_StreamErrorWatcher()
lg=logging.getLogger("test.ws"); lg.addHandler(w); lg.setLevel(logging.ERROR)
check("clean start", w.hit is None)
lg.error("error: symbol limit exceeded (405)")
check("catches the EXACT 2026-08-21 error", w.hit is not None, w.hit)
check("names the real cause, not a guess", "stream_max_subscriptions" in w.hit[1], w.hit)
check("keeps alpaca's raw text for the log", "405" in w.hit[0], w.hit)

# connection-limit moved from FATAL to RETRYABLE on 2026-08-21 - a restart can
# briefly overlap the previous process's socket, and giving up on that would
# throw away the session's live data over a few seconds. Covered in test_socket.
w_r=_StreamErrorWatcher(); lg_r=logging.getLogger("t.retry")
lg_r.addHandler(w_r); lg_r.setLevel(logging.ERROR)
lg_r.error("error: connection limit exceeded")
check("connection limit classified RETRYABLE, not fatal",
      w_r.retryable is not None and w_r.hit is None, (w_r.hit, w_r.retryable))

for msg, needle in [("auth failed", "rejected"),
                    ("error: insufficient subscription", "data plan")]:
    w2=_StreamErrorWatcher(); lg2=logging.getLogger(f"t.{needle[:5]}")
    lg2.addHandler(w2); lg2.setLevel(logging.ERROR); lg2.error(msg)
    check(f"catches {msg!r}", w2.hit is not None and needle in w2.hit[1], w2.hit)

w3=_StreamErrorWatcher(); lg3=logging.getLogger("t.noise")
lg3.addHandler(w3); lg3.setLevel(logging.INFO)
lg3.info("connected to wss://stream.data.alpaca.markets/v2/iex")
lg3.error("some unrelated transient blip")
check("ignores INFO chatter", w3.hit is None or "blip" not in str(w3.hit))
check("ignores unrecognised errors (no false give-up)", w3.hit is None, w3.hit)
w3.emit(object())
check("malformed record -> no raise", True)
check("case-insensitive", (lambda: (lambda h,l: (l.addHandler(h), l.setLevel(logging.ERROR), l.error("ERROR: SYMBOL LIMIT EXCEEDED"), h.hit is not None)[-1])(_StreamErrorWatcher(), logging.getLogger("t.case")))())

print("\n=== B. WATCHDOG ACTS FAST ===")
def mk(trades=True, cap=28):
    ps=PriceStream("k","s",feed="iex",subscribe_trades=trades,max_subscriptions=cap)
    ps._run_forever=lambda: None
    return ps
ps=mk(); ps.start([f"S{i}" for i in range(59)], priority=[])
check("watchdog thread is running", ps._watchdog.is_alive())
check("error watcher attached to alpaca's logger",
      ps._error_watcher in logging.getLogger(ALPACA_WS_LOGGER).handlers)
t0=time.monotonic()
logging.getLogger(ALPACA_WS_LOGGER).error("error: symbol limit exceeded (405)")
for _ in range(60):
    if ps._gave_up: break
    time.sleep(0.1)
elapsed=time.monotonic()-t0
check("gives up on the named error", ps._gave_up is True)
check(f"...in seconds, not the 120s timeout (took {elapsed:.1f}s)", elapsed < 10, elapsed)
check("handler detached on stop",
      ps._error_watcher not in logging.getLogger(ALPACA_WS_LOGGER).handlers)
check("is_healthy() reports false after giving up", ps.is_healthy() is False)

print("\n=== C. NO FALSE GIVE-UPS ===")
ps2=mk(); ps2.start(["A","B"])
logging.getLogger(ALPACA_WS_LOGGER).error("transient socket hiccup, retrying")
time.sleep(0.5)
check("unrecognised error does NOT kill the stream", ps2._gave_up is False)
with ps2._lock: ps2._bars_received = 5
logging.getLogger(ALPACA_WS_LOGGER).error("error: symbol limit exceeded")
time.sleep(3)
check("once bars are flowing, watchdog stands down", ps2._gave_up is False)
ps2.stop()

print("\n=== D. THE MISLEADING LOG LINE ===")
src=open(repo_file("src", "data", "stream.py")).read()
check("no longer claims 'connected' before the socket is up",
      'logger.info(f"PriceStream connected, subscribed to' not in src)
check("says what it is actually doing instead", "waiting for the first bar to confirm" in src)
check("logs the real subscription count, not the symbol count", "subs = len(self._symbols)" in src)
check("doubles the count when trades are on", "subs *= 2" in src)

print("\n=== E. COOLDOWN ===")
t=CFG["trading"]
check("cooldown reduced from 20", t["reentry_cooldown_minutes"] < 20, t["reentry_cooldown_minutes"])
check("cooldown not removed entirely", t["reentry_cooldown_minutes"] > 0, t["reentry_cooldown_minutes"])
check("shorter than the entry window (20 min)", t["reentry_cooldown_minutes"] < 20)
check("still gates only losers", t["reentry_cooldown_after_loss_only"] is True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
