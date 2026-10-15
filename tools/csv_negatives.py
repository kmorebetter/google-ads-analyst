#!/usr/bin/env python3
"""Negative keyword candidates from a Google Ads UI search-terms export. No API.

Run:
  python3 tools/csv_negatives.py EXPORT_30D.csv \
      --min-spend 50 --min-clicks 10 --target-cpa 120 --target-cpa-multiple 3 \
      --clearance-export EXPORT_90D.csv \
      [--brand-token acme ...] [--protect-term "invoice app" ...] [--out DIR]

The CSV-upload path behind the Negative Keyword Miner template. It reads the
Search terms report from Google Ads (Insights and reports > Search terms >
Download, CSV or "Excel CSV") and applies the numbers-only rule from
`search-term-methodology`:

* the four thresholds are the account owner's; none has a default, and the
  script refuses to run without all four;
* a term is decided by its spend, clicks and conversions, never by its words;
* a term already Added or Excluded, containing a brand token, or on the
  protect list is never a candidate.

Decisions (on the main export):
  NEGATE  zero conversions, spend >= min-spend and clicks >= min-clicks
  HOLD    zero conversions, spend >= min-spend but clicks < min-clicks
  CUT     converting, but cost per conversion > target-cpa x multiple
  KEEP    everything else
  CLEARED would be NEGATE or CUT, but the same search term (any campaign)
          converts at or under the cut line in the --clearance-export.
  REVIEW  a competitor term (--competitor-token) in a competitor/conquest
          campaign: that is the campaign's job, so it is routed to the owner,
          not negated. Navigational competitor terms (login, support, phone
          number, password, cancel...) are existing customers and stay NEGATE.

Brand protection matches brand tokens and close misspellings ("acmee",
"akme") using a sound-alike key, without catching look-alikes ("acne",
"academy").

90-day cross-check (--clearance-export): a 30-day file cannot see a sale from
day 31 to 90. Pass a second export covering a longer range (90 days
recommended). The check joins on the search term text alone, account-wide,
because a term converting in one campaign must not be negated in another.
The clearance file must cover at least as many days as the main file when
both carry a date range; otherwise the run refuses. Without it, negatives.csv
is still written but summary.md and the exit message flag the list as
UNCLEARED.

Writes decisions.csv, negatives.csv (Google Ads Editor shape, exact negatives
at ad-group level) and summary.md. Standard library only. Read-only.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import gads_csv as G
from gads_csv import CsvInputError  # re-exported for callers and tests

REQUIRED = {
    "search_term": ("search term", "search terms", "query"),
    "campaign": ("campaign",),
    "ad_group": ("ad group",),
    "clicks": ("clicks",),
    "cost": ("cost",),
    "conversions": ("conversions", "conv."),
}
OPTIONAL = {
    "match_type": ("match type", "search term match type"),
    "status": ("added/excluded", "added / excluded", "status"),
    "impressions": ("impr.", "impressions"),
    "currency": ("currency code", "currency"),
}
NEGATIVE_DECISIONS = ("NEGATE", "CUT")


@dataclass
class Thresholds:
    min_spend: Decimal
    min_clicks: int
    target_cpa: Decimal
    target_cpa_multiple: Decimal


@dataclass
class Row:
    search_term: str
    campaign: str
    ad_group: str
    clicks: int
    cost: Decimal
    conversions: Decimal
    status: str = ""
    match_type: str = ""
    currency: str = ""
    decision: str = ""
    reason: str = ""


def norm(term: str) -> str:
    return " ".join(term.lower().split())


NAVIGATIONAL = ("login", "log in", "sign in", "signin", "password", "support", "customer service",
                "phone number", "contact", "cancel", "refund", "help center", "status", "down")
CONQUEST_CAMPAIGN = ("conquest", "competitor", "competitive", "rival", "vs ")
FORBID = (("campaign type", "Campaigns"), ("ad strength", "Ads"), ("asset type", "Assets"))


def sound_key(word: str) -> str:
    """Rough sound-alike key: k->c, ph->f, collapse doubles, drop final e/es."""
    w = re.sub(r"[^a-z0-9]", "", word.lower())
    w = w.replace("ph", "f").replace("ck", "c").replace("k", "c").replace("q", "c")
    w = re.sub(r"(.)\1+", r"\1", w)
    w = re.sub(r"e+s?$", "", w) if len(w) > 3 else w
    return w


def brand_hit(term: str, brand_tokens) -> "str | None":
    words = term.split()
    joined = "".join(words)
    for token in brand_tokens:
        t = norm(token)
        if not t:
            continue
        if t in term or t.replace(" ", "") in joined:
            return token
        first = t.split()[0]
        key = sound_key(first)
        if len(key) >= 3 and any(sound_key(w) == key for w in words):
            return token
    return None


def load(raw: bytes) -> "tuple[list[Row], G.Export]":
    exp = G.read(raw, REQUIRED, OPTIONAL, "Search terms", FORBID)
    rows = [Row(
        search_term=r["search_term"],
        campaign=r["campaign"],
        ad_group=r["ad_group"],
        clicks=int(G.number(r["clicks"])),
        cost=G.number(r["cost"]),
        conversions=G.number(r["conversions"]),
        status=r.get("status", ""),
        match_type=r.get("match_type", ""),
        currency=r.get("currency", ""),
    ) for r in exp.rows]
    return rows, exp


def load_path(path) -> "tuple[list[Row], G.Export]":
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        raise CsvInputError(f"cannot open {path}: {e.strerror or e}")
    return load(raw)


def read_export(raw: bytes) -> "list[Row]":
    return load(raw)[0]


def converting_terms(rows: "list[Row]") -> "dict[str, Decimal]":
    out: "dict[str, Decimal]" = {}
    for r in rows:
        if r.conversions > 0:
            out[norm(r.search_term)] = out.get(norm(r.search_term), Decimal("0")) + r.conversions
    return out


def term_totals(rows: "list[Row]") -> "dict[str, tuple[Decimal, Decimal]]":
    """Account-wide (conversions, cost) per normalised term."""
    out: dict = {}
    for r in rows:
        c, s = out.get(norm(r.search_term), (Decimal("0"), Decimal("0")))
        out[norm(r.search_term)] = (c + r.conversions, s + r.cost)
    return out


def decide(row: Row, t: Thresholds, brand_tokens=(), protect=(), competitor_tokens=()) -> "tuple[str, str]":
    term = norm(row.search_term)
    status = row.status.lower()
    if status.startswith(("added", "excluded")):
        return "KEEP", f"already {status}"
    hit = brand_hit(term, brand_tokens)
    if hit:
        return "KEEP", f"brand term (matches '{hit}'); never negated"
    if term in {norm(p) for p in protect}:
        return "KEEP", "on the protect list"
    comp = next((c for c in competitor_tokens if c and norm(c) in term), None)
    if comp and any(w in row.campaign.lower() for w in CONQUEST_CAMPAIGN):
        if not any(n in term for n in NAVIGATIONAL):
            return "REVIEW", (f"competitor term ('{comp}') in a competitor campaign; that is its "
                              "job, so the owner decides, not the numbers alone")
    if row.conversions == 0:
        if row.cost >= t.min_spend and row.clicks >= t.min_clicks and len(term.split()) == 1:
            return "REVIEW", (f"0 conversions on {row.cost} spend, but a one-word term is usually "
                              "your core topic. Fix the keyword or match type instead of an exact negative")
        if row.cost >= t.min_spend and row.clicks >= t.min_clicks:
            return "NEGATE", (f"0 conversions on {row.cost} spend and {row.clicks} "
                              f"clicks (thresholds {t.min_spend} / {t.min_clicks})")
        if row.cost >= t.min_spend:
            return "HOLD", (f"0 conversions on {row.cost} spend but only "
                            f"{row.clicks} clicks; below {t.min_clicks}, the zero "
                            "is not trusted yet")
        return "KEEP", f"spend {row.cost} below {t.min_spend}"
    cpa = row.cost / row.conversions
    ceiling = t.target_cpa * t.target_cpa_multiple
    if cpa > ceiling:
        return "CUT", (f"cost per conversion {cpa.quantize(Decimal('0.01'))} is above "
                       f"{ceiling} ({t.target_cpa_multiple}x target {t.target_cpa})")
    return "KEEP", f"converting at {cpa.quantize(Decimal('0.01'))} per conversion"


def run(rows, t, brand_tokens=(), protect=(), clearance: "dict | None" = None, competitor_tokens=()):
    """Decide every row. Term-scoped safety: a term that converts anywhere in
    the main export is never NEGATE in another campaign; a term converting in
    the clearance export is CLEARED."""
    in_window = converting_terms(rows)
    for r in rows:
        r.decision, r.reason = decide(r, t, brand_tokens, protect, competitor_tokens)
        key = norm(r.search_term)
        if r.decision == "NEGATE" and key in in_window:
            r.decision = "KEEP"
            r.reason = (f"0 conversions here, but the same term has {in_window[key]} "
                        "conversions elsewhere in this export (term-scoped safety)")
        if clearance is not None and r.decision in NEGATIVE_DECISIONS and key in clearance:
            conv, cost = clearance[key] if isinstance(clearance[key], tuple) else (clearance[key], Decimal("0"))
            ceiling = t.target_cpa * t.target_cpa_multiple
            if conv > 0 and (cost == 0 or cost / conv <= ceiling):
                r.reason = (f"was {r.decision}: {r.reason}; but the term has {conv} conversions "
                            "in the clearance export at an acceptable cost")
                r.decision = "CLEARED"
            elif conv > 0:
                r.reason += (f"; the clearance export also shows {conv} conversions at "
                             f"{(cost / conv).quantize(Decimal('0.01'))} each, still above the cut line")
    return rows


def write_outputs(rows, t, out: Path, source_name: str, exp: "G.Export | None" = None,
                  clearance_note: "str | None" = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "decisions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Search term", "Campaign", "Ad group", "Clicks", "Cost",
                    "Conversions", "Decision", "Reason"])
        for r in sorted(rows, key=lambda r: (-r.cost, r.search_term)):
            w.writerow([r.search_term, r.campaign, r.ad_group, r.clicks, r.cost,
                        r.conversions, r.decision, r.reason])
    neg = [r for r in rows if r.decision in NEGATIVE_DECISIONS]
    seen = set()
    with open(out / "negatives.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Campaign", "Ad group", "Keyword", "Criterion Type"])
        for r in sorted(neg, key=lambda r: (r.campaign, r.ad_group, norm(r.search_term))):
            key = (r.campaign, r.ad_group, norm(r.search_term))
            if key in seen:
                continue
            seen.add(key)
            w.writerow([r.campaign, r.ad_group, f"[{norm(r.search_term)}]", "Negative exact"])
    order = ("NEGATE", "CUT", "HOLD", "REVIEW", "CLEARED", "KEEP")
    counts = {d: sum(1 for r in rows if r.decision == d) for d in order}
    spend = {d: sum((r.cost for r in rows if r.decision == d), Decimal("0")) for d in order}
    currencies = sorted({r.currency for r in rows if r.currency}) or ["(not in export)"]
    top = sorted(neg, key=lambda r: -r.cost)[:10]
    rng = ""
    if exp and exp.date_range:
        rng = f" Date range: {exp.date_range[0]} to {exp.date_range[1]} ({exp.days} days)."
    lines = [
        "# Negative keyword candidates (from CSV export)",
        "",
        f"Source file: `{source_name}`. Rows read: {len(rows)}.{rng} Currency: {', '.join(currencies)}.",
        f"Thresholds (set by the account owner): min spend {t.min_spend}, min clicks "
        f"{t.min_clicks}, target CPA {t.target_cpa}, cut at {t.target_cpa_multiple}x.",
        "",
        f"**Clearance:** {clearance_note or 'UNCLEARED. No clearance export was given, so a term that converted outside this date range can still be on the list. Rerun with --clearance-export (90 days) before uploading.'}",
        "",
        G.md_table(["Decision", "Terms", "Spend"], [[d, counts[d], G.money(spend[d])] for d in order]),
        "",
        f"negatives.csv rows: {len(seen)}",
        "",
        "## Top 10 by spend",
        "",
        G.md_table(["Search term", "Campaign", "Cost", "Clicks", "Conv.", "Decision"],
                   [[r.search_term, r.campaign, G.money(r.cost), r.clicks, r.conversions, r.decision]
                    for r in top]) if top else "(no negative candidates)",
        "",
        "## Before you upload",
        "",
        "- HOLD rows are not negatives. Look again when they have more clicks.",
        "- CLEARED rows converted in the longer export and are never negatives.",
        "- REVIEW rows are competitor searches in your competitor campaign. You decide those.",
        "- Nothing was changed in your account. Upload `negatives.csv` yourself in "
        "Google Ads Editor or the UI if you agree with it.",
    ]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"counts": counts, "negatives": len(seen)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("export")
    p.add_argument("--min-spend", type=Decimal)
    p.add_argument("--min-clicks", type=int)
    p.add_argument("--target-cpa", type=Decimal)
    p.add_argument("--target-cpa-multiple", type=Decimal)
    p.add_argument("--clearance-export", help="longer-range Search terms export (90 days recommended)")
    p.add_argument("--brand-token", action="append", default=[])
    p.add_argument("--protect-term", action="append", default=[])
    p.add_argument("--competitor-token", action="append", default=[])
    p.add_argument("--out", default="outputs/csv")
    a = p.parse_args(argv)
    missing = [f for f, v in (("--min-spend", a.min_spend), ("--min-clicks", a.min_clicks),
                              ("--target-cpa", a.target_cpa),
                              ("--target-cpa-multiple", a.target_cpa_multiple)) if v is None]
    if missing:
        print("REFUSING to decide anything. The thresholds are the account owner's "
              "to set and none has a default. Missing: " + ", ".join(missing),
              file=sys.stderr)
        return 2
    t = Thresholds(a.min_spend, a.min_clicks, a.target_cpa, a.target_cpa_multiple)
    try:
        rows, exp = load_path(a.export)
        clearance, note = None, None
        if a.clearance_export:
            crow, cexp = load_path(a.clearance_export)
            if exp.days and cexp.days and cexp.days < exp.days:
                raise CsvInputError(
                    f"the clearance export covers {cexp.days} days but the main export "
                    f"covers {exp.days}. The clearance file must cover the longer range.")
            clearance = {k: v for k, v in term_totals(crow).items() if v[0] > 0}
            span = f"{cexp.days} days" if cexp.days else "an unstated date range"
            note = (f"CLEARED against `{Path(a.clearance_export).name}` ({span}, "
                    f"{len(crow)} rows, {len(clearance)} converting terms).")
            if cexp.days is not None and cexp.days < 90:
                note += f" Warning: {cexp.days} days is shorter than the recommended 90."
            if cexp.days is None:
                note += " Warning: the clearance file has no date range line; confirm it covers 90 days."
    except CsvInputError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    run(rows, t, a.brand_token, a.protect_term, clearance, a.competitor_token)
    res = write_outputs(rows, t, Path(a.out), Path(a.export).name, exp, note)
    flag = "" if clearance is not None else " (UNCLEARED: rerun with --clearance-export)"
    print(f"wrote decisions.csv, negatives.csv ({res['negatives']} rows), summary.md to {a.out}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
