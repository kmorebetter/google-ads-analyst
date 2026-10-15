#!/usr/bin/env python3
"""One-time Google Ads account audit from UI exports. No API. Read-only.

Run:
  python3 tools/csv_audit.py --campaigns CAMPAIGNS.csv [--ads ADS.csv] \
      [--assets ASSETS.csv] [--search-terms ST_30D.csv --clearance-export ST_90D.csv \
       --min-spend 50 --min-clicks 10 --target-cpa 120 --target-cpa-multiple 3] \
      [--target-cpa 120] [--brand-token acme ...] [--out DIR]

The engine behind the Google Ads Auditor template. Each check maps to a rule in
`campaign-guardrails`, `ad-copy-principles` or `conversion-measurement`:

  ZERO_CONV_SPEND   a Search/Shopping/PMax campaign spent >= min spend with 0 conversions
  HIGH_CPA          CPA above target x multiple, with >= 5 conversions (low-volume floor)
  LOW_DATA          < 5 conversions: no CPA or budget call is made (guardrail 5)
  LOST_IS_BUDGET    Search lost impression share (budget) >= 20%
  TOF_NOT_JUDGED    Video / Demand Gen / Display: never judged on CPA (guardrail 1)
  BRAND_NOT_LEVER   brand campaigns are never a growth lever (guardrail 2)
  POOR_AD_STRENGTH  enabled RSA rated Poor
  FEW_HEADLINES     enabled RSA with fewer than 8 headlines
  SINGLE_RSA        enabled ad group with only one enabled RSA
  AD_POLICY         ad disapproved or limited by policy
  KEYWORD_INSERTION_CONQUEST  {KeyWord:...} in a competitor/conquest campaign
  MISSING_SITELINKS / MISSING_CALLOUTS / MISSING_SNIPPETS
                    enabled Search campaign with none at campaign or account level
  NEGATIVE_CANDIDATES  summary of csv_negatives on the search terms export

Writes audit.md (plain language), findings.csv and change-list.csv (actions
for the owner to apply; nothing is applied).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import gads_csv as G
from gads_csv import CsvInputError

HERE = Path(__file__).resolve().parent

CAMP_REQ = {"campaign": ("campaign",), "cost": ("cost",), "conversions": ("conversions", "conv.")}
CAMP_OPT = {
    "campaign_id": ("campaign id",),
    "type": ("campaign type", "advertising channel type", "type"),
    "status": ("campaign status", "status"),
    "budget": ("budget", "daily budget"),
    "clicks": ("clicks",),
    "impressions": ("impr.", "impressions"),
    "conv_value": ("conv. value", "conversion value", "all conv. value"),
    "lost_is_budget": ("search lost is (budget)", "search lost impr. share (budget)"),
    "currency": ("currency code", "currency"),
}
ADS_REQ = {"campaign": ("campaign",), "ad_group": ("ad group",), "ad_type": ("ad type",)}
ADS_OPT = {
    "status": ("ad status", "status"),
    "strength": ("ad strength",),
    "policy": ("policy details", "approval status", "status reasons"),
    "ad_id": ("ad id",),
    **{f"h{i}": (f"headline {i}",) for i in range(1, 16)},
}
ASSET_REQ = {"asset_type": ("asset type", "type")}
ASSET_OPT = {"level": ("level", "asset level", "added to"), "campaign": ("campaign",),
             "status": ("status", "asset status")}

CAMP_FORBID = (("search term", "Search terms"), ("ad type", "Ads"), ("asset type", "Assets"))
TOF_TYPES = ("video", "demand gen", "display", "discovery")
CONQUEST_WORDS = ("conquest", "competitor", "competitive", "rival")
MIN_CONV = 5


@dataclass
class Finding:
    code: str
    entity: str
    severity: str           # high / medium / info
    evidence: str
    action: str

    def as_row(self):
        return [self.code, self.entity, self.severity, self.evidence, self.action]


def _config():
    try:
        return json.loads((HERE / "account-config.json").read_text())
    except Exception:
        return {}


def _conv(n) -> str:
    return f"{n} conversion" + ("" if n == 1 else "s")


def _enabled(status: str) -> bool:
    s = (status or "").lower()
    return not s or s.startswith(("enabled", "eligible", "active", "limited"))


def _is_conquest(name: str, extra=()) -> bool:
    n = name.lower()
    return any(w in n for w in tuple(CONQUEST_WORDS) + tuple(t.lower() for t in extra))


def _is_brand(row: dict, brand_ids, brand_tokens) -> bool:
    """Brand by campaign ID from the account profile, else by the word
    brand/branded in the name. Never by the company name: most accounts put
    the company name in every campaign."""
    if row.get("campaign_id") and row["campaign_id"] in brand_ids:
        return True
    n = row["campaign"].lower()
    if re.search(r"non[\s\-]?brand", n):
        return False
    return bool(re.search(r"\bbrand(ed)?\b", n))


def audit_campaigns(exp: G.Export, target_cpa, multiple, min_spend, brand_ids, brand_tokens, conquest_tokens=()):
    out = []
    search_campaigns = []
    for r in exp.rows:
        if not _enabled(r.get("status", "")):
            continue
        name = r["campaign"]
        ctype = (r.get("type") or "").lower()
        cost = G.number(r["cost"])
        conv = G.number(r["conversions"])
        if any(t in ctype for t in TOF_TYPES):
            out.append(Finding("TOF_NOT_JUDGED", name, "info",
                               f"{ctype.title()} campaign, spend {G.money(cost)}, {conv} conversions",
                               "Judge it on reach and engagement, not cost per conversion. No change suggested."))
            continue
        if not ctype or "search" in ctype:
            search_campaigns.append(name)
        if _is_brand(r, brand_ids, brand_tokens):
            out.append(Finding("BRAND_NOT_LEVER", name, "info",
                               f"brand campaign, spend {G.money(cost)}, {conv} conversions",
                               "Keep it on brand terms only. Do not add budget to grow the account through brand."))
            continue
        if conv == 0:
            if cost >= min_spend:
                out.append(Finding("ZERO_CONV_SPEND", name, "high",
                                   f"spent {G.money(cost)} with 0 conversions",
                                   "Check conversion tracking first. If tracking is fine, review its search terms and keywords, then cut or pause the waste."))
            continue
        if conv < MIN_CONV and target_cpa and multiple and not _is_conquest(name, conquest_tokens) and cost >= target_cpa * multiple * 2 \
                and cost / conv > target_cpa * multiple:
            out.append(Finding("HIGH_COST_LOW_CONV", name, "high",
                               f"spent {G.money(cost)} for {_conv(conv)} ({G.money(cost / conv)} each, target {G.money(target_cpa)})",
                               "Review its search terms and landing page now, and cap its budget until it converts. It has too few conversions to tune bids, but enough spend to show a problem."))
            continue
        if conv < MIN_CONV:
            out.append(Finding("LOW_DATA", name, "info",
                               f"only {_conv(conv)} (fewer than {MIN_CONV})",
                               "Too little data for a bid or budget change. Wait for more conversions."))
            continue
        cpa = cost / conv
        if target_cpa and cpa > target_cpa * multiple:
            out.append(Finding("HIGH_CPA", name, "high",
                               f"cost per conversion {G.money(cpa)} vs target {G.money(target_cpa)} ({conv} conversions)",
                               "Tighten the bid target or trim the worst keywords and search terms before adding budget."))
        lost = r.get("lost_is_budget")
        if lost:
            lis = G.number(lost)
            lis = lis / 100 if lis > 1 else lis
            if lis >= Decimal("0.20"):
                good = target_cpa and cpa <= target_cpa
                out.append(Finding("LOST_IS_BUDGET", name, "medium",
                                   f"loses {int(lis * 100)}% of impressions to budget, cost per conversion {G.money(cpa)}",
                                   ("Add budget here first. It converts below your target, so raise its daily budget or move money here from a weaker campaign."
                                    if good else "Fix cost per conversion before adding budget, since it is not beating target.")))
    return out, search_campaigns


def audit_ads(exp: G.Export, conquest_tokens=(), notes=None):
    out = []
    head_cols = sum(1 for i in range(1, 16) if f"headline {i}" in exp.header)
    if head_cols and head_cols < 8 and notes is not None:
        notes.append(f"Headline counts: the Ads export has only {head_cols} headline columns. "
                     "Add Headline 1 to Headline 15 in Columns to check how many headlines each ad has.")
    per_group = defaultdict(int)
    for r in exp.rows:
        if "responsive search" not in r["ad_type"].lower():
            continue
        status = (r.get("status") or "").lower()
        policy = (r.get("policy") or "").lower()
        ent = f"{r['campaign']} > {r['ad_group']}" + (f" (ad {r['ad_id']})" if r.get("ad_id") else "")
        live = status.startswith(("enabled", "eligible", "approved", "limited", "disapproved")) or not status
        if not live:
            continue   # paused or removed ads are not findings
        if "disapproved" in status or "disapproved" in policy or "limited" in status or "limited" in policy:
            out.append(Finding("AD_POLICY", ent, "high", f"status '{r.get('status')}' {r.get('policy','')}".strip(),
                               "Open the ad's policy details in Google Ads and fix or replace the flagged text."))
        if "disapproved" in status:
            continue
        per_group[(r["campaign"], r["ad_group"])] += 1
        heads = [r.get(f"h{i}", "") for i in range(1, 16)]
        heads = [h for h in heads if h and h != "--"]
        if (r.get("strength") or "").lower() == "poor":
            out.append(Finding("POOR_AD_STRENGTH", ent, "medium", "ad strength Poor",
                               "Add more distinct headlines and descriptions. Ad strength is a variety score, so also check spelling and policy yourself."))
        if heads and len(heads) < 8 and head_cols >= 8:
            out.append(Finding("FEW_HEADLINES", ent, "medium", f"{len(heads)} headlines (15 allowed)",
                               "Add headlines until there are at least 8, each saying something different."))
        if _is_conquest(r["campaign"], conquest_tokens) and any("{keyword:" in h.lower() for h in heads):
            out.append(Finding("KEYWORD_INSERTION_CONQUEST", ent, "high",
                               "keyword insertion in a competitor campaign",
                               "Remove keyword insertion here. It can put a competitor's brand name in your headline."))
    for (camp, grp), n in sorted(per_group.items()):
        if n == 1:
            out.append(Finding("SINGLE_RSA", f"{camp} > {grp}", "medium", "only 1 enabled responsive search ad",
                               "Add a second ad with a different angle so Google has something to compare."))
    return out


ASSET_KINDS = {"MISSING_SITELINKS": ("sitelink",), "MISSING_CALLOUTS": ("callout",),
               "MISSING_SNIPPETS": ("structured snippet", "snippet")}


def audit_assets(exp: G.Export, search_campaigns):
    have_account, have_campaign = set(), defaultdict(set)
    for r in exp.rows:
        if r.get("status") and r["status"].lower().startswith(("removed", "paused")):
            continue
        kind = r["asset_type"].lower()
        level = (r.get("level") or "").lower()
        camp = r.get("campaign") or ""
        for code, words in ASSET_KINDS.items():
            if any(w in kind for w in words):
                if "account" in level or (not camp or camp == "--" or camp.lower().startswith("all")):
                    have_account.add(code)
                else:
                    have_campaign[camp].add(code)
    out = []
    for code in ASSET_KINDS:
        missing = [c for c in search_campaigns
                   if code not in have_account and code not in have_campaign.get(c, set())]
        label = code.split("_", 1)[1].lower()
        nice = "structured snippets" if label == "snippets" else label
        if len(missing) >= 3 and len(missing) == len(search_campaigns):
            out.append(Finding(code, f"Account (all {len(missing)} search campaigns)", "medium",
                               f"no {nice} anywhere",
                               f"Add at least 4 {nice} once at account level. That covers every campaign."))
            continue
        for camp in missing:
            out.append(Finding(code, camp, "medium", f"no {nice} at campaign or account level",
                               f"Add at least 4 {nice} to this campaign or to the whole account."))
    return out


def render(findings, notes, out: Path, sources):
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "findings.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Code", "Where", "Severity", "Evidence", "What to do"])
        for x in findings:
            w.writerow(x.as_row())
    with open(out / "change-list.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Priority", "Where", "Change", "Why"])
        for x in findings:
            if x.severity in ("high", "medium"):
                w.writerow([x.severity, x.entity, x.action, x.evidence])
    sev = {"high": 0, "medium": 1, "info": 2}
    lines = ["# Google Ads audit", "",
             "This audit reads the files you uploaded. It did not change anything in your account.",
             "", "Files: " + ", ".join(f"`{s}`" for s in sources) + ".", ""]
    highs = [x for x in findings if x.severity == "high"]
    lines += ["## The short version", ""]
    if highs:
        lines += [f"- {len(highs)} high-priority problems. Start with these."]
    lines += [f"- {sum(1 for x in findings if x.severity == 'medium')} medium fixes.",
              f"- {sum(1 for x in findings if x.severity == 'info')} notes that need no change.", ""]
    for title, level in (("Fix first", "high"), ("Fix next", "medium"), ("Notes", "info")):
        group = [x for x in findings if x.severity == level]
        if not group:
            continue
        lines += [f"## {title}", ""]
        for x in group:
            lines += [f"- **{x.entity}**: {x.evidence}. What to do: {x.action}"]
        lines += [""]
    if notes:
        lines += ["## What this audit could not check", ""] + [f"- {n}" for n in notes] + [""]
    (out / "audit.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--campaigns", required=True)
    p.add_argument("--ads")
    p.add_argument("--assets")
    p.add_argument("--search-terms")
    p.add_argument("--clearance-export")
    p.add_argument("--min-spend", type=Decimal)
    p.add_argument("--min-clicks", type=int)
    p.add_argument("--target-cpa", type=Decimal)
    p.add_argument("--target-cpa-multiple", type=Decimal)
    p.add_argument("--brand-token", action="append", default=[])
    p.add_argument("--out", default="outputs/audit")
    a = p.parse_args(argv)
    cfg = _config()
    cb = cfg.get("campaign_buckets", {})
    brand_ids = set(cb.get("brand_campaign_ids", {}))
    brand_tokens = a.brand_token
    conquest_tokens = cb.get("conquest_tokens", [])
    if a.target_cpa is None:
        print("REFUSED: --target-cpa is required. It is the owner's number; the audit "
              "will not judge cost per conversion without it.", file=sys.stderr)
        return 2
    multiple = a.target_cpa_multiple or Decimal("2")
    min_spend = a.min_spend if a.min_spend is not None else a.target_cpa
    findings, notes, sources = [], [], [Path(a.campaigns).name]
    try:
        camp = G.read_file(a.campaigns, CAMP_REQ, CAMP_OPT, "Campaigns", CAMP_FORBID)
        f, search = audit_campaigns(camp, a.target_cpa, multiple, min_spend, brand_ids, brand_tokens, conquest_tokens)
        findings += f
        if "lost_is_budget" not in camp.rows[0]:
            notes.append("Budget limits: add the 'Search lost IS (budget)' column to the Campaigns export.")
        if a.ads:
            sources.append(Path(a.ads).name)
            findings += audit_ads(G.read_file(a.ads, ADS_REQ, ADS_OPT, "Ads"), conquest_tokens, notes)
        else:
            notes.append("Ad copy: upload the Ads export to check ad strength, headlines and policy.")
        if a.assets:
            sources.append(Path(a.assets).name)
            findings += audit_assets(G.read_file(a.assets, ASSET_REQ, ASSET_OPT, "Assets"), search)
        else:
            notes.append("Sitelinks, callouts and snippets: upload the Assets export.")
        if a.search_terms:
            import csv_negatives as N
            if None in (a.min_spend, a.min_clicks, a.target_cpa_multiple):
                notes.append("Search terms: skipped. Negative keyword thresholds were not all given.")
            else:
                sources.append(Path(a.search_terms).name)
                t = N.Thresholds(a.min_spend, a.min_clicks, a.target_cpa, a.target_cpa_multiple)
                rows, _ = N.load_path(a.search_terms)
                clear = None
                if a.clearance_export:
                    clear = {k: v for k, v in N.term_totals(N.load_path(a.clearance_export)[0]).items() if v[0] > 0}
                N.run(rows, t, brand_tokens, (), clear)
                neg = [r for r in rows if r.decision in N.NEGATIVE_DECISIONS]
                if neg:
                    waste = sum((r.cost for r in neg), Decimal("0"))
                    findings.append(Finding("NEGATIVE_CANDIDATES", "Search terms", "high",
                                            f"{len(neg)} search terms cost {G.money(waste)} without paying back",
                                            "Review negatives.csv from the Negative Keyword Miner and upload the ones you agree with."))
                if clear is None:
                    notes.append("Search terms were not checked against a 90-day export.")
        notes.append("Performance Max channel and search-category splits are only in the Google Ads website.")
    except CsvInputError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    render(findings, notes, Path(a.out), sources)
    print(f"wrote audit.md, findings.csv, change-list.csv to {a.out} ({len(findings)} findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
