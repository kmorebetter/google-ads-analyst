#!/usr/bin/env python3
"""Run the graded eval suite for the three bot templates and score them.

Run:  python3 evals/run_evals.py [--label after] [--write]

Scores:
  Negative Keyword Miner  precision and recall of negatives.csv against the
                          expert answer key (EITHER rows excluded)
  Auditor / Weekly        precision and recall of actionable findings
                          (severity high/medium) against the expert key;
                          usefulness = share of findings that name a place,
                          give evidence and give a concrete action
  All                     clarity: Flesch-Kincaid grade of the report prose
                          (tables and names stripped), and zero em dashes
  Bad inputs              every malformed input refused with a plain message,
                          a non-zero exit and no Python traceback

Exit status is non-zero if any threshold is missed.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(HERE))

import scenarios as S  # noqa: E402

THRESHOLDS = {
    "negatives_precision": 0.95,
    "negatives_recall": 0.90,
    "audit_precision": 0.90,
    "audit_recall": 0.90,
    "audit_usefulness": 0.95,
    "weekly_precision": 0.90,
    "weekly_recall": 0.90,
    "weekly_usefulness": 0.95,
    "clarity_max_grade": 9.0,
    "em_dashes": 0,
    "bad_inputs_handled": 1.0,
    "max_seconds_large": 10.0,
}

VERBS = {"check", "add", "remove", "cut", "review", "tighten", "confirm", "look", "fix", "open",
         "keep", "wait", "watch", "note", "judge", "pause", "upload", "spend", "fix", "do"}


def tool(name):
    import importlib
    return importlib.import_module({"negatives": "csv_negatives", "audit": "csv_audit",
                                    "weekly": "csv_weekly"}[name])


def call(name, files, args):
    """Run a tool's main() in a temp dir. Returns (exit, stdout, stderr, outdir, seconds)."""
    d = Path(tempfile.mkdtemp(prefix="gads-eval-"))
    for fn, data in files.items():
        (d / fn).write_bytes(data)
    out = d / "out"
    argv = [str(d / a) if (d / a).exists() or a.endswith(".csv") else a for a in args] + ["--out", str(out)]
    so, se = io.StringIO(), io.StringIO()
    t0 = time.time()
    code = 0
    with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
        try:
            code = tool(name).main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 2
            print(f"[SystemExit {e.code}]", file=sys.stderr)
        except Exception as e:  # a crash is a failure, recorded as a traceback
            import traceback
            traceback.print_exc()
            code = 99
    return code, so.getvalue(), se.getvalue(), out, time.time() - t0


def syllables(word):
    w = word.lower()
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups)
    if w.endswith("e") and n > 1 and not w.endswith("le"):
        n -= 1
    return max(1, n)


def clarity(md: str):
    prose = []
    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith(("|", "#", "```")):
            continue
        s = re.sub(r"\*\*[^*]+\*\*", "It", s)
        s = re.sub(r"`[^`]*`", "it", s)
        s = re.sub(r"\[[^\]]*\]", "it", s)
        s = re.sub(r"[-+]?\d[\d,]*(\.\d+)?%?", "ten", s)
        s = s.lstrip("-* ")
        if not s.endswith((".", "!", "?", ":")):
            s += "."
        prose.append(s)
    text = " ".join(prose)
    words = re.findall(r"[A-Za-z']+", text)
    sents = max(1, len(re.findall(r"[.!?:](\s|$)", text)))
    if not words:
        return 0.0
    syl = sum(syllables(w) for w in words)
    return round(0.39 * len(words) / sents + 11.8 * syl / len(words) - 15.59, 1)


incomplete: list = []


def norm(t):
    return " ".join(t.lower().split())


def score_negatives(scenarios=None):
    tp = fp = fn = 0
    rows, grades, dashes, slow = [], [], 0, 0.0
    for sc in (scenarios if scenarios is not None else S.neg_scenarios()):
        code, so, se, out, secs = call("negatives", sc.files, sc.args)
        pred = set()
        if code == 0 and (out / "negatives.csv").exists():
            with open(out / "negatives.csv", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    pred.add((r["Campaign"], r["Ad group"], norm(r["Keyword"].strip("[]\"+"))))
            md = (out / "summary.md").read_text(encoding="utf-8")
            grades.append(clarity(md)); dashes += md.count("\u2014")
        if code != 0:
            incomplete.append(sc.name)
        neg = {k for k, v in sc.labels.items() if v == "NEG"}
        either = {k for k, v in sc.labels.items() if v == "EITHER"}
        s_tp = len(pred & neg); s_fp = len(pred - neg - either); s_fn = len(neg - pred)
        tp += s_tp; fp += s_fp; fn += s_fn
        if "large" in sc.name:
            slow = max(slow, secs)
        rows.append({"scenario": sc.name, "exit": code, "tp": s_tp, "fp": s_fp, "fn": s_fn,
                     "false_positives": sorted(" | ".join(k) for k in pred - neg - either)[:8],
                     "misses": sorted(" | ".join(k) for k in neg - pred)[:8],
                     "error": (se.strip().splitlines() or [""])[-1][:200] if code else "",
                     "seconds": round(secs, 2)})
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "clarity_grade": max(grades) if grades else None,
            "em_dashes": dashes, "seconds_large": round(slow, 2), "scenarios": rows}


def useful(f):
    words = f["What to do"].split()
    return bool(f["Where"].strip()) and len(f["Evidence"].split()) >= 2 and len(words) >= 6 and \
        any(w.strip(".,:;").lower() in VERBS for w in words[:3])


def score_findings(name, scenarios, report):
    tp = fp = fn = 0
    good = total = 0
    rows, grades, dashes, slow = [], [], 0, 0.0
    for sc in scenarios:
        code, so, se, out, secs = call(name, sc.files, sc.args)
        pred = set()
        if code == 0 and (out / "findings.csv").exists():
            with open(out / "findings.csv", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r["Severity"] in ("high", "medium"):
                        pred.add((r["Code"], r["Where"]))
                        total += 1; good += useful(r)
            md = (out / report).read_text(encoding="utf-8")
            grades.append(clarity(md)); dashes += md.count("\u2014")
        if code != 0:
            incomplete.append(sc.name)
        s_tp = len(pred & sc.expect); s_fp = len(pred - sc.expect - sc.either); s_fn = len(sc.expect - pred)
        tp += s_tp; fp += s_fp; fn += s_fn
        if "large" in sc.name:
            slow = max(slow, secs)
        rows.append({"scenario": sc.name, "exit": code, "tp": s_tp, "fp": s_fp, "fn": s_fn,
                     "false_positives": sorted(" | ".join(k) for k in pred - sc.expect - sc.either)[:8],
                     "misses": sorted(" | ".join(k) for k in sc.expect - pred)[:8],
                     "error": (se.strip().splitlines() or [""])[-1][:200] if code else "",
                     "seconds": round(secs, 2)})
    p = tp / (tp + fp) if tp + fp else (1.0 if not fn else 0.0)
    r = tp / (tp + fn) if tp + fn else 1.0
    return {"precision": round(p, 3), "recall": round(r, 3),
            "usefulness": round(good / total, 3) if total else 0.0,
            "clarity_grade": max(grades) if grades else None, "em_dashes": dashes,
            "seconds_large": round(slow, 2), "scenarios": rows}


def score_bad():
    rows, ok = [], 0
    for sc in S.bad_scenarios():
        code, so, se, out, _ = call(sc.tool, sc.files, sc.args)
        handled = code not in (0, 99) and "Traceback" not in se and \
            bool(re.search(r"REFUS", se)) and "SystemExit" not in se
        ok += handled
        rows.append({"scenario": sc.name, "exit": code, "handled": handled,
                     "message": (se.strip().splitlines() or [""])[-1][:160]})
    return {"handled": round(ok / len(rows), 3), "scenarios": rows}


def verdicts(res):
    T = THRESHOLDS
    n, a, w, b = res["negatives"], res["audit"], res["weekly"], res["bad_inputs"]
    checks = {
        "negatives precision": (n["precision"], ">=", T["negatives_precision"]),
        "negatives recall": (n["recall"], ">=", T["negatives_recall"]),
        "audit precision": (a["precision"], ">=", T["audit_precision"]),
        "audit recall": (a["recall"], ">=", T["audit_recall"]),
        "audit usefulness": (a["usefulness"], ">=", T["audit_usefulness"]),
        "weekly precision": (w["precision"], ">=", T["weekly_precision"]),
        "weekly recall": (w["recall"], ">=", T["weekly_recall"]),
        "weekly usefulness": (w["usefulness"], ">=", T["weekly_usefulness"]),
        "negatives clarity grade": (n["clarity_grade"], "<=", T["clarity_max_grade"]),
        "audit clarity grade": (a["clarity_grade"], "<=", T["clarity_max_grade"]),
        "weekly clarity grade": (w["clarity_grade"], "<=", T["clarity_max_grade"]),
        "em dashes in outputs": (n["em_dashes"] + a["em_dashes"] + w["em_dashes"], "<=", T["em_dashes"]),
        "bad inputs handled": (b["handled"], ">=", T["bad_inputs_handled"]),
        "valid scenarios completed": (res["completed"], ">=", 1.0),
        "large-account seconds": (max(n["seconds_large"], a["seconds_large"], w["seconds_large"]), "<=", T["max_seconds_large"]),
    }
    out = {}
    for k, (v, op, t) in checks.items():
        passed = v is not None and (v >= t if op == ">=" else v <= t)
        out[k] = {"value": v, "threshold": f"{op} {t}", "pass": passed}
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="current")
    p.add_argument("--write", action="store_true", help="save evals/results/<label>.json")
    a = p.parse_args(argv)
    res = {"negatives": score_negatives(),
           "audit": score_findings("audit", S.audit_scenarios(), "audit.md"),
           "weekly": score_findings("weekly", S.weekly_scenarios(), "weekly.md"),
           "bad_inputs": score_bad()}
    res["holdout"] = score_negatives(S.holdout_scenarios())
    total = len(S.neg_scenarios()) + len(S.audit_scenarios()) + len(S.weekly_scenarios())
    res["completed"] = round((total - len(incomplete)) / total, 3)
    res["incomplete"] = incomplete
    res["verdicts"] = verdicts(res)
    if a.write:
        (HERE / "results").mkdir(exist_ok=True)
        (HERE / "results" / f"{a.label}.json").write_text(json.dumps(res, indent=2) + "\n")
    for k, v in res["verdicts"].items():
        print(f"{'PASS' if v['pass'] else 'FAIL'}  {k}: {v['value']} (needs {v['threshold']})")
    h = res["holdout"]
    print(f"INFO  hold-out negatives (not gating): precision {h['precision']}, recall {h['recall']}")
    for part in ("negatives", "audit", "weekly", "holdout"):
        for s in res[part]["scenarios"]:
            if s["fp"] or s["fn"] or s["exit"]:
                print(f"  {part} / {s['scenario']}: exit={s['exit']} tp={s['tp']} fp={s['fp']} fn={s['fn']} "
                      f"FP={s['false_positives']} MISS={s['misses']} {s['error']}")
    for s in res["bad_inputs"]["scenarios"]:
        if not s["handled"]:
            print(f"  bad input NOT handled: {s['scenario']}: exit={s['exit']} {s['message']}")
    return 0 if all(v["pass"] for v in res["verdicts"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
