#!/usr/bin/env python3
"""Generate the committed synthetic raw-pull fixture.

Run:  python3 tools/make_fixture.py            # writes tools/fixtures/synthetic/
      python3 tools/make_fixture.py --check    # fails if the fixture is stale

Why this exists
---------------
Runtime pulls are gitignored, because they hold live account data. The
consequence, measured 2026-08-20: on a fresh clone **265 of 505 tests skip**,
and `test_budget_optimize_facts.py` executes **0 of its 71**. Four of the five
suites pin to one date, 2026-08-20, and the older raw directories predate the
manifest convention so no builder has ever run against a second dataset.

This fixture is that second dataset. It is **entirely synthetic** - invented
campaign ids, invented amounts, an August 2026 date with invented ids that could not be mistaken for a
real pull - so committing it puts no account data in git. Its job is to prove
the builders and their guards work on data they have never seen, which is a
different question from whether one day's numbers are right.

The manifest's filter strings are generated **from the builders' own scope
constants**, so a builder that changes its expected filter cannot silently
drift away from the fixture: regenerate, and the diff shows up in review.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import account_config  # noqa: E402

FIXTURE_DATE = "2026-08-08"
#: The directory name must be a valid YYYY-MM-DD: the builders read the pull
#: date from it to write current-state windows (VF-17 rule 4). August 2026 is
#: the month with a stated Google Ads budget in `account-config.json`, so an
#: August fixture exercises the stated-budget pacing path; the unstated-month
#: (null-with-reason) path is exercised by tests that restage this fixture
#: into September.
#:
#: **The date is real; none of the data is.** Campaign ids are 7000000xxxx,
#: budget ids 8000000x and the customer is 9999999999 - none of which exist -
#: and the manifest carries `SYNTHETIC: true`. It also lives in `tools/fixtures/`,
#: not in the gitignored `google-ads/outputs/raw/`, so it can never be mistaken
#: for a real pull.
FIXTURE_ROOT = HERE / "fixtures" / FIXTURE_DATE
CUSTOMER = "9999999999"

#: Invented ids. Deliberately outside this account's 11-digit 2x range so a
#: grep for a real campaign id can never hit the fixture.
BRAND = "70000000001"
NONBRAND = "70000000002"
PMAX = "70000000003"
TOF = "70000000004"
DORMANT = "70000000005"
SHARED_A = "70000000006"
SHARED_B = "70000000007"

BUDGET_BRAND = "80000001"
BUDGET_NONBRAND = "80000002"
BUDGET_PMAX = "80000003"
BUDGET_TOF = "80000004"
BUDGET_DORMANT = "80000005"
BUDGET_SHARED = "80000006"  # carries SHARED_A and SHARED_B

AG_NONBRAND = "90000001"
AG_BRAND = "90000002"

#: audit-ad-copy fixture objects. Thirteen servable RSAs so the serve-table
#: selection rule (top 10 by cost + any above CAD 2,000) actually excludes
#: something: ads 1-10 make the top ten, ad 11 clears the cost floor, ads 12
#: and 13 are excluded - which is what exercises the headline-facts diet.
AG_RSA = "90000003"
AG_RSA2 = "90000004"
ADCOPY_AD_IDS = [f"600000000{n:02d}" for n in range(1, 14)]
ADCOPY_COSTS = [3000, 2900, 2800, 2700, 2600, 2500, 2400, 2300, 2200, 2100,
                2050, 150, 90]

#: The 8 example conversion actions, from `account-config.json`, so conversion-basis
#: code paths run against the same names the builders filter on. One source,
#: zero copies.
EIGHT = account_config.eight_actions()
DEFAULTS = {
    "Basic Trial Signup": 350,
    "Pro Trial Signup": 700,
    "Plus Trial Signup": 1000,
    "Business Trial Signup": 1700,
    "Basic Annual Signup": 500,
    "Pro Annual Signup": 800,
    "Plus Annual Signup": 1200,
    "Business Annual Signup": 1900,
}


def payload(field_mask: str, results: list) -> dict:
    return {
        "action": "post",
        "data": {"fieldMask": field_mask, "results": results},
        "path": f"/v22/customers/{CUSTOMER}/googleAds:search",
        "status": 200,
        "success": True,
    }


def micros(cad: float) -> str:
    return str(int(round(cad * 1_000_000)))


def campaign(cid, name, channel, strategy, budget_id, amount, troas=None, tcpa=None):
    row = {
        "campaign": {
            "id": cid,
            "name": name,
            "status": "ENABLED",
            "advertisingChannelType": channel,
            "biddingStrategyType": strategy,
            "resourceName": f"customers/{CUSTOMER}/campaigns/{cid}",
        },
        "campaignBudget": {
            "id": budget_id,
            "amountMicros": micros(amount),
            "explicitlyShared": False,
            "resourceName": f"customers/{CUSTOMER}/campaignBudgets/{budget_id}",
        },
    }
    if troas is not None:
        row["campaign"]["maximizeConversionValue"] = {"targetRoas": troas}
    if tcpa is not None:
        row["campaign"]["maximizeConversions"] = {"targetCpaMicros": micros(tcpa)}
    return row


def campaigns_current() -> dict:
    return payload(
        "campaign.id,campaign.name,campaign.status,campaign.advertisingChannelType,"
        "campaign.biddingStrategyType,campaign.maximizeConversionValue.targetRoas,"
        "campaign.maximizeConversions.targetCpaMicros,campaign.targetRoas.targetRoas,"
        "campaign.targetCpa.targetCpaMicros,campaignBudget.id,"
        "campaignBudget.amountMicros,campaignBudget.explicitlyShared",
        [
            campaign(BRAND, "Synthetic - Search - Brand - Core", "SEARCH",
                     "MAXIMIZE_CONVERSION_VALUE", BUDGET_BRAND, 100, troas=20),
            campaign(NONBRAND, "Synthetic - Search - Generic - Features", "SEARCH",
                     "MAXIMIZE_CONVERSION_VALUE", BUDGET_NONBRAND, 400, troas=1.2),
            campaign(PMAX, "Synthetic - Performance Max", "PERFORMANCE_MAX",
                     "MAXIMIZE_CONVERSION_VALUE", BUDGET_PMAX, 500, troas=1.5),
            campaign(TOF, "Synthetic - Demand Gen", "DEMAND_GEN",
                     "MAXIMIZE_CONVERSIONS", BUDGET_TOF, 50, tcpa=400),
            campaign(DORMANT, "Synthetic - Search - Dormant Shell", "SEARCH",
                     "TARGET_SPEND", BUDGET_DORMANT, 75),
            # One genuinely shared pool: two campaigns, one budget id. The
            # fan-out guard (VF-19) has nothing to catch without this.
            campaign(SHARED_A, "Synthetic - Search - Shared A", "SEARCH",
                     "MAXIMIZE_CONVERSION_VALUE", BUDGET_SHARED, 200, troas=1.1),
            campaign(SHARED_B, "Synthetic - Search - Shared B", "SEARCH",
                     "MAXIMIZE_CONVERSION_VALUE", BUDGET_SHARED, 200, troas=1.1),
        ],
    )


def cost_is(spend: dict, *, sentinel_on: str | None = None) -> dict:
    """Cost + impression share. `sentinel_on` plants the 0.0999 '<10%' bound."""
    results = []
    for cid, cad in spend.items():
        metrics = {
            "costMicros": micros(cad),
            "impressions": str(int(cad * 20)),
            "clicks": str(int(cad / 4)),
            "searchImpressionShare": 0.0999 if cid == sentinel_on else 0.42,
            "searchBudgetLostImpressionShare": 0.55 if cid == PMAX else 0.05,
            "searchRankLostImpressionShare": 0.30,
            "searchAbsoluteTopImpressionShare": 0.41,
            "searchTopImpressionShare": 0.60,
        }
        results.append({"campaign": {"id": cid}, "metrics": metrics})
    return payload(
        "campaign.id,metrics.costMicros,metrics.impressions,metrics.clicks,"
        "metrics.searchImpressionShare,metrics.searchBudgetLostImpressionShare,"
        "metrics.searchRankLostImpressionShare,"
        "metrics.searchAbsoluteTopImpressionShare,metrics.searchTopImpressionShare",
        results,
    )


def conversions(rows: list) -> dict:
    """`rows` = [(campaign_id, action_name, count)]. Value follows the default."""
    return payload(
        "campaign.id,segments.conversionActionName,metrics.conversions,"
        "metrics.conversionsValue",
        [
            {
                "campaign": {"id": cid},
                "segments": {"conversionActionName": action},
                "metrics": {
                    "conversions": count,
                    "conversionsValue": count * DEFAULTS[action],
                },
            }
            for cid, action, count in rows
        ],
    )


def spend_mtd() -> dict:
    return payload(
        "segments.date,metrics.costMicros",
        [
            {"segments": {"date": f"2026-08-{day:02d}"}, "metrics": {"costMicros": micros(1000 + day * 10)}}
            for day in range(1, 8)
        ],
    )


def change_events() -> dict:
    def ev(cid, kind, fields, when, user="synthetic.operator@example.invalid"):
        return {
            "changeEvent": {
                "resourceName": f"customers/{CUSTOMER}/changeEvents/{cid}~{when}",
                "changeDateTime": f"{when} 12:00:00.000000",
                "changeResourceType": kind,
                "resourceChangeOperation": "UPDATE",
                "changedFields": fields,
                "campaign": f"customers/{CUSTOMER}/campaigns/{cid}",
                "userEmail": user,
                "clientType": "GOOGLE_ADS_WEB_CLIENT",
            }
        }
    return payload(
        "changeEvent.changeDateTime,changeEvent.changeResourceType,"
        "changeEvent.resourceChangeOperation,changeEvent.changedFields,"
        "changeEvent.campaign,changeEvent.adGroup,changeEvent.userEmail,"
        "changeEvent.clientType",
        [
            # PMax budget change INSIDE its 28-day window. This is the row that
            # catches the line-898 ordering bug: cleared must read False, not null.
            ev(PMAX, "CAMPAIGN_BUDGET", "amountMicros", "2026-08-06"),
            ev(NONBRAND, "CAMPAIGN", "maximizeConversionValue.targetRoas", "2026-08-05"),
            ev(BRAND, "CAMPAIGN_BUDGET", "amountMicros", "2026-08-02"),
            # A second operator, so a single-author claim cannot pass.
            ev(NONBRAND, "AD_GROUP_CRITERION", "keyword.text", "2026-08-04",
               user="synthetic.second@example.invalid"),
        ],
    )


def search_terms_cost() -> dict:
    """One row per decision branch, plus the structural hazards.

    Deliberately includes, because a fixture that only holds happy rows tests
    nothing the real pull would: a **duplicate identity** (one term, two
    triggering keywords, so the CSV dedup and cost-summing paths run), a
    **criterion id reused across two ad groups** (VF-19: keying on the criterion
    alone misattributes cost), and a **clicks-exceeding-impressions** row (the
    real pull has 16 of them, so the defect must be reported not smoothed).
    """
    # term, ad_group, campaign, cost, clicks, status, keyword, criterion, impressions
    rows = [
        ("negate-me", AG_NONBRAND, NONBRAND, 400, 20, "NONE", "kw alpha", "50000001", None),
        ("kept-by-90d", AG_NONBRAND, NONBRAND, 500, 25, "NONE", "kw alpha", "50000001", None),
        ("held-thin-clicks", AG_NONBRAND, NONBRAND, 300, 2, "NONE", "kw beta", "50000002", None),
        ("below-floor", AG_NONBRAND, NONBRAND, 10, 1, "NONE", "kw beta", "50000002", None),
        ("added-keyword", AG_NONBRAND, NONBRAND, 900, 40, "ADDED", "added-keyword", "50000003", None),
        ("excluded-already", AG_NONBRAND, NONBRAND, 150, 8, "EXCLUDED", "kw beta", "50000002", None),
        ("converts-now", AG_BRAND, BRAND, 200, 15, "NONE", "kw brand", "50000004", None),
        ("over-cut-line", AG_NONBRAND, NONBRAND, 4000, 30, "NONE", "kw alpha", "50000001", None),
        # Duplicate identity: same (campaign, ad group, term), two keywords.
        ("dup-identity", AG_NONBRAND, NONBRAND, 260, 12, "NONE", "kw gamma", "50000005", None),
        ("dup-identity", AG_NONBRAND, NONBRAND, 140, 6, "NONE", "kw delta", "50000006", None),
        # Criterion 50000001 also appears under a SECOND ad group.
        ("reused-criterion", AG_BRAND, BRAND, 320, 14, "NONE", "kw alpha", "50000001", None),
        # Clicks exceed impressions: the raw-pull defect, reported not smoothed.
        ("impossible-row", AG_NONBRAND, NONBRAND, 120, 9, "NONE", "kw beta", "50000002", 3),
        # Filler so a top-ten table has ten ranks to fill.
        ("filler-one", AG_NONBRAND, NONBRAND, 700, 22, "NONE", "kw beta", "50000002", None),
        ("filler-two", AG_BRAND, BRAND, 640, 19, "NONE", "kw brand", "50000004", None),
        ("filler-three", AG_NONBRAND, NONBRAND, 580, 17, "NONE", "kw gamma", "50000005", None),
    ]
    return payload(
        "campaign.id,campaign.name,adGroup.id,adGroup.name,"
        "searchTermView.searchTerm,searchTermView.status,segments.keyword.info.text,"
        "segments.keyword.info.matchType,segments.keyword.adGroupCriterion,"
        "metrics.costMicros,metrics.clicks,metrics.impressions",
        [
            {
                "campaign": {"id": cid, "name": f"Synthetic campaign {cid}"},
                "adGroup": {"id": ag, "name": f"Synthetic ad group {ag}"},
                "searchTermView": {"searchTerm": term, "status": status},
                "segments": {
                    "keyword": {
                        "info": {"text": kw, "matchType": "BROAD"},
                        "adGroupCriterion": (
                            f"customers/{CUSTOMER}/adGroupCriteria/{ag}~{crit}"
                        ),
                    }
                },
                "metrics": {
                    "costMicros": micros(cad),
                    "clicks": str(clicks),
                    "impressions": str(imp if imp is not None else clicks * 30),
                },
            }
            for term, ag, cid, cad, clicks, status, kw, crit, imp in rows
        ],
    )


def search_terms_conversions() -> dict:
    return payload(
        "campaign.id,adGroup.id,searchTermView.searchTerm,segments.keyword.info.text,"
        "segments.conversionActionName,metrics.conversions,metrics.conversionsValue",
        [
            {
                "campaign": {"id": BRAND},
                "adGroup": {"id": AG_BRAND},
                "searchTermView": {"searchTerm": "converts-now"},
                "segments": {"keyword": {"info": {"text": "kw brand"}},
                             "conversionActionName": "Basic Trial Signup"},
                "metrics": {"conversions": 2, "conversionsValue": 700},
            },
            # One conversion at a CPA far above 5x the 400 target, so the
            # over-cut-line branch has a row.
            {
                "campaign": {"id": NONBRAND},
                "adGroup": {"id": AG_NONBRAND},
                "searchTermView": {"searchTerm": "over-cut-line"},
                "segments": {"keyword": {"info": {"text": "kw alpha"}},
                             "conversionActionName": "Basic Trial Signup"},
                "metrics": {"conversions": 1, "conversionsValue": 350},
            }
        ],
    )


def donotnegate_90d() -> dict:
    """The guard's whole point: a term that a 30-day window reads as zero."""
    return payload(
        "campaign.id,campaign.name,adGroup.id,adGroup.name,"
        "searchTermView.searchTerm,segments.conversionActionName,"
        "metrics.conversions,metrics.conversionsValue",
        [
            {
                "campaign": {"id": NONBRAND, "name": "Synthetic - Search - Generic - Features"},
                "adGroup": {"id": AG_NONBRAND, "name": f"Synthetic ad group {AG_NONBRAND}"},
                "searchTermView": {"searchTerm": "kept-by-90d"},
                "segments": {"conversionActionName": "Basic Trial Signup"},
                "metrics": {"conversions": 1, "conversionsValue": 350},
            }
        ],
    )


def campaign_assets() -> dict:
    rows = []
    for cid, ft, aid in (
        (BRAND, "SITELINK", "60000001"),
        (NONBRAND, "SITELINK", "60000001"),   # shared asset, two campaigns
        (NONBRAND, "CALLOUT", "60000002"),
        (NONBRAND, "PRICE", "60000003"),
        (PMAX, "SITELINK", "60000001"),
        # An attachment on a campaign that is NOT in campaigns-current.json.
        # This is the VF-5-class row: the status filter is on the attachment.
        ("70000000099", "SITELINK", "60000004"),
    ):
        rows.append({
            "campaign": {"id": cid},
            "campaignAsset": {"fieldType": ft, "status": "ENABLED"},
            "asset": {"id": aid},
        })
    return payload("campaign.id,campaignAsset.fieldType,campaignAsset.status,asset.id", rows)


def customer_assets() -> dict:
    return payload(
        "customerAsset.fieldType,customerAsset.status,asset.id",
        [
            {"customerAsset": {"fieldType": "CALLOUT", "status": "ENABLED"}, "asset": {"id": "60000010"}},
            {"customerAsset": {"fieldType": "STRUCTURED_SNIPPET", "status": "ENABLED"}, "asset": {"id": "60000011"}},
        ],
    )


def asset_field_type() -> dict:
    rows = []
    for cid, ft, imp, clk in (
        (NONBRAND, "SITELINK", 5000, 300),
        (NONBRAND, "CALLOUT", 4000, 250),
        (NONBRAND, "STRUCTURED_SNIPPET", 20, 1),
        (NONBRAND, "PRICE", 900, 40),
        (BRAND, "SITELINK", 3000, 400),
    ):
        rows.append({
            "campaign": {"id": cid},
            "assetFieldTypeView": {"fieldType": ft},
            "metrics": {"impressions": str(imp), "clicks": str(clk), "costMicros": micros(imp / 10)},
        })
    return payload(
        "campaign.id,assetFieldTypeView.fieldType,metrics.impressions,"
        "metrics.clicks,metrics.costMicros",
        rows,
    )


def _rsa_inventory_row(cid, cname, ag_id, ag_name, ad_id, texts,
                       topics=None):
    row = {
        "campaign": {
            "id": cid, "name": cname, "status": "ENABLED",
            "advertisingChannelType": "SEARCH",
            "resourceName": f"customers/{CUSTOMER}/campaigns/{cid}",
        },
        "adGroup": {
            "id": ag_id, "name": ag_name, "status": "ENABLED",
            "resourceName": f"customers/{CUSTOMER}/adGroups/{ag_id}",
        },
        "adGroupAd": {
            "status": "ENABLED",
            "adStrength": "GOOD",
            "resourceName": f"customers/{CUSTOMER}/adGroupAds/{ag_id}~{ad_id}",
            "ad": {
                "id": ad_id,
                "finalUrls": ["https://example.invalid/"],
                "responsiveSearchAd": {
                    "headlines": [{"text": t} for t in texts],
                    "descriptions": [
                        {"text": f"Synthetic description one for ad {ad_id}"},
                        {"text": f"Synthetic description two for ad {ad_id}"},
                    ],
                },
            },
        },
    }
    if topics:
        row["adGroupAd"]["policySummary"] = {
            "approvalStatus": "APPROVED_LIMITED",
            "policyTopicEntries": [{"topic": t} for t in topics],
        }
    return row


def _adcopy_ad_home(n):
    """Ads 1-10 sit in AG_RSA under NONBRAND; 11-13 in AG_RSA2 under BRAND."""
    if n <= 10:
        return NONBRAND, "Synthetic - Search - Generic - Features", AG_RSA, "Synthetic RSA Group"
    return BRAND, "Synthetic - Search - Brand - Core", AG_RSA2, "Synthetic RSA Group Two"


def _adcopy_headlines(n, ad_id):
    texts = [
        f"Synthetic headline alpha {ad_id}",
        f"Synthetic headline beta {ad_id}",
        f"Synthetic headline gamma {ad_id}",
    ]
    if n == 1:
        # A fourth live headline that never serves: the dead slot.
        texts.append(f"Synthetic dead slot {ad_id}")
    return texts


def adcopy_inventory() -> dict:
    rows = []
    for n, ad_id in enumerate(ADCOPY_AD_IDS, start=1):
        cid, cname, ag_id, ag_name = _adcopy_ad_home(n)
        topics = ["TRADEMARK_IN_AD_TEXT_SPECIFICS"] if n == 13 else None
        rows.append(_rsa_inventory_row(cid, cname, ag_id, ag_name, ad_id,
                                       _adcopy_headlines(n, ad_id), topics))
    return payload(
        "campaign.id,campaign.name,campaign.status,"
        "campaign.advertisingChannelType,adGroup.id,adGroup.name,"
        "adGroup.status,adGroupAd.status,adGroupAd.adStrength,"
        "adGroupAd.resourceName,adGroupAd.ad.id,adGroupAd.ad.finalUrls,"
        "adGroupAd.ad.responsiveSearchAd.headlines,"
        "adGroupAd.ad.responsiveSearchAd.descriptions,"
        "adGroupAd.policySummary.approvalStatus,"
        "adGroupAd.policySummary.policyTopicEntries",
        rows,
    )


def adcopy_metrics() -> dict:
    rows = []
    for n, (ad_id, cad) in enumerate(zip(ADCOPY_AD_IDS, ADCOPY_COSTS), start=1):
        cid, _, ag_id, _ = _adcopy_ad_home(n)
        rows.append({
            "campaign": {"id": cid,
                         "resourceName": f"customers/{CUSTOMER}/campaigns/{cid}",
                         "status": "ENABLED"},
            "adGroup": {"id": ag_id,
                        "resourceName": f"customers/{CUSTOMER}/adGroups/{ag_id}"},
            "adGroupAd": {
                "status": "ENABLED",
                "resourceName": f"customers/{CUSTOMER}/adGroupAds/{ag_id}~{ad_id}",
                "ad": {"id": ad_id},
            },
            "metrics": {"costMicros": micros(cad), "impressions": str(400 * n),
                        "clicks": str(12 * n)},
        })
    return payload(
        "campaign.id,adGroup.id,adGroupAd.resourceName,adGroupAd.ad.id,"
        "metrics.costMicros,metrics.impressions,metrics.clicks",
        rows,
    )


def adcopy_conversions() -> dict:
    rows = []
    for ad_id, action, count in (
        (ADCOPY_AD_IDS[0], "Basic Trial Signup", 3),
        (ADCOPY_AD_IDS[1], "Business Trial Signup", 1),
    ):
        n = ADCOPY_AD_IDS.index(ad_id) + 1
        cid, _, ag_id, _ = _adcopy_ad_home(n)
        rows.append({
            "campaign": {"id": cid,
                         "resourceName": f"customers/{CUSTOMER}/campaigns/{cid}"},
            "adGroup": {"id": ag_id,
                        "resourceName": f"customers/{CUSTOMER}/adGroups/{ag_id}"},
            "adGroupAd": {
                "resourceName": f"customers/{CUSTOMER}/adGroupAds/{ag_id}~{ad_id}",
                "ad": {"id": ad_id},
            },
            "segments": {"conversionActionName": action},
            "metrics": {"conversions": count, "conversionsValue": count * 350.0},
        })
    return payload(
        "campaign.id,adGroup.id,adGroupAd.resourceName,adGroupAd.ad.id,"
        "segments.conversionActionName,metrics.conversions,"
        "metrics.conversionsValue",
        rows,
    )


def adcopy_asset_serves() -> dict:
    """HEADLINE serve rows for ads 1, 2 and 12, plus one removed-but-served.

    Ad 1: three of its four live headlines serve (the fourth is the dead
    slot) and one serve row carries a text no longer on the ad (history,
    never a live problem). Ad 12 is EXCLUDED by the serve-table selection,
    so its rows prove the account totals still cover an undetailed ad.
    """
    rows = []
    asset_seq = [0]

    def serve(n, ad_id, text, impressions, clicks, label="GOOD"):
        cid, _, ag_id, _ = _adcopy_ad_home(n)
        asset_seq[0] += 1
        aid = f"500000000{asset_seq[0]:02d}"
        rows.append({
            "campaign": {"id": cid,
                         "resourceName": f"customers/{CUSTOMER}/campaigns/{cid}"},
            "adGroup": {"id": ag_id,
                        "resourceName": f"customers/{CUSTOMER}/adGroups/{ag_id}"},
            "adGroupAd": {
                "resourceName": f"customers/{CUSTOMER}/adGroupAds/{ag_id}~{ad_id}",
                "ad": {"id": ad_id},
            },
            "asset": {"id": aid, "textAsset": {"text": text},
                      "resourceName": f"customers/{CUSTOMER}/assets/{aid}"},
            "adGroupAdAssetView": {
                "fieldType": "HEADLINE",
                "performanceLabel": label,
                "resourceName": (
                    f"customers/{CUSTOMER}/adGroupAdAssetViews/"
                    f"{ag_id}~{ad_id}~{aid}~HEADLINE"
                ),
            },
            "metrics": {"impressions": str(impressions), "clicks": str(clicks)},
        })

    a1, a2, a12 = ADCOPY_AD_IDS[0], ADCOPY_AD_IDS[1], ADCOPY_AD_IDS[11]
    serve(1, a1, f"Synthetic headline alpha {a1}", 900, 40)
    serve(1, a1, f"Synthetic headline beta {a1}", 300, 9)
    serve(1, a1, f"Synthetic headline gamma {a1}", 100, 2)
    serve(1, a1, f"Synthetic removed headline {a1}", 250, 7)  # not live: history
    serve(2, a2, f"Synthetic headline alpha {a2}", 700, 30)
    serve(2, a2, f"Synthetic headline beta {a2}", 200, 5)
    serve(12, a12, f"Synthetic headline alpha {a12}", 80, 1)
    serve(12, a12, f"Synthetic headline beta {a12}", 0, 0)  # dead slot with a row
    return payload(
        "campaign.id,adGroup.id,adGroupAd.resourceName,adGroupAd.ad.id,"
        "asset.id,asset.textAsset.text,adGroupAdAssetView.fieldType,"
        "adGroupAdAssetView.performanceLabel,metrics.impressions,"
        "metrics.clicks",
        rows,
    )


SPEND_7D = {BRAND: 90, NONBRAND: 380, PMAX: 520, TOF: 40, SHARED_A: 150, SHARED_B: 30}
SPEND_PRIOR = {BRAND: 80, NONBRAND: 500, PMAX: 480, TOF: 60, SHARED_A: 140, SHARED_B: 25}

FILES = {
    "campaigns-current.json": (
        campaigns_current, "current-as-of-" + FIXTURE_DATE,
        "campaign.status='ENABLED'",
        "Synthetic. 7 enabled campaigns: brand, non-brand, PMax, TOF, a dormant "
        "shell, and TWO on one shared budget id so the VF-19 fan-out guard has "
        "something to catch.",
    ),
    "campaign-cost-is-7d.json": (
        lambda: cost_is(SPEND_7D, sentinel_on=PMAX), "window:LAST_7_DAYS",
        "campaign.status='ENABLED' AND segments.date DURING LAST_7_DAYS",
        "Synthetic. PMax carries the 0.0999 '<10%' impression-share sentinel so "
        "the VF-11 suppression path runs, and a 0.55 lost-to-budget so the "
        "constrained-cluster path runs.",
    ),
    "campaign-cost-is-prior7d.json": (
        lambda: cost_is(SPEND_PRIOR), "window:2026-07-25..2026-07-31",
        "campaign.status='ENABLED' AND segments.date BETWEEN '2026-07-25' AND '2026-07-31'",
        "Synthetic prior week - genuinely the 7 days BEFORE the current "
        "window (the pair guard refuses an overlapping or identical week).",
    ),
    "campaign-conversions-7d.json": (
        lambda: conversions([
            (BRAND, "Basic Trial Signup", 8),
            (NONBRAND, "Basic Trial Signup", 3),
            (NONBRAND, "Business Trial Signup", 1),
            (PMAX, "Plus Trial Signup", 6),
            (SHARED_A, "Pro Trial Signup", 1),
        ]),
        "window:LAST_7_DAYS",
        "campaign.status='ENABLED' AND segments.date DURING LAST_7_DAYS AND "
        "segments.conversion_action_name IN (the 8)",
        "Synthetic. 8-action only. TOF has none on purpose, so the rule-1 "
        "no-efficiency-fact path runs.",
    ),
    "campaign-conversions-prior7d.json": (
        lambda: conversions([
            (BRAND, "Basic Trial Signup", 6),
            (NONBRAND, "Basic Trial Signup", 2),
            (NONBRAND, "Business Trial Signup", 2),
            (PMAX, "Plus Trial Signup", 5),
            (TOF, "Basic Trial Signup", 1),
        ]),
        "window:2026-07-25..2026-07-31",
        "campaign.status='ENABLED' AND segments.date BETWEEN '2026-07-25' AND "
        "'2026-07-31' AND segments.conversion_action_name IN (the 8)",
        "Synthetic prior week. TOF converts here and not this week, so the "
        "membership-differs-between-weeks path runs.",
    ),
    "account-spend-mtd.json": (
        spend_mtd, "window:2026-08-01..2026-08-07",
        "segments.date BETWEEN '2026-08-01' AND '2026-08-07'",
        "Synthetic. Customer-level, NO status filter - that is what makes it the "
        "right pacing instrument and why it must never be differenced against an "
        "enabled-only sum.",
    ),
    "change-events-14d.json": (
        change_events, "window:LAST_14_DAYS",
        "change_event.change_date_time DURING LAST_14_DAYS",
        "Synthetic. Includes a PMax budget change INSIDE its 28-day window - the "
        "row that catches the line-898 ordering bug - and a second operator so a "
        "single-author claim cannot pass.",
    ),
    "search-terms-cost-30d.json": (
        search_terms_cost, "window:LAST_30_DAYS",
        "segments.date DURING LAST_30_DAYS AND metrics.cost_micros > 0",
        "Synthetic. One row per decision branch plus three structural hazards: a "
        "duplicate identity, a criterion id reused across two ad groups, and a "
        "clicks-exceeding-impressions row. ad_group.name is present so the "
        "Editor-import lint has something to pass.",
    ),
    "search-terms-conversions-30d.json": (
        search_terms_conversions, "window:LAST_30_DAYS",
        "segments.date DURING LAST_30_DAYS AND segments.conversion_action_name IN (the 8)",
        "Synthetic conversion side.",
    ),
    "donotnegate-90d-8action.json": (
        donotnegate_90d, "window:2026-05-10..2026-08-07",
        "segments.date BETWEEN '2026-05-10' AND '2026-08-07' AND "
        "segments.conversion_action_name IN (the 8)",
        "Synthetic 90-day clearance. Holds ONE term, 'kept-by-90d', which the "
        "30-day pull shows as a zero-conversion negate candidate. That single row "
        "is the whole test of the guard.",
    ),
    "campaign-assets-current.json": (
        campaign_assets, "current-as-of-" + FIXTURE_DATE,
        "campaign_asset.status='ENABLED'",
        "Synthetic. One asset shared across 3 campaigns, and one attachment on a "
        "campaign absent from campaigns-current.json so the status-filter defect "
        "has something to join out.",
    ),
    "customer-assets-current.json": (
        customer_assets, "current-as-of-" + FIXTURE_DATE,
        "customer_asset.status='ENABLED'",
        "Synthetic account-level assets, so the inheritance path resolves.",
    ),
    "audit-ad-copy-rsa-inventory.json": (
        adcopy_inventory, "current-as-of-" + FIXTURE_DATE,
        "ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND "
        "campaign.status = 'ENABLED' AND campaign.advertising_channel_type = "
        "'SEARCH' AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'",
        "Synthetic. 13 servable RSAs; ad 1 carries a never-serving live "
        "headline (the dead slot) and ad 13 a TRADEMARK policy topic.",
    ),
    "audit-ad-copy-rsa-metrics-30d.json": (
        adcopy_metrics, "window:LAST_30_DAYS",
        "ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND "
        "campaign.status = 'ENABLED' AND campaign.advertising_channel_type = "
        "'SEARCH' AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' AND "
        "segments.date DURING LAST_30_DAYS",
        "Synthetic. Costs arranged so the serve-table rule selects 11 of 13 "
        "(top 10 plus one above the CAD 2,000 floor) - the diet's exercise.",
    ),
    "audit-ad-copy-rsa-conversions-30d.json": (
        adcopy_conversions, "window:LAST_30_DAYS",
        "ad_group_ad.status = 'ENABLED' AND campaign.status = 'ENABLED' AND "
        "ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' AND "
        "segments.conversion_action_name IN (the 8) AND segments.date DURING "
        "LAST_30_DAYS",
        "Synthetic 8-action conversions at the ad level (two-query rule).",
    ),
    "audit-ad-copy-asset-serves-30d.json": (
        adcopy_asset_serves, "window:LAST_30_DAYS",
        "ad_group_ad_asset_view.field_type IN ('HEADLINE','DESCRIPTION') AND "
        "segments.date DURING LAST_30_DAYS",
        "Synthetic HEADLINE serves for ads 1, 2 and 12 - one dead slot, one "
        "removed-but-served row, and ad 12 excluded by the serve-table rule "
        "so the diet's totals-still-cover-everything property is testable.",
    ),
    "asset-field-type-30d.json": (
        asset_field_type, "window:LAST_30_DAYS",
        "segments.date DURING LAST_30_DAYS",
        "Synthetic serve data. Asset-credited and double-counting across field "
        "types by design.",
    ),
}


def write(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    manifest = {
        "raw_dir": f"tools/fixtures/{root.name}",
        "pulled": FIXTURE_DATE,
        "customer": CUSTOMER,
        "currency": "CAD",
        "SYNTHETIC": True,
        "why": (
            "Committed test fixture. ENTIRELY SYNTHETIC - invented campaign ids, "
            "invented amounts, an August 2026 date with invented ids. Contains no account data, which is "
            "why it can live in git while google-ads/outputs/ cannot. Generated "
            "by tools/make_fixture.py; edit that, not this."
        ),
        "temporality_is_declared_not_inferred": True,
        "files": {},
    }
    for name, (fn, temporality, filt, note) in FILES.items():
        data = fn()
        (root / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        manifest["files"][name] = {
            "present": True,
            "rows": len(data["data"]["results"]),
            "truncated": False,
            "temporality": temporality,
            "filter": filt,
            "note": note,
        }
    (root / "_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n", encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the committed fixture is stale")
    args = ap.parse_args()
    if not args.check:
        write(FIXTURE_ROOT)
        print(f"wrote {len(FILES) + 1} files to {FIXTURE_ROOT}")
        return 0
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / FIXTURE_ROOT.name
        write(fresh)
        stale = []
        for path in sorted(fresh.rglob("*.json")):
            committed = FIXTURE_ROOT / path.name
            if not committed.exists() or committed.read_text() != path.read_text():
                stale.append(path.name)
        if stale:
            print("STALE fixture files: " + ", ".join(stale), file=sys.stderr)
            print("Run: python3 tools/make_fixture.py", file=sys.stderr)
            return 1
    print("fixture is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
