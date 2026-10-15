#!/usr/bin/env python3
"""Graded eval scenarios for the three bot templates.

Every scenario is synthetic but shaped like a real Google Ads UI export:
report-title and date-range lines, thousands separators, currency symbols,
"--" for empty, quoted names with commas, "Total:" rows, UTF-16 "Excel CSV"
files, segmented duplicate rows, renamed campaigns, and so on.

Answer keys are written the way a senior PPC reviewer would grade the work,
not derived from the tools' code:

  Negatives:  NEG    an expert would add this exact negative
              KEEP   an expert would NOT negate it (brand, misspelled brand,
                     competitor research in a conquest campaign, converting
                     elsewhere, converted in the 90-day file, too few clicks)
              EITHER defensible both ways; excluded from scoring
  Audit/Weekly: `expect` is the set of (code, where) findings an expert would
              raise; `either` may be raised or not; anything else actionable
              that the tool raises is a false positive.

Large scenarios are generated from a seeded random model whose ground truth
is fixed before the tool runs.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

ST_HEAD = ["Search term", "Match type", "Added/Excluded", "Campaign", "Ad group", "Clicks",
           "Impr.", "CTR", "Currency code", "Avg. CPC", "Cost", "Conversions", "Cost / conv."]


def _q(v):
    v = str(v)
    return f'"{v}"' if ("," in v or '"' in v) else v


def export(title, date_line, head, rows, total=True, sep=","):
    lines = [title, date_line if sep == "," else date_line.replace('"', ""),
             sep.join(head if sep == "\t" else [_q(h) for h in head])]
    for r in rows:
        lines.append(sep.join(str(c) if sep == "\t" else _q(c) for c in r))
    if total:
        lines.append(sep.join(["Total: " + title.split(" report")[0]] + [""] * (len(head) - 1)))
    return "\n".join(lines) + "\n"


def money(x):
    return f"{x:,.2f}"


def st_row(term, camp, group, clicks, cost, conv, status="None", match="Broad match", impr=None):
    impr = impr or max(clicks * 12, 1)
    cpc = money(cost / clicks) if clicks else "--"
    cpa = money(cost / conv) if conv else "0.00"
    return [term, match, status, camp, group, f"{clicks:,}", f"{impr:,}",
            f"{(clicks / impr * 100 if impr else 0):.2f}%", "USD", cpc, money(cost), f"{conv:.2f}", cpa]


D30 = '"September 1, 2026 - September 30, 2026"'
D90 = '"July 3, 2026 - September 30, 2026"'
D7 = '"October 5, 2026 - October 11, 2026"'


@dataclass
class NegScenario:
    name: str
    files: dict                      # filename -> bytes
    args: list                       # CLI args (paths relative to scenario dir)
    labels: dict                     # (campaign, ad_group, norm term) -> NEG/KEEP/EITHER
    note: str = ""


@dataclass
class FindScenario:
    name: str
    files: dict
    args: list
    expect: set
    either: set = field(default_factory=set)
    note: str = ""


@dataclass
class BadScenario:
    name: str
    tool: str                        # negatives / audit / weekly
    files: dict
    args: list
    note: str = ""


def _norm(t):
    return " ".join(t.lower().split())


def neg_scenarios():
    out = []
    G, B, C = "Search - Generic - Invoicing", "Search - Brand - Core", "Search - Conquest - RivalCo"
    TH = ["--min-spend", "50", "--min-clicks", "10", "--target-cpa", "120", "--target-cpa-multiple", "3",
          "--brand-token", "acme"]

    # N1: small messy export
    rows = [
        (st_row("free invoice template word", G, "Templates", 35, 70.0, 0), "NEG"),
        (st_row("invoice generator", G, "Generators", 4, 60.0, 0), "KEEP"),          # too few clicks: hold
        (st_row("small business invoicing software", G, "Software", 50, 200.0, 2), "KEEP"),
        (st_row("how to write an invoice", G, "Informational", 30, 1000.0, 1), "NEG"),  # CPA 1000 > 360
        (st_row("cheap invoice", G, "Generators", 2, 10.0, 0), "KEEP"),
        (st_row("invoice clerk jobs", G, "Software", 18, 64.0, 0), "NEG"),
        (st_row("invoicing app, free trial", G, "Software", 22, 88.0, 0), "NEG"),     # comma in term
        (st_row("acme invoicing", B, "Brand", 120, 96.0, 30, status="Added", match="Exact match"), "KEEP"),
        (st_row("acme login", B, "Brand", 40, 52.0, 0), "KEEP"),                      # brand
        (st_row("invoice factoring", G, "Software", 11, 49.99, 0), "KEEP"),           # just under spend floor
        (st_row("invoice ninja", G, "Software", 12, 75.0, 0, status="Excluded"), "KEEP"),
    ]
    out.append(NegScenario("N1 small messy export",
        {"st30.csv": export("Search terms report", D30, ST_HEAD, [r for r, _ in rows]).encode()},
        ["st30.csv"] + TH,
        {(r[3], r[4], _norm(r[0])): l for r, l in rows},
        "title/date lines, totals, commas in terms, Added/Excluded, just-under-floor"))

    # N2: brand, misspelled brand, competitor terms
    rows = [
        (st_row("acmee invoicing", G, "Software", 20, 80.0, 0), "KEEP"),          # misspelled brand
        (st_row("akme invoice app", G, "Software", 14, 66.0, 0), "KEEP"),         # misspelled brand
        (st_row("acme software reviews", G, "Software", 25, 90.0, 0), "KEEP"),    # brand research
        (st_row("rivalco invoice software", C, "RivalCo", 25, 120.0, 0), "KEEP"),  # conquest purpose
        (st_row("rivalco vs acme", C, "RivalCo", 16, 70.0, 0), "KEEP"),
        (st_row("rivalco login", C, "RivalCo", 30, 95.0, 0), "NEG"),              # navigational: existing customer
        (st_row("rivalco customer service phone number", C, "RivalCo", 12, 55.0, 0), "NEG"),
        (st_row("rivalco login", G, "Software", 15, 90.0, 0), "NEG"),
        (st_row("rivalco pricing", G, "Software", 13, 60.0, 0), "EITHER"),
        (st_row("academy invoice course", G, "Software", 21, 84.0, 0), "NEG"),    # not brand (looks like 'acme')
        (st_row("acne cream", G, "Software", 19, 57.0, 0), "NEG"),                # not brand
    ]
    out.append(NegScenario("N2 brand, misspellings, competitors",
        {"st30.csv": export("Search terms report", D30, ST_HEAD, [r for r, _ in rows]).encode()},
        ["st30.csv"] + TH + ["--competitor-token", "rivalco"],
        {(r[3], r[4], _norm(r[0])): l for r, l in rows},
        "misspelled brand, look-alike non-brand, competitor research vs navigational"))

    # N3: ambiguous intent, term-scoped safety, 90-day clearance
    rows30 = [
        (st_row("free invoice template", G, "Templates", 40, 120.0, 0), "KEEP"),     # converts in other campaign
        (st_row("free invoice template", "Search - Generic - Templates", "Templates", 60, 150.0, 3), "KEEP"),
        (st_row("invoice software for contractors", G, "Software", 26, 130.0, 0), "KEEP"),  # converted in 90d
        (st_row("invoice template excel", G, "Templates", 33, 99.0, 0), "NEG"),
        (st_row("what is an invoice", G, "Informational", 45, 112.5, 0), "NEG"),
        (st_row("billing software", G, "Software", 28, 140.0, 0), "KEEP"),           # converted in 90d (different case)
        (st_row("quickbooks alternative", G, "Software", 18, 1500.0, 2), "NEG"),     # CPA 750 > 360, no 90d help
    ]
    rows90 = [r for r, _ in rows30] + [
        st_row("invoice software for contractors", G, "Software", 70, 300.0, 2),
        st_row("Billing Software", "Search - Generic - Billing", "Billing", 50, 200.0, 1),
        st_row("quickbooks alternative", G, "Software", 40, 3000.0, 3),
    ]
    out.append(NegScenario("N3 ambiguous intent + 90-day clearance",
        {"st30.csv": export("Search terms report", D30, ST_HEAD, [r for r, _ in rows30]).encode(),
         "st90.csv": export("Search terms report", D90, ST_HEAD, rows90).encode()},
        ["st30.csv"] + TH + ["--clearance-export", "st90.csv"],
        {(r[3], r[4], _norm(r[0])): l for r, l in rows30},
        "same term converting elsewhere; terms that converted only in days 31-90"))

    # N4: Excel UTF-16 TSV, non-English, case/space duplicates
    rows = [
        (st_row("factura gratis", G, "Templates", 30, 75.0, 0), "NEG"),
        (st_row("modèle de facture", G, "Templates", 24, 66.0, 0), "NEG"),
        (st_row("Invoice  Generator Free", G, "Generators", 20, 60.0, 0), "NEG"),
        (st_row("invoice generator free", G, "Generators", 15, 45.0, 0), "NEG"),   # same key as above
        (st_row("rechnung vorlage", G, "Templates", 9, 80.0, 0), "KEEP"),
    ]
    out.append(NegScenario("N4 UTF-16 Excel export, non-English, duplicates",
        {"st30.csv": export("Search terms report", D30, ST_HEAD, [r for r, _ in rows], sep="\t").encode("utf-16")},
        ["st30.csv"] + TH,
        {(r[3], r[4], _norm(r[0])): l for r, l in rows},
        "UTF-16 tab file, accents, whitespace/case duplicates"))

    # N5: large account, 3,000 rows, seeded ground truth
    rnd = random.Random(7)
    heads = ["invoice", "billing", "estimate", "receipt", "quote", "payment"]
    tails = ["software", "app", "template", "tool", "online", "for freelancers", "generator", "system"]
    junk = ["jobs", "salary", "course", "definition", "meaning", "pdf free download", "clerk"]
    camps = [f"Search - Generic - {h.title()}" for h in heads]
    rows, labels = [], {}
    for i in range(3000):
        camp = rnd.choice(camps)
        kind = rnd.random()
        if kind < 0.08:
            term = f"acme {rnd.choice(tails)} {i}"
            clicks, cost, conv, lab = rnd.randint(10, 80), rnd.uniform(60, 300), 0, "KEEP"
        elif kind < 0.30:
            term = f"{rnd.choice(heads)} {rnd.choice(junk)} {i}"
            clicks = rnd.randint(1, 40); cost = rnd.uniform(5, 200); conv = 0
            lab = "NEG" if (cost >= 50 and clicks >= 10) else "KEEP"
        elif kind < 0.40:
            term = f"{rnd.choice(heads)} {rnd.choice(tails)} expensive {i}"
            conv = rnd.randint(1, 3); cost = conv * rnd.uniform(400, 900); clicks = rnd.randint(20, 90)
            lab = "NEG"
        else:
            term = f"{rnd.choice(heads)} {rnd.choice(tails)} {i}"
            conv = rnd.choice([0, 0, 1, 2, 4]); clicks = rnd.randint(1, 60)
            cost = rnd.uniform(5, 45) if conv == 0 else conv * rnd.uniform(40, 200)
            lab = "KEEP"
        cost = round(cost, 2)
        rows.append(st_row(term, camp, "Core", clicks, cost, conv))
        labels[(camp, "Core", _norm(term))] = lab
    out.append(NegScenario("N5 large account (3,000 rows)",
        {"st30.csv": export("Search terms report", D30, ST_HEAD, rows).encode()},
        ["st30.csv"] + TH, labels, "scale and timing"))
    return out


CAMP_HEAD = ["Campaign", "Campaign ID", "Campaign type", "Campaign status", "Budget", "Currency code",
             "Clicks", "Impr.", "Cost", "Conversions", "Conv. value", "Search lost IS (budget)"]
ADS_HEAD = ["Campaign", "Ad group", "Ad ID", "Ad type", "Ad status", "Ad strength", "Policy details"] + \
           [f"Headline {i}" for i in range(1, 16)]
ASSET_HEAD = ["Asset type", "Asset", "Level", "Campaign", "Status"]


def camp_row(name, cid, ctype, status, cost, conv, clicks=None, lost="--", budget=100):
    clicks = clicks if clicks is not None else max(int(cost / 2), 0)
    return [name, cid, ctype, status, money(budget), "USD", f"{clicks:,}", f"{clicks * 15:,}",
            money(cost), f"{conv:.2f}", money(conv * 300), lost]


def ad_row(camp, group, adid, status, strength, nheads, policy="Approved", insertion=False):
    heads = [f"Headline text {i}" for i in range(1, nheads + 1)]
    if insertion:
        heads[0] = "{KeyWord:Invoicing Software}"
    heads += ["--"] * (15 - nheads)
    return [camp, group, adid, "Responsive search ad", status, strength, policy] + heads


def audit_scenarios():
    out = []
    AUD = ["--target-cpa", "120", "--brand-token", "acme"]
    # A1: small account, everything typical
    camps = [
        camp_row("Acme - Search - Brand", "101", "Search", "Enabled", 900.0, 90),
        camp_row("Acme - Search - Generic - Invoicing", "102", "Search", "Enabled", 4200.0, 12, lost="35.50%"),
        camp_row("Acme - Search - Generic - Billing", "103", "Search", "Enabled", 1500.0, 0),
        camp_row("Acme - Search - Conquest - RivalCo", "104", "Search", "Enabled", 800.0, 2),
        camp_row("Acme - Video - Awareness", "105", "Video", "Enabled", 2000.0, 0),
        camp_row("Acme - Search - Old Promo", "106", "Search", "Paused", 0.0, 0),
    ]
    ads = [
        ad_row("Acme - Search - Brand", "Brand", "9001", "Enabled", "Excellent", 15),
        ad_row("Acme - Search - Brand", "Brand", "9002", "Enabled", "Good", 12),
        ad_row("Acme - Search - Generic - Invoicing", "Invoicing", "9003", "Enabled", "Poor", 5),
        ad_row("Acme - Search - Generic - Billing", "Billing", "9004", "Enabled", "Average", 10),
        ad_row("Acme - Search - Generic - Billing", "Billing", "9005", "Enabled", "Good", 11),
        ad_row("Acme - Search - Conquest - RivalCo", "RivalCo", "9006", "Enabled", "Good", 10, insertion=True),
        ad_row("Acme - Search - Conquest - RivalCo", "RivalCo", "9007", "Enabled", "Good", 10),
        ad_row("Acme - Search - Generic - Invoicing", "Invoicing", "9008", "Paused", "Poor", 4, policy="Disapproved"),
        ad_row("Acme - Search - Generic - Invoicing", "Invoicing", "9009", "Enabled", "Good", 12),
    ]
    assets = [
        ["Sitelink", "Pricing", "Account", "--", "Enabled"],
        ["Callout", "Free trial", "Campaign", "Acme - Search - Brand", "Enabled"],
        ["Callout", "No setup fee", "Campaign", "Acme - Search - Generic - Invoicing", "Removed"],
        ["Structured snippet", "Types: Invoices", "Account", "--", "Enabled"],
    ]
    S = "Acme - Search - "
    expect = {
        ("HIGH_CPA", S + "Generic - Invoicing"),          # 350 CPA > 240 (2x target)
        ("ZERO_CONV_SPEND", S + "Generic - Billing"),
        ("FEW_HEADLINES", S + "Generic - Invoicing > Invoicing (ad 9003)"),
        ("POOR_AD_STRENGTH", S + "Generic - Invoicing > Invoicing (ad 9003)"),
        ("KEYWORD_INSERTION_CONQUEST", S + "Conquest - RivalCo > RivalCo (ad 9006)"),
        ("MISSING_CALLOUTS", S + "Generic - Invoicing"),
        ("MISSING_CALLOUTS", S + "Generic - Billing"),
        ("MISSING_CALLOUTS", S + "Conquest - RivalCo"),
    }
    either = {("LOST_IS_BUDGET", S + "Generic - Invoicing")}
    out.append(FindScenario("A1 small account",
        {"campaigns.csv": export("Campaign report", D30, CAMP_HEAD, camps).encode(),
         "ads.csv": export("Ad report", D30, ADS_HEAD, ads).encode(),
         "assets.csv": export("Asset report", D30, ASSET_HEAD, assets, total=False).encode()},
        ["--campaigns", "campaigns.csv", "--ads", "ads.csv", "--assets", "assets.csv"] + AUD,
        expect, either, "company name in every campaign name; paused disapproved ad; removed callout"))

    # A2: messy, Excel UTF-16, PMax, brand without the word brand, policy-limited ads
    camps = [
        camp_row("Search | Core Brand Terms", "201", "Search", "Enabled", 600.0, 1),   # brand: low conv is fine
        camp_row("Acme Branded, Exact", "202", "Search", "Enabled", 500.0, 0),          # brand by config? no: by token in name -> expert: brand
        camp_row("PMax - All Products", "203", "Performance Max", "Enabled", 3000.0, 0),
        camp_row("Demand Gen - Prospecting", "204", "Demand Gen", "Enabled", 1200.0, 1),
        camp_row("Search - NonBrand - Estimates", "205", "Search", "Enabled (limited by budget)", 2400.0, 20, lost="45.20%"),
        camp_row("Search - NonBrand - Receipts", "206", "Search", "Enabled", 300.0, 3),
    ]
    ads = [
        ad_row("Search - NonBrand - Estimates", "Estimates", "9101", "Eligible (limited)", "Good", 12,
               policy="Approved (limited): Trademarks in ad text"),
        ad_row("Search - NonBrand - Estimates", "Estimates", "9102", "Enabled", "Good", 12),
        ad_row("Search - NonBrand - Receipts", "Receipts", "9103", "Enabled", "Average", 9),
    ]
    assets = [
        ["Sitelink", "Pricing", "Campaign", "Search - NonBrand - Estimates", "Enabled"],
        ["Callout", "Free trial", "Account", "", "Enabled"],
    ]
    expect = {
        ("ZERO_CONV_SPEND", "PMax - All Products"),
        ("LOST_IS_BUDGET", "Search - NonBrand - Estimates"),
        ("AD_POLICY", "Search - NonBrand - Estimates > Estimates (ad 9101)"),
        ("SINGLE_RSA", "Search - NonBrand - Receipts > Receipts"),
        ("MISSING_SITELINKS", "Search - NonBrand - Receipts"),
        # snippets are missing in every search campaign: one account-level fix, not four
        ("MISSING_SNIPPETS", "Account (all 4 search campaigns)"),
    }
    either = {("MISSING_SITELINKS", "Search | Core Brand Terms"), ("MISSING_SNIPPETS", "Search | Core Brand Terms"),
              ("MISSING_SITELINKS", "Acme Branded, Exact"), ("MISSING_SNIPPETS", "Acme Branded, Exact")}
    out.append(FindScenario("A2 messy UTF-16 export, PMax, policy-limited",
        {"campaigns.csv": export("Campaign report", D30, CAMP_HEAD, camps, sep="\t").encode("utf-16"),
         "ads.csv": export("Ad report", D30, ADS_HEAD, ads).encode(),
         "assets.csv": export("Asset report", D30, ASSET_HEAD, assets, total=False).encode()},
        ["--campaigns", "campaigns.csv", "--ads", "ads.csv", "--assets", "assets.csv"] + AUD,
        expect, either, "brand campaigns without the word brand in a fixed position; PMax; limited ads"))

    # A3: large account, 60 campaigns, generated truth
    rnd = random.Random(11)
    camps, expect = [], set()
    for i in range(60):
        name = f"Acme - Search - Generic - Theme {i:02d}"
        conv = rnd.choice([0, 0, 2, 6, 10, 25, 40])
        cost = rnd.uniform(100, 6000)
        cost = round(cost, 2)
        if conv == 0 and cost >= 120:
            expect.add(("ZERO_CONV_SPEND", name))
        elif conv >= 5 and cost / conv > 240:
            expect.add(("HIGH_CPA", name))
        elif 0 < conv < 5 and cost >= 480 and cost / conv > 240:
            expect.add(("HIGH_COST_LOW_CONV", name))   # too few conversions to tune, too much spend to wait
        camps.append(camp_row(name, str(300 + i), "Search", "Enabled", cost, conv))
    out.append(FindScenario("A3 large account (60 campaigns)",
        {"campaigns.csv": export("Campaign report", D30, CAMP_HEAD, camps).encode()},
        ["--campaigns", "campaigns.csv"] + AUD, expect, set(), "scale"))
    return out


def weekly_scenarios():
    out = []
    LW = '"September 28, 2026 - October 4, 2026"'
    MTD = '"October 1, 2026 - October 11, 2026"'
    # W1: typical week
    lw = [camp_row("Search - Brand", "1", "Search", "Enabled", 700, 60, clicks=900),
          camp_row("Search - Generic - Invoicing", "2", "Search", "Enabled", 2000, 20, clicks=1000),
          camp_row("Search - Generic - Billing", "3", "Search", "Enabled", 1000, 12, clicks=500),
          camp_row("Video - Awareness", "4", "Video", "Enabled", 800, 4, clicks=300),
          camp_row("Search - Generic - Receipts", "5", "Search", "Enabled", 300, 2, clicks=150),
          camp_row("Search - Old Test", "6", "Search", "Enabled", 400, 3, clicks=200)]
    tw = [camp_row("Search - Brand", "1", "Search", "Enabled", 720, 58, clicks=910),
          camp_row("Search - Generic - Invoicing", "2", "Search", "Enabled", 2800, 21, clicks=1400),
          camp_row("Search - Generic - Billing", "3", "Search", "Enabled", 1010, 6, clicks=505),
          camp_row("Video - Awareness", "4", "Video", "Enabled", 810, 0, clicks=310),
          camp_row("Search - Generic - Receipts", "5", "Search", "Enabled", 310, 0, clicks=150),
          camp_row("Search - Generic - Estimates", "7", "Search", "Enabled", 500, 4, clicks=250)]
    mtd = [camp_row("All campaigns", "0", "Search", "Enabled", 24000, 120)]
    expect = {("SPEND_UP", "Search - Generic - Invoicing"), ("CONV_DOWN", "Search - Generic - Billing"),
              ("NEW_CAMPAIGN", "Search - Generic - Estimates"), ("GONE_CAMPAIGN", "Search - Old Test"),
              ("PACING_OVER", "Monthly budget")}
    out.append(FindScenario("W1 typical week",
        {"lw.csv": export("Campaign report", LW, CAMP_HEAD, lw).encode(),
         "tw.csv": export("Campaign report", D7, CAMP_HEAD, tw).encode(),
         "mtd.csv": export("Campaign report", MTD, CAMP_HEAD, mtd).encode()},
        ["--this-week", "tw.csv", "--last-week", "lw.csv", "--mtd", "mtd.csv", "--month-budget", "54400",
         "--as-of", "2026-10-11"],
        expect, set(), "video conversions drop (not judged); 2 to 0 conversions (noise)"))

    # W2: tracking break
    lw2 = [camp_row(f"Search - Generic - T{i}", str(i), "Search", "Enabled", 1000, 15, clicks=500) for i in range(5)]
    tw2 = [camp_row(f"Search - Generic - T{i}", str(i), "Search", "Enabled", 1010, 3, clicks=505) for i in range(5)]
    either = {("CONV_DOWN", f"Search - Generic - T{i}") for i in range(5)} | \
             {("LOW_DATA", f"Search - Generic - T{i}") for i in range(5)}
    out.append(FindScenario("W2 tracking break",
        {"lw.csv": export("Campaign report", LW, CAMP_HEAD, lw2).encode(),
         "tw.csv": export("Campaign report", D7, CAMP_HEAD, tw2).encode()},
        ["--this-week", "tw.csv", "--last-week", "lw.csv"],
        {("TRACKING_CHECK", "Whole account")}, either, "conversions crash while clicks hold"))

    # W3: planned budget step down
    lw3 = [camp_row(f"Search - Generic - P{i}", str(i), "Search", "Enabled", 1000, 20, clicks=500) for i in range(4)]
    tw3 = [camp_row(f"Search - Generic - P{i}", str(i), "Search", "Enabled", 800, 17, clicks=410) for i in range(4)]
    out.append(FindScenario("W3 planned 20% step down",
        {"lw.csv": export("Campaign report", LW, CAMP_HEAD, lw3).encode(),
         "tw.csv": export("Campaign report", D7, CAMP_HEAD, tw3).encode()},
        ["--this-week", "tw.csv", "--last-week", "lw.csv", "--planned-change-pct", "-20"],
        set(), {("CONV_DOWN", f"Search - Generic - P{i}") for i in range(4)},
        "spend falls exactly as planned: not a finding"))

    # W4: messy: device-segmented duplicate rows, renamed campaign, '--'
    lw4 = [camp_row("Search - Generic - Invoicing", "2", "Search", "Enabled", 600, 10, clicks=300),
           camp_row("Search - Generic - Invoicing", "2", "Search", "Enabled", 400, 8, clicks=200),
           camp_row("Search - Billing (old name)", "3", "Search", "Enabled", 500, 9, clicks=250)]
    tw4 = [camp_row("Search - Generic - Invoicing", "2", "Search", "Enabled", 520, 9, clicks=260),
           camp_row("Search - Generic - Invoicing", "2", "Search", "Enabled", 480, 9, clicks=240),
           camp_row("Search - Generic - Billing", "3", "Search", "Enabled", 505, 9, clicks=252)]
    tw4[2][-1] = "--"
    out.append(FindScenario("W4 segmented rows and a renamed campaign",
        {"lw.csv": export("Campaign report", LW, CAMP_HEAD, lw4).encode(),
         "tw.csv": export("Campaign report", D7, CAMP_HEAD, tw4, sep="\t").encode("utf-16")},
        ["--this-week", "tw.csv", "--last-week", "lw.csv"], set(), set(),
        "nothing changed: duplicates must be summed and the rename must not look new"))

    # W5: large account
    rnd = random.Random(5)
    lw5, tw5, expect5, either5 = [], [], set(), set()
    for i in range(80):
        name = f"Search - Generic - Theme {i:02d}"
        c0 = round(rnd.uniform(200, 3000), 2); v0 = rnd.choice([1, 3, 8, 15, 30])
        move = rnd.choice([1.0, 1.0, 1.0, 1.05, 0.95, 1.4, 0.6])
        vmove = rnd.choice([1.0, 1.0, 1.0, 0.5, 1.6])
        c1 = round(c0 * move, 2); v1 = round(v0 * vmove)
        lw5.append(camp_row(name, str(i), "Search", "Enabled", c0, v0, clicks=500))
        tw5.append(camp_row(name, str(i), "Search", "Enabled", c1, v1, clicks=500))
        if abs(c1 - c0) / c0 >= 0.15 and abs(c1 - c0) >= 50:
            expect5.add(("SPEND_UP" if c1 > c0 else "SPEND_DOWN", name))
        if v0 and abs(v1 - v0) / v0 >= 0.15:
            if v0 >= 5 and v1 >= 5:
                expect5.add(("CONV_UP" if v1 > v0 else "CONV_DOWN", name))
    out.append(FindScenario("W5 large account (80 campaigns)",
        {"lw.csv": export("Campaign report", LW, CAMP_HEAD, lw5).encode(),
         "tw.csv": export("Campaign report", D7, CAMP_HEAD, tw5).encode()},
        ["--this-week", "tw.csv", "--last-week", "lw.csv"], expect5, either5, "scale"))
    return out


def bad_scenarios():
    G = "Search - Generic"
    st = export("Search terms report", D30, ST_HEAD, [st_row("a", G, "x", 20, 60.0, 0)]).encode()
    st7 = export("Search terms report", D7, ST_HEAD, [st_row("a", G, "x", 20, 60.0, 0)]).encode()
    camp = export("Campaign report", D30, CAMP_HEAD, [camp_row("C", "1", "Search", "Enabled", 10, 0)]).encode()
    camp_lw = export("Campaign report", '"September 28, 2026 - October 4, 2026"', CAMP_HEAD,
                     [camp_row("C", "1", "Search", "Enabled", 10, 0)]).encode()
    TH = ["--min-spend", "50", "--min-clicks", "10", "--target-cpa", "120", "--target-cpa-multiple", "3"]
    return [
        BadScenario("negatives: campaigns file instead of search terms", "negatives", {"f.csv": camp}, ["f.csv"] + TH),
        BadScenario("negatives: empty file", "negatives", {"f.csv": b""}, ["f.csv"] + TH),
        BadScenario("negatives: binary junk (xlsx)", "negatives", {"f.csv": b"PK\x03\x04\x14\x00\x06\x00" + bytes(range(256))}, ["f.csv"] + TH),
        BadScenario("negatives: header only", "negatives", {"f.csv": b"Search terms report\nx\nSearch term,Campaign,Ad group,Clicks,Cost,Conversions\n"}, ["f.csv"] + TH),
        BadScenario("negatives: missing thresholds", "negatives", {"f.csv": st}, ["f.csv", "--min-spend", "50"]),
        BadScenario("negatives: clearance shorter than main", "negatives", {"f.csv": st, "c.csv": st7}, ["f.csv"] + TH + ["--clearance-export", "c.csv"]),
        BadScenario("negatives: file does not exist", "negatives", {}, ["nope.csv"] + TH),
        BadScenario("negatives: bad number in cost", "negatives", {"f.csv": st.replace(b"60.00", b"sixty")}, ["f.csv"] + TH),
        BadScenario("audit: search terms file as campaigns", "audit", {"f.csv": st}, ["--campaigns", "f.csv", "--target-cpa", "120"]),
        BadScenario("audit: no target CPA", "audit", {"f.csv": camp}, ["--campaigns", "f.csv"]),
        BadScenario("weekly: same file for both weeks", "weekly", {"a.csv": camp}, ["--this-week", "a.csv", "--last-week", "a.csv"]),
        BadScenario("weekly: 30 days vs 7 days", "weekly", {"a.csv": camp, "b.csv": camp_lw}, ["--this-week", "a.csv", "--last-week", "b.csv"]),
        BadScenario("weekly: pacing without budget", "weekly", {"a.csv": camp_lw, "b.csv": camp_lw.replace(b"September 28", b"September 21").replace(b"October 4", b"September 27"), "m.csv": camp},
                    ["--this-week", "a.csv", "--last-week", "b.csv", "--mtd", "m.csv", "--as-of", "2026-10-11"]),
        BadScenario("weekly: bad as-of date", "weekly", {"a.csv": camp_lw, "b.csv": camp_lw.replace(b"September 28", b"September 21").replace(b"October 4", b"September 27"), "m.csv": camp},
                    ["--this-week", "a.csv", "--last-week", "b.csv", "--mtd", "m.csv", "--month-budget", "100", "--as-of", "Oct 11"]),
    ]


def holdout_scenarios():
    """Held out: written after the fixes and never tuned against. Scored and
    reported, not gating, so it shows where the bot still falls short."""
    G, B = "Search - Generic - Invoicing", "Search - Brand - Core"
    TH = ["--min-spend", "50", "--min-clicks", "10", "--target-cpa", "120", "--target-cpa-multiple", "3",
          "--brand-token", "acme", "--competitor-token", "rivalco"]
    rows = [
        (st_row("acmesoftwear", G, "Software", 14, 70.0, 0), "KEEP"),          # compound misspelling
        (st_row("akmi invoice", G, "Software", 12, 60.0, 0), "KEEP"),          # harder misspelling
        (st_row("rival co login", G, "Software", 15, 75.0, 0), "NEG"),         # split competitor name
        (st_row("invoice software", B, "Brand", 12, 60.0, 0), "NEG"),          # generic leaking into brand
        (st_row("invoice", G, "Software", 90, 400.0, 0), "KEEP"),              # core head term: fix keywords, do not negate
        (st_row("invoicing", G, "Software", 70, 310.0, 0), "KEEP"),            # core head term
        (st_row("best free invoicing software", G, "Software", 20, 80.0, 0), "EITHER"),
        (st_row("invoice template google docs", G, "Templates", 25, 75.0, 0), "NEG"),
    ]
    return [NegScenario("H1 hold-out: hard search terms",
        {"st30.csv": export("Search terms report", D30, ST_HEAD, [r for r, _ in rows]).encode()},
        ["st30.csv"] + TH, {(r[3], r[4], _norm(r[0])): l for r, l in rows}, "not tuned against")]
