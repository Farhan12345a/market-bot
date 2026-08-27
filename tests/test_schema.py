"""
The two pieces of new code from 2026-08-26 that nothing else covers:
csv_schema.repair_header and main._rank_burst.

Both were written in response to real faults, and both are the kind of code
where a plausible-looking implementation is silently wrong - the header repair
can shift every value one column left, and the ranker can reorder the wrong way
round. Neither failure would raise.
"""
import copy, csv, os, shutil, sys, tempfile, yaml
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
from src.analytics.csv_schema import repair_header, read_header, remap_row
from src.analytics.signal_journal import JOURNAL_FIELDS
import src.main as M

CFG = yaml.safe_load(open(CONFIG))
P = F = 0
def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")

TMP = tempfile.mkdtemp()
OLD19 = ("date,signal_time,symbol,entry_method,price,signal_pct,excess_vs_spy_pct,"
         "spy_pct,rvol,spread_pct,burst_width,taken,skip_reason,qty,size_multiplier,"
         "price_15min,pct_15min,price_30min,pct_30min").split(",")

print("=== 1. THE SCHEMA REALLY DID GROW BY INSERTION ===")
check("old header is NOT a prefix of the new one",
      OLD19 != JOURNAL_FIELDS[:len(OLD19)])
check("the divergence is at index 11 (taken vs opening_hit_rate)",
      OLD19[11] == "taken" and JOURNAL_FIELDS[11] == "opening_hit_rate")
check("every old column still exists in the new schema",
      all(c in JOURNAL_FIELDS for c in OLD19))

print("\n=== 2. REPAIR PRESERVES MEANING, NOT POSITION ===")
def build(path, n_old=5, n_new=5):
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(OLD19)
        for i in range(n_old):
            r = dict.fromkeys(OLD19, "")
            r.update(date="2026-08-20", symbol=f"OLD{i}", price="100.0",
                     taken="True", skip_reason="none", pct_15min="1.5")
            w.writerow([r[k] for k in OLD19])
        for i in range(n_new):
            r = dict.fromkeys(JOURNAL_FIELDS, "")
            r.update(date="2026-08-26", symbol=f"NEW{i}", price="200.0",
                     cf_score="61.05", taken="False", skip_reason="burst_throttle",
                     pct_15min="-0.5")
            w.writerow([r[k] for k in JOURNAL_FIELDS])

p = os.path.join(TMP, "j.csv"); build(p)
check("stale header detected", len(read_header(p)) == 19)
check("repair reports a change", repair_header(p, JOURNAL_FIELDS) is True)
check("header is now full width", len(read_header(p)) == len(JOURNAL_FIELDS))

rows = list(csv.DictReader(open(p, newline="")))
check("no rows lost", len(rows) == 10)
old_row = [r for r in rows if r["symbol"] == "OLD0"][0]
new_row = [r for r in rows if r["symbol"] == "NEW0"][0]
check("legacy `taken` did NOT land under opening_hit_rate", old_row["taken"] == "True")
check("legacy skip_reason preserved", old_row["skip_reason"] == "none")
check("legacy pct_15min preserved", old_row["pct_15min"] == "1.5")
check("legacy opening_hit_rate is BLANK, not shifted data",
      old_row["opening_hit_rate"] == "")
check("legacy cf_score is BLANK (never recorded)", old_row["cf_score"] == "")
check("current row's cf_score survives", new_row["cf_score"] == "61.05")
check("current row's skip_reason survives", new_row["skip_reason"] == "burst_throttle")
check("a backup was written", os.path.exists(p + ".bak"))
check("second repair is a no-op", repair_header(p, JOURNAL_FIELDS) is False)

print("\n=== 3. REPAIR REFUSES WHAT IT CANNOT IDENTIFY ===")
p2 = os.path.join(TMP, "weird.csv")
with open(p2, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["date", "symbol", "a_column_that_never_existed"])
    w.writerow(["2026-08-20", "X", "1"])
before = open(p2).read()
check("a header with an unknown column is refused",
      repair_header(p2, JOURNAL_FIELDS) is False)
check("...and the file is left byte-identical", open(p2).read() == before)
check("missing file is a no-op", repair_header(os.path.join(TMP, "nope.csv"), JOURNAL_FIELDS) is False)
p3 = os.path.join(TMP, "empty.csv"); open(p3, "w").close()
check("empty file is a no-op", repair_header(p3, JOURNAL_FIELDS) is False)
p4 = os.path.join(TMP, "ok.csv")
with open(p4, "w", newline="") as f:
    csv.writer(f).writerow(JOURNAL_FIELDS)
check("already-correct header is a no-op", repair_header(p4, JOURNAL_FIELDS) is False)

print("\n=== 3b. THREE GENERATIONS IN ONE FILE ===")
# Adding the sector columns on 2026-08-26 put a 19-column header, 32-column rows
# from earlier the same day, and a 34-column schema in one file. Without a
# declared schema history the middle generation - carrying every continuation
# factor - matched neither end and was copied verbatim, silently reintroducing
# the exact fault this module exists to fix.
from src.analytics.signal_journal import JOURNAL_FIELDS_HISTORY
check("a schema history is declared", len(JOURNAL_FIELDS_HISTORY) >= 2)
check("every historic column still exists today",
      all(c in JOURNAL_FIELDS for h in JOURNAL_FIELDS_HISTORY for c in h))
V19, V32 = JOURNAL_FIELDS_HISTORY[0], JOURNAL_FIELDS_HISTORY[-1]
p3 = os.path.join(TMP, "three.csv")
with open(p3, "w", newline="") as f:
    w = csv.writer(f); w.writerow(V19)
    r = dict.fromkeys(V19, ""); r.update(symbol="V1", taken="True", pct_15min="0.9",
                                         skip_reason="none")
    w.writerow([r[k] for k in V19])
    r = dict.fromkeys(V32, ""); r.update(symbol="V2", cf_score="61.05", taken="False",
                                         skip_reason="burst_throttle", pct_15min="1.2")
    w.writerow([r[k] for k in V32])
    r = dict.fromkeys(JOURNAL_FIELDS, ""); r.update(symbol="V3", cf_score="70.0",
                                                    cf_sector_strength="82.5",
                                                    cf_sector_etf="WGMI")
    w.writerow([r[k] for k in JOURNAL_FIELDS])
widths = sorted({len(r) for r in list(csv.reader(open(p3, newline="")))[1:]})
check("fixture really holds three widths", len(widths) == 3, widths)
check("repair runs", repair_header(p3, JOURNAL_FIELDS,
                                   legacy_schemas=JOURNAL_FIELDS_HISTORY) is True)
g = {r["symbol"]: r for r in csv.DictReader(open(p3, newline=""))}
check("all three rows survive", set(g) == {"V1", "V2", "V3"}, sorted(g))
check("v1 taken preserved", g["V1"]["taken"] == "True")
check("v1 has no cf_score invented", g["V1"]["cf_score"] == "")
check("v2 cf_score preserved (the regression this guards)", g["V2"]["cf_score"] == "61.05",
      g["V2"]["cf_score"])
check("v2 skip_reason preserved", g["V2"]["skip_reason"] == "burst_throttle")
check("v2 has no sector value invented", g["V2"]["cf_sector_strength"] == "")
check("v3 sector columns preserved",
      g["V3"]["cf_sector_strength"] == "82.5" and g["V3"]["cf_sector_etf"] == "WGMI")
# The counterfactual: repair the SAME original file without declaring a
# history, and the middle generation no longer reads correctly. This is what
# makes the history load-bearing rather than decorative.
nohist = os.path.join(TMP, "nohist.csv")
shutil.copy2(p3 + ".bak", nohist)
repair_header(nohist, JOURNAL_FIELDS)          # no legacy_schemas
ng = {r["symbol"]: r for r in csv.DictReader(open(nohist, newline=""))}
check("without the history, v2's cf_score no longer reads correctly",
      ng.get("V2", {}).get("cf_score") != "61.05", ng.get("V2", {}).get("cf_score"))
check("...while v1 and v3 are unaffected either way",
      ng["V1"]["taken"] == "True" and ng["V3"]["cf_sector_etf"] == "WGMI")

print("\n=== 4. remap_row ===")
check("remap places by name",
      remap_row(["a", "b"], ["x", "y"], ["y", "x", "z"]) == ["b", "a", ""])

print("\n=== 5. _rank_burst ===")
def cand(sym, score):
    return {"symbol": sym, "cont": {"cf_score": score}}

ON = copy.deepcopy(CFG); ON["trading"]["use_continuation_score"] = True
OFF = copy.deepcopy(CFG); OFF["trading"]["use_continuation_score"] = False

c = [cand("A", 10.0), cand("B", 90.0), cand("C", 50.0)]
out, note = M._rank_burst(OFF, list(c))
check("off -> order untouched", [x["symbol"] for x in out] == ["A", "B", "C"])
check("off -> no note", note is None)

out, note = M._rank_burst(ON, list(c))
check("on -> best first", [x["symbol"] for x in out] == ["B", "C", "A"])
check("on -> note names the scores", "B=90" in note, note)
check("nothing is dropped", len(out) == 3)

out, _ = M._rank_burst(ON, [cand("A", None), cand("B", 50.0), cand("C", None)])
check("unscorable sort LAST but are kept",
      [x["symbol"] for x in out][0] == "B" and len(out) == 3)

out, note = M._rank_burst(ON, [cand("SOLO", 5.0)])
check("a single candidate is not 'ranked'", note is None and len(out) == 1)
out, note = M._rank_burst(ON, [])
check("empty burst is safe", out == [] and note is None)

miss = [{"symbol": "A"}, cand("B", 50.0)]
out, _ = M._rank_burst(ON, miss)
check("a candidate with no cont dict does not raise",
      [x["symbol"] for x in out] == ["B", "A"])

ties = [cand("A", 50.0), cand("B", 50.0)]
out, _ = M._rank_burst(ON, ties)
check("equal scores keep both", len(out) == 2)

print("\n=== 6. RANKING CHANGES *WHICH*, NEVER *HOW MANY* ===")
c5 = [cand(s, v) for s, v in [("A", 10.0), ("B", 90.0), ("C", 50.0), ("D", 70.0), ("E", 30.0)]]
ranked, _ = M._rank_burst(ON, list(c5))
plain, _ = M._rank_burst(OFF, list(c5))
check("same population either way", sorted(x["symbol"] for x in ranked) ==
      sorted(x["symbol"] for x in plain))
BURST_MAX = 3
check("throttle keeps the best 3 when ranked",
      [x["symbol"] for x in ranked[:BURST_MAX]] == ["B", "D", "C"])
check("throttle kept the first 3 by list order when not",
      [x["symbol"] for x in plain[:BURST_MAX]] == ["A", "B", "C"])

shutil.rmtree(TMP, ignore_errors=True)
print("\n=== 7. THE ANALYSIS SCRIPTS TRACK THE REAL SCHEMA ===")
# ops/session-metrics.py and ops/analyze-journal.py restate the field lists so
# they can run on a VPS checkout older than themselves. That duplication drifts
# silently: on 2026-08-28 adding two columns left session-metrics with the same
# 34 names in a DIFFERENT order, which maps every value after index 23 to the
# wrong column while parsing without error.
import importlib.util
def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, repo_file("ops", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

sm = _load("session-metrics.py", "sm")
aj = _load("analyze-journal.py", "aj")
check("session-metrics journal schema matches src exactly",
      sm.JOURNAL_FIELDS == list(JOURNAL_FIELDS),
      [i for i, (x, y) in enumerate(zip(sm.JOURNAL_FIELDS, JOURNAL_FIELDS)) if x != y][:3])
check("analyze-journal schema matches src exactly",
      aj.JOURNAL_FIELDS == list(JOURNAL_FIELDS),
      [i for i, (x, y) in enumerate(zip(aj.JOURNAL_FIELDS, JOURNAL_FIELDS)) if x != y][:3])
check("session-metrics declares a trade-schema history",
      len(sm.TRADE_FIELDS_HISTORY) >= 1)
check("every historic trade column still exists",
      all(c in sm.TRADE_FIELDS for h in sm.TRADE_FIELDS_HISTORY for c in h))
check("list_source is the LAST trade column (appended, never inserted)",
      sm.TRADE_FIELDS[-1] == "list_source")

# A row one column short must still map, not vanish.
import tempfile as _tf, csv as _csv, os as _os
_d = _tf.mkdtemp()
_p = _os.path.join(_d, "t.csv")
with open(_p, "w", newline="") as f:
    w = _csv.writer(f)
    w.writerow(sm.TRADE_FIELDS[:-1])                      # old header
    w.writerow(["2026-08-26", "AAA"] + [""] * (len(sm.TRADE_FIELDS) - 3) + ["1.0"])
rows, widths = sm.read_rows(_p, sm.TRADE_FIELDS, sm.TRADE_FIELDS_HISTORY)
check("a pre-list_source row is not dropped", len(rows) == 1, widths)
check("...and its symbol still reads", rows and rows[0]["symbol"] == "AAA")
shutil.rmtree(_d, ignore_errors=True)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
