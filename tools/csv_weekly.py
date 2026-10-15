#!/usr/bin/env python3
"""Weekly Google Ads review from two Campaigns exports (this week, last week)
plus an optional month-to-date export for pacing. No API. Read-only.

Run:
  python3 tools/csv_weekly.py --this-week TW.csv --last-week LW.csv \
      [--mtd MTD.csv --month-budget 54400 --as-of 2026-10-12] \
      [--planned-change-pct -12.5] [--brand-token acme ...] [--out DIR]

The engine behind the Weekly Google Ads Reviewer template. Findings:

  PACING_OVER / PACING_UNDER / PACING_OK
                 month-to-date spend vs budget pro-rated by day, +/- 10% band
                 (campaign-guardrails rule 8: the budget is the owner's stated
                 figure, never derived; a planned step down is not a finding)
  SPEND_UP / SPEND_DOWN   campaign spend moved >= 15% week over week
                          (and by at least the min-move amount)
  CONV_UP / CONV_DOWN     conversions moved >= 15% with >= 5 conversions in
                          both weeks (low-volume floor)
  LOW_DATA                a big conversion move on fewer than 5 conversions:
                          reported as noise, never as a trend
  NEW_CAMPAIGN / GONE_CAMPAIGN  in one week's export but not the other
  TRACKING_CHECK          account conversions fell >= 50% while clicks held
                          within 15%: check tracking before anything else
  TOF campaigns (Video, Demand Gen, Display) are never judged on conversions.

Writes weekly.md and findings.csv.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import gads_csv as G
from gads_csv import CsvInputError
from csv_audit import CAMP_REQ, CAMP_OPT, CAMP_FORBID, TOF_TYPES, Finding

WOW = Decimal("0.15")
FLOOR = 5
BAND = Decimal("0.10")


@dataclass
class Camp:
    name: str
    ctype: str
    cost: Decimal
    conv: Decimal
    clicks: Decimal


def load(path) -> "tuple[dict, G.Export]":
    exp = G.read_file(path, CAMP_REQ, CAMP_OPT, "Campaigns", CAMP_FORBID)
    out = {}
    for r in exp.rows:
        key = r.get("campaign_id") or r["campaign"]
        c = out.get(key)
        cost, conv = G.number(r["cost"]), G.number(r["conversions"])
        clicks = G.number(r.get("clicks", "0"))
        if c:  # duplicate rows (segmented export): sum them
            c.cost += cost; c.conv += conv; c.clicks += clicks
        else:
            out[key] = Camp(r["campaign"], (r.get("type") or "").lower(), cost, conv, clicks)
    return out, exp


def pct(a: Decimal, b: Decimal) -> "Decimal | None":
    if b == 0:
        return None
    return (a - b) / b


def review(tw: dict, lw: dict, min_move: Decimal, planned: "Decimal | None" = None):
    out = []
    for key in sorted(set(tw) | set(lw), key=lambda k: (tw.get(k) or lw.get(k)).name):
        a, b = tw.get(key), lw.get(key)
        if a and not b:
            if a.cost > 0:
                out.append(Finding("NEW_CAMPAIGN", a.name, "medium", f"new this week, spent {G.money(a.cost)}",
                                   "Confirm this campaign was meant to launch and is in the right budget plan."))
            continue
        if b and not a:
            if b.cost > 0:
                out.append(Finding("GONE_CAMPAIGN", b.name, "medium", f"spent {G.money(b.cost)} last week, missing this week",
                                   "Confirm it was paused on purpose. If not, check its status and budget."))
            continue
        tof = any(t in a.ctype for t in TOF_TYPES)
        d = pct(a.cost, b.cost)
        as_planned = planned is not None and d is not None and abs(d - planned) < Decimal("0.05")
        if d is not None and abs(d) >= WOW and abs(a.cost - b.cost) >= min_move and not as_planned:
            up = d > 0
            out.append(Finding("SPEND_UP" if up else "SPEND_DOWN", a.name, "medium",
                               f"spend {G.money(b.cost)} to {G.money(a.cost)} ({int(d * 100):+d}%)",
                               "Check the change history for a budget or bid edit. If none, look at search volume and impression share."))
        if tof:
            continue
        dc = pct(a.conv, b.conv)
        if dc is not None and abs(dc) >= WOW:
            if a.conv >= FLOOR and b.conv >= FLOOR:
                up = dc > 0
                out.append(Finding("CONV_UP" if up else "CONV_DOWN", a.name, "high" if not up else "medium",
                                   f"conversions {b.conv} to {a.conv} ({int(dc * 100):+d}%)",
                                   "Look at its search terms and ads for what changed. Do not change bids until the trend holds a second week."
                                   if not up else "Note what changed so it can be repeated. No action needed yet."))
            else:
                out.append(Finding("LOW_DATA", a.name, "info", f"conversions {b.conv} to {a.conv}",
                                   "Too few conversions to call a trend. Watch it next week."))
    tw_conv = sum((c.conv for c in tw.values() if not any(t in c.ctype for t in TOF_TYPES)), Decimal(0))
    lw_conv = sum((c.conv for c in lw.values() if not any(t in c.ctype for t in TOF_TYPES)), Decimal(0))
    tw_clk = sum((c.clicks for c in tw.values()), Decimal(0))
    lw_clk = sum((c.clicks for c in lw.values()), Decimal(0))
    dconv, dclk = pct(tw_conv, lw_conv), pct(tw_clk, lw_clk)
    if dconv is not None and dconv <= Decimal("-0.5") and lw_conv >= FLOOR and dclk is not None and abs(dclk) < WOW:
        out.insert(0, Finding("TRACKING_CHECK", "Whole account", "high",
                              f"conversions {lw_conv} to {tw_conv} while clicks held ({int(dclk * 100):+d}%)",
                              "Check conversion tracking before reading anything else in this report. A broken tag looks like a crash in results."))
    return out


def pacing(mtd: dict, budget: Decimal, as_of: date):
    days = calendar.monthrange(as_of.year, as_of.month)[1]
    spent = sum((c.cost for c in mtd.values()), Decimal(0))
    expected = budget * as_of.day / days
    ratio = spent / expected if expected else Decimal(0)
    projected = spent / as_of.day * days
    ev = (f"spent {G.money(spent)} of {G.money(budget)} by day {as_of.day} of {days}; "
          f"on-pace amount {G.money(expected)}; month-end projection {G.money(projected)}")
    if ratio > 1 + BAND:
        need = (budget - spent) / (days - as_of.day) if days > as_of.day else Decimal(0)
        return Finding("PACING_OVER", "Monthly budget", "high", ev,
                       f"Cut daily spend to about {G.money(max(need, Decimal(0)))} for the rest of the month to land on budget.")
    if ratio < 1 - BAND:
        return Finding("PACING_UNDER", "Monthly budget", "medium", ev,
                       "Spend is behind plan. Check for limited campaigns or paused ads before raising budgets.")
    return Finding("PACING_OK", "Monthly budget", "info", ev, "On pace. No change needed.")


def render(findings, out: Path, tw, lw, notes):
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "findings.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Code", "Where", "Severity", "Evidence", "What to do"])
        for x in findings:
            w.writerow(x.as_row())
    ts = sum((c.cost for c in tw.values()), Decimal(0)); ls = sum((c.cost for c in lw.values()), Decimal(0))
    tc = sum((c.conv for c in tw.values()), Decimal(0)); lc = sum((c.conv for c in lw.values()), Decimal(0))
    lines = ["# Weekly Google Ads review", "",
             "This review reads the files you uploaded. It did not change anything in your account.",
             "The last few days are not final yet. Late conversions still come in, so treat them as early numbers.", "",
             "## The week in numbers", "",
             G.md_table(["", "Last week", "This week"],
                        [["Spend", G.money(ls), G.money(ts)], ["Conversions", lc, tc]]), ""]
    for title, level in (("Look at first", "high"), ("Worth a look", "medium"), ("Notes", "info")):
        group = [x for x in findings if x.severity == level]
        if not group:
            continue
        lines += [f"## {title}", ""]
        lines += [f"- **{x.entity}**: {x.evidence}. What to do: {x.action}" for x in group] + [""]
    if not [x for x in findings if x.severity in ("high", "medium")]:
        lines += ["Nothing needs action this week.", ""]
    if notes:
        lines += ["## What this review could not check", ""] + [f"- {n}" for n in notes] + [""]
    (out / "weekly.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--this-week", required=True)
    p.add_argument("--last-week", required=True)
    p.add_argument("--mtd")
    p.add_argument("--month-budget", type=Decimal)
    p.add_argument("--as-of")
    p.add_argument("--min-move", type=Decimal, default=Decimal("50"))
    p.add_argument("--planned-change-pct", type=Decimal,
                   help="the owner's planned week-over-week spend change, e.g. -20; moves within 5 points of it are not findings")
    p.add_argument("--out", default="outputs/weekly")
    a = p.parse_args(argv)
    notes = []
    try:
        tw, twe = load(a.this_week)
        lw, lwe = load(a.last_week)
        if twe.days and lwe.days and twe.days != lwe.days:
            raise CsvInputError(f"the two weeks cover different lengths ({lwe.days} and {twe.days} days). Export the same number of days for both.")
        if twe.date_range and lwe.date_range and twe.date_range == lwe.date_range:
            raise CsvInputError("both files cover the same dates. Upload last week and this week.")
        planned = a.planned_change_pct / 100 if a.planned_change_pct is not None else None
        findings = review(tw, lw, a.min_move, planned)
        if planned is not None:
            notes.append(f"Spend moves within 5 points of the planned {a.planned_change_pct}% change are treated as the plan, not as findings.")
        if a.mtd:
            if a.month_budget is None or not a.as_of:
                raise CsvInputError("pacing needs --month-budget (the owner's stated figure) and --as-of")
            mtd, _ = load(a.mtd)
            findings.insert(0, pacing(mtd, a.month_budget, date.fromisoformat(a.as_of)))
        else:
            notes.append("Budget pacing: upload a month-to-date Campaigns export and give the monthly budget.")
    except (CsvInputError, ValueError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    render(findings, Path(a.out), tw, lw, notes)
    print(f"wrote weekly.md and findings.csv to {a.out} ({len(findings)} findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
