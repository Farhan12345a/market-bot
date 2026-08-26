"""HTTPS notification delivery: Resend + Pushover."""
import sys, os, copy, json, types, yaml
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.notifications.senders as SN
from src.notifications.senders import ResendSender, PushoverSender, build_senders, notify
from src.notifications.email_notifier import EmailNotifier

CFG = yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

class Resp:
    def __init__(s,code,text="ok"): s.status_code=code; s.text=text
CALLS=[]
class FakeRequests:
    def __init__(s,code=200,boom=False): s.code=code; s.boom=boom
    def post(s,url,**kw):
        CALLS.append((url,kw))
        if s.boom: raise ConnectionError("network down")
        return Resp(s.code, "detail")
def patch(mod, code=200, boom=False):
    CALLS.clear()
    sys.modules["requests"]=FakeRequests(code,boom)
def env(**kw):
    for k,v in kw.items():
        if v is None: os.environ.pop(k,None)
        else: os.environ[k]=v

def cfg_with(**kw):
    c=copy.deepcopy(CFG); c["notifications"].update(kw); return c

print("=== A. SHIPPED CONFIG IS SAFE BY DEFAULT ===")
raw=open(CONFIG).read()
check("no app password left in config.yaml", "qgej" not in raw and "sender_password:" not in raw)
check("SMTP disabled (it can never work on this host)", CFG["notifications"]["email"]["enabled"] is False)
# resend is enabled in the repo as of 2026-08-21: it is verified working on the
# live host, and keeping the repo at false meant every pull collided with the
# Droplet's hand-edit. The flag is not a secret; the API key still lives only in
# the environment, which is what the next assertion actually guards.
check("resend enabled in the repo (verified working live)", CFG["notifications"]["resend"]["enabled"] is True)
check("pushover still ships disabled (no keys yet)", CFG["notifications"]["pushover"]["enabled"] is False)
check("pushover ships disabled", CFG["notifications"]["pushover"]["enabled"] is False)
check("no secret anywhere in notifications config",
      not any("key" in str(k).lower() or "token" in str(k).lower() or "password" in str(k).lower()
              for sec in CFG["notifications"].values() if isinstance(sec,dict) for k in sec))

print("\n=== B. AVAILABILITY GATING ===")
env(RESEND_API_KEY=None, PUSHOVER_TOKEN=None, PUSHOVER_USER=None)
c=cfg_with(resend={"enabled":True,"from":"a@b.c","to":"d@e.f"})
check("resend enabled but no key -> unavailable", ResendSender(c).available() is False)
env(RESEND_API_KEY="re_test")
check("resend enabled + key -> available", ResendSender(c).available() is True)
check("resend disabled + key -> still unavailable",
      ResendSender(cfg_with(resend={"enabled":False})).available() is False)
c2=cfg_with(resend={"enabled":True,"from":"a@b.c","to":""})
check("resend with no recipient -> unavailable", ResendSender(c2).available() is False)

cp=cfg_with(pushover={"enabled":True})
check("pushover no creds -> unavailable", PushoverSender(cp).available() is False)
env(PUSHOVER_TOKEN="t")
check("pushover half-configured -> unavailable", PushoverSender(cp).available() is False)
env(PUSHOVER_USER="u")
check("pushover both creds -> available", PushoverSender(cp).available() is True)

print("\n=== C. RESEND REQUEST SHAPE ===")
patch(SN)
r=ResendSender(c); ok=r.send("Subj","text body","<h1>html</h1>")
url,kw = CALLS[0]
check("posts to the resend endpoint", url=="https://api.resend.com/emails", url)
check("bearer auth header", kw["headers"]["Authorization"]=="Bearer re_test")
check("sends the HTML report as the body", kw["json"]["html"]=="<h1>html</h1>")
check("recipient is a list", kw["json"]["to"]==["d@e.f"], kw["json"]["to"])
check("subject passed through", kw["json"]["subject"]=="Subj")
check("has a timeout (won't hang the bot)", kw["timeout"]==SN.HTTP_TIMEOUT_SECONDS)
check("200 -> True", ok is True)
patch(SN); r.send("S","text-only", None)
check("no html -> falls back to the text body", "<pre>text-only</pre>"==CALLS[0][1]["json"]["html"])
patch(SN,403); check("403 -> False, no raise", ResendSender(c).send("S","t") is False)
patch(SN,500); check("500 -> False, no raise", ResendSender(c).send("S","t") is False)
patch(SN,boom=True); check("network error -> False, no raise", ResendSender(c).send("S","t") is False)

print("\n=== D. PUSHOVER REQUEST SHAPE ===")
patch(SN)
p_=PushoverSender(cp); ok=p_.send("Title","message body","<h1>ignored</h1>")
url,kw = CALLS[0]
check("posts to the pushover endpoint", url==SN.PushoverSender.ENDPOINT, url)
check("token + user sent", kw["data"]["token"]=="t" and kw["data"]["user"]=="u")
check("title and message set", kw["data"]["title"]=="Title" and kw["data"]["message"]=="message body")
check("html is NOT pushed to a phone", "html" not in kw["data"] and "<h1>" not in kw["data"]["message"])
check("priority passed", kw["data"]["priority"]==0)
check("200 -> True", ok is True)
patch(SN)
PushoverSender(cp).send("T","x"*3000)
msg=CALLS[0][1]["data"]["message"]
check(f"over-long message trimmed to {SN.PUSHOVER_MESSAGE_LIMIT}", len(msg)==SN.PUSHOVER_MESSAGE_LIMIT, len(msg))
check("trim is marked with an ellipsis", msg.endswith("..."))
patch(SN,500); check("500 -> False, no raise", PushoverSender(cp).send("T","m") is False)
patch(SN,boom=True); check("network error -> False, no raise", PushoverSender(cp).send("T","m") is False)

print("\n=== E. BUILD + FAN-OUT ===")
env(RESEND_API_KEY="re_test", PUSHOVER_TOKEN="t", PUSHOVER_USER="u")
both=cfg_with(resend={"enabled":True,"from":"a@b.c","to":"d@e.f"}, pushover={"enabled":True})
s=build_senders(both)
check("both channels built", {x.name for x in s}=={"resend","pushover"}, [x.name for x in s])
check("order is email then push", [x.name for x in s]==["resend","pushover"])
env(RESEND_API_KEY=None)
check("missing key drops only that channel", [x.name for x in build_senders(both)]==["pushover"])
env(RESEND_API_KEY="re_test")
env(RESEND_API_KEY=None, PUSHOVER_TOKEN=None, PUSHOVER_USER=None)
check("enabled but no keys in the env -> empty, no raise", build_senders(CFG)==[])
env(RESEND_API_KEY="re_test", PUSHOVER_TOKEN="t", PUSHOVER_USER="u")

patch(SN); check("notify: all deliver -> True", notify(s,"S","t","<b>h</b>") is True)
check("notify: fans out to EVERY channel, not first-wins", len(CALLS)==2, len(CALLS))
patch(SN,500); check("notify: all fail -> False", notify(s,"S","t") is False)
class HalfBroken:
    name="broken"
    def send(s,*a,**k): raise RuntimeError("boom")
patch(SN)
check("notify: one raising sender doesn't stop the others",
      notify([HalfBroken()]+s,"S","t") is True)
check("notify: empty sender list -> False, no raise", notify([],"S","t") is False)

print("\n=== F. EMAILNOTIFIER INTEGRATION ===")
env(RESEND_API_KEY="re_test", PUSHOVER_TOKEN="t", PUSHOVER_USER="u")
import tempfile
tmp=tempfile.mkdtemp()
nc=copy.deepcopy(both); nc["notifications"]["report_dir"]=tmp
en=EmailNotifier(nc)
check("notifier builds senders regardless of email.enabled", len(en.senders)==2, len(en.senders))
check("smtp stays off", en.enabled is False)

trades=[{"symbol":"MARA","entry_price":10,"exit_price":10.1,"qty":10,"pl":121.0,"pl_pct":1.0,
         "exit_reason":"TAKE_PROFIT","entry_method":"X","burst_logic":"","stop_loss_used":False},
        {"symbol":"HUT","entry_price":10,"exit_price":9.9,"qty":10,"pl":-55.0,"pl_pct":-1.0,
         "exit_reason":"FINAL_EXIT_-1.0%","entry_method":"X","burst_logic":"","stop_loss_used":True}]
tf=os.path.join(tmp,"trades.json"); json.dump(trades,open(tf,"w"))
patch(SN)
ok=en.send_daily_summary(trades_file=tf)
check("daily summary delivered", ok is True)
check("went to both channels", len(CALLS)==2, len(CALLS))
report=[f for f in os.listdir(tmp) if f.startswith("trading-report")]
check("report still saved to disk", len(report)==1, os.listdir(tmp))
push_msg=[kw["data"]["message"] for u,kw in CALLS if "pushover" in u][0]
check("push carries P&L", "$+66.00" in push_msg or "+66" in push_msg, push_msg)
check("push carries win rate", "50%" in push_msg, push_msg)
check("push names best and worst", "MARA" in push_msg and "HUT" in push_msg, push_msg)
check("push flags take-profit fires (Monday's decision)", "take-profit" in push_msg, push_msg)
check("push is short enough for a lock screen", len(push_msg) < 300, len(push_msg))
email_html=[kw["json"]["html"] for u,kw in CALLS if "resend" in u][0]
check("email carries the FULL html report, not the summary", "<table" in email_html.lower(), email_html[:80])

patch(SN,500)
ok=en.send_daily_summary(trades_file=tf)
check("all channels failing -> False", ok is False)
check("report saved even when delivery fails",
      len([f for f in os.listdir(tmp) if f.startswith("trading-report")])==1)

print("\n=== G. ALERTS ===")
patch(SN)
check("send_alert delivers", en.send_alert("Bot not running","No process at 09:25 ET") is True)
check("alert hits both channels", len(CALLS)==2, len(CALLS))
en2=EmailNotifier({"notifications":{"report_dir":tmp}})
check("no channels -> alert returns False, logs, no raise",
      en2.send_alert("x","y") is False)

print("\n=== H. NOTHING ELSE REGRESSED ===")
env(RESEND_API_KEY=None, PUSHOVER_TOKEN=None, PUSHOVER_USER=None)
c3=copy.deepcopy(CFG); c3["notifications"]["report_dir"]=tmp
en3=EmailNotifier(c3)
check("shipped config minus keys -> zero channels, no crash", en3.senders==[])
patch(SN)
check("no keys -> summary returns False (saved only)",
      en3.send_daily_summary(trades_file=tf) is False)
check("no keys -> no HTTP calls made at all", len(CALLS)==0, len(CALLS))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
