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
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
