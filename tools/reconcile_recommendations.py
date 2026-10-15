#!/usr/bin/env python3
"""Reconcile `recommendations-log.csv` against what the account actually did.

Usage:
    python3 tools/reconcile_recommendations.py <outputs_dir> [--raw-dir <dir>] [--apply]

The log's designed loop never closed: every row was written `open`, status
transitions were a manual CSV edit nobody made (54 rows, all `open`, by
2026-08-21 - MAINTENANCE.md calls it the largest problem), so the
`after_value` backfill keyed on `applied` could never fire. Meanwhile the
changes themselves were visible all along in `change_event` and in the
current-state structure pull. This tool closes the loop from that evidence:

1. **Detection** - for each `open` row, read the metric's current value from
   `campaigns-current.json` and propose `open -> applied` when it moved the
   way the recommendation asked, with a dated `change_event` as evidence when
   one is in reach (the resource only looks back 14 days, so older changes
   get the observation date labelled "on-or-before"). Anything ambiguous -
   unparseable values, unknown campaign, no current-state pull - is
   `needs_review`, never auto-flipped. A value still at `before_value` stays
   `open`, and long-open rows (>= 14 days) are surfaced as their own signal.
2. **Backfill** - the interim spec from `budget-optimize` step 8: `applied`
   rows at least 7 days old with a blank `after_value` get the current value
   and its date. Only backfill; a filled cell is never overwritten.
3. **Outcome** - for `applied` rows at least 14 days old, compute the
   targeted efficiency metric (assumed-value 8-action CPA, or ROAS for a
   `target_roas` row) over matched pre/post pulls when the operator has
   saved them (filenames below), apply the conversion floor from
   `account-config.json`, flag confounds from `change_event`, and propose
   `improved` / `no_change` / `worsened` with the delta. When the pulls are
   absent the verdict is `insufficient_data: <what is needed>` in the REPORT
   ONLY - the CSV cell stays blank so a later run with data can fill it.

Expected outcome-pass filenames in the raw dir (windows declared in
`_manifest.json` like any pull; matched pre/post windows around the applied
date, pulled by `outcomes-review`):

    outcome-cost-pre14d.json          outcome-cost-post14d.json
    outcome-conversions-pre14d.json   outcome-conversions-post14d.json

Cost pulls carry `campaign.id` + `metrics.costMicros`; conversion pulls the
8-action-restricted segment pull (`campaign.id`,
`segments.conversionActionName`, `metrics.conversions`, `.conversionsValue`).

Without `--apply` nothing is written: the report prints what would change.
With `--apply` the CSV is rewritten in place - original columns and order
preserved, the six loop-closing columns (`applied_date`, `applied_evidence`,
`outcome_verdict`, `outcome_delta`, `outcome_window`, `outcome_date`)
appended to the header once if missing, quoting preserved for
comma-carrying campaign names. All passes run on in-memory state in order
(detect, then backfill, then outcome), so one `--apply` does everything the
evidence supports and an immediate second run proposes nothing.

Exit codes: 0 reconciled (report-only or applied), 1 malformed CSV (a row's
column count differs from the header - the row is named), 2 missing log.

Stdlib only. Reads raw JSON already on disk; makes no API calls; read-only
against the account by construction.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_config

LOG_NAME = "recommendations-log.csv"

BASE_COLUMNS = [
    "date_recommended", "skill", "campaign", "metric", "before_value",
    "recommendation", "reasoning", "status", "after_value", "after_date",
]
NEW_COLUMNS = [
    "applied_date", "applied_evidence", "outcome_verdict", "outcome_delta",
    "outcome_window", "outcome_date",
]

CAMPAIGNS_CURRENT = "campaigns-current.json"
CHANGE_EVENTS = "change-events-14d.json"
OUTCOME_FILES = {
    "cost_pre": "outcome-cost-pre14d.json",
    "cost_post": "outcome-cost-post14d.json",
    "conv_pre": "outcome-conversions-pre14d.json",
    "conv_post": "outcome-conversions-post14d.json",
}

LONG_OPEN_DAYS = 14
BACKFILL_MIN_DAYS = 7
OUTCOME_MIN_DAYS = 14

#: The only metrics `current_value` can read out of `campaigns-current.json`.
#: A row whose metric is outside this set can never auto-flip, so the writers
#: (budget-optimize step 8, the canonical schema) must use one of these names
#: whenever the recommendation targets a campaign setting.
READABLE_METRICS = ("daily_budget", "target_roas", "target_cpa")

#: Metrics that are legitimate recommendations but have **no account field
#: this tool can read**, each mapped to how a human settles it instead.
#:
#: These are not errors and never will be readable: creative text, keyword
#: membership, owner inputs and observed performance are not settings on a
#: campaign, so no current-state pull can confirm them. Before 2026-08-28 they
#: were reported as "metric is not one this tool can read" and pooled with real
#: parse failures under "needs review", which is how 6 of 9 rows became noise a
#: reader learns to skip. They now get their own terminal disposition: still
#: `open` in the CSV (this tool never guesses at a status), but reported apart
#: from rows that a fix would make readable.
MANUAL_METRICS = {
    "search_term_negatives":
        "negative keywords are not in the current-state pull - confirm against "
        "the campaign's negative list or the same day's mine-search-terms CSV",
    "negation_thresholds":
        "an owner input, not an account setting - closes when the four "
        "thresholds are supplied (STANDING-ITEMS.md)",
    "ad_copy":
        "creative text - judge against current RSA inventory via audit-ad-copy, "
        "never a numeric field",
    "keywords":
        "keyword membership - judge against a keyword pull, not campaigns-current",
    "pacing_pct_of_prorated_budget":
        "an account-level derived figure, not a campaign field - weekly-review "
        "recomputes it fresh every run",
    "spend_impressions_clicks":
        "observed performance, not a setting - judge from the cost pulls for "
        "the window after the change landed",
}

#: A metric cell naming more than one metric (`daily_budget + target_roas`).
#: One row carries one `before_value` and one `after_value`, so a compound row
#: is not reconcilable at any effort: there is no single value to compare. The
#: fix belongs at the write site - one row per metric - so this is reported as
#: a malformed row, not as an unreadable metric.
_COMPOUND_METRIC = re.compile(r"\s(?:\+|/|,|and)\s|,")


def classify_metric(metric: str) -> "tuple[str, str]":
    """(kind, detail) for a row's metric cell.

    kind is "readable", "compound", "manual" or "unknown". Only "readable"
    reaches `current_value`; the rest each get their own disposition so a
    reader can tell "nobody has judged this yet" from "no machine ever will".
    """
    name = (metric or "").strip().lower()
    if name in READABLE_METRICS:
        return "readable", ""
    if _COMPOUND_METRIC.search(name):
        return "compound", (
            f"metric {metric!r} names more than one metric, and a row carries "
            "one before/after pair - split it into one row per metric at the "
            "write site (budget-optimize step 8)")
    if name in MANUAL_METRICS:
        return "manual", MANUAL_METRICS[name]
    return "unknown", (
        f"metric {metric!r} is not one this tool can read. Readable: "
        + ", ".join(READABLE_METRICS)
        + ". Judged by hand: " + ", ".join(sorted(MANUAL_METRICS))
        + ". Use one of those names, or add it to MANUAL_METRICS if it is "
          "genuinely not a readable campaign setting.")

#: First thing that looks like a number, after $/CAD/USD/commas are stripped.
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
#: "increase to $300/day", "tighten tROAS from 0.85 to 1.1" - the value after
#: the last " to ".
_TO_VALUE = re.compile(r"\bto\s+(?:CAD\s*|USD\s*|\$)?(-?[\d,]+(?:\.\d+)?)", re.I)
_UP_WORDS = re.compile(r"\b(increase|raise|add|grow|expand)\b", re.I)
_DOWN_WORDS = re.compile(r"\b(cut|decrease|reduce|lower|trim|shrink)\b", re.I)
#: A budget id embedded in the campaign cell ("budget 80000002: A, B").
_BUDGET_ID = re.compile(r"\bbudget\s*(?:id\s*)?[:#]?\s*(\d{6,})", re.I)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class MalformedLog(Exception):
    """A data row whose column count differs from the header's."""


# --------------------------------------------------------------------------
# CSV in / out. `csv` handles the quoted-comma campaign names; QUOTE_MINIMAL
# on the way out re-quotes exactly the fields that need it (the Tier 1 lint
# rule this log has carried since 2026-07-14).
# --------------------------------------------------------------------------

def read_log(path: Path) -> "tuple[list[str], list[dict]]":
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise MalformedLog(f"{path.name}: empty file, not even a header")
        rows = []
        for lineno, raw in enumerate(reader, start=2):
            if not raw:
                continue  # a trailing blank line is not a row
            if len(raw) != len(header):
                raise MalformedLog(
                    f"{path.name} line {lineno}: {len(raw)} columns against a "
                    f"{len(header)}-column header - fix the row before "
                    "reconciling (an unquoted comma shifts every later column)"
                )
            rows.append(dict(zip(header, raw)))
    return header, rows


def write_log(path: Path, header: "list[str]", rows: "list[dict]") -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for row in rows:
            writer.writerow([row.get(col, "") for col in header])


# --------------------------------------------------------------------------
# Raw-pull readers. Same JSON shape as every pull in this pack; kept local
# (not pulls.load_results) because an absent or empty file here is a
# disposition to report, never a BuildError that stops the run.
# --------------------------------------------------------------------------

def _results(raw_dir: "Path | None", filename: str) -> "list[dict] | None":
    if raw_dir is None:
        return None
    path = raw_dir / filename
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ((payload.get("data") or {}).get("results")) or []


def observation_date(raw_dir: "Path | None") -> "str | None":
    """The date the raw dir's current-state pulls describe: its dir name."""
    if raw_dir is None:
        return None
    match = _DATE.search(raw_dir.name)
    return match.group(0) if match else None


def manifest_window(raw_dir: "Path | None", filename: str) -> "str | None":
    if raw_dir is None:
        return None
    path = raw_dir / "_manifest.json"
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    entry = manifest.get(filename) or {}
    temporality = entry.get("temporality", "")
    return temporality.removeprefix("window:") if temporality.startswith("window:") else None


@dataclass
class Structure:
    """Index over campaigns-current.json, keyed the way each metric needs."""

    by_campaign_id: dict = field(default_factory=dict)
    by_name: dict = field(default_factory=dict)      # lower name -> [rows]
    budget_amount: dict = field(default_factory=dict)  # budget id -> Decimal CAD
    budget_campaigns: dict = field(default_factory=dict)  # budget id -> [campaign ids]

    @classmethod
    def load(cls, raw_dir: "Path | None") -> "Structure | None":
        results = _results(raw_dir, CAMPAIGNS_CURRENT)
        if not results:
            return None
        self = cls()
        for row in results:
            campaign = row.get("campaign") or {}
            budget = row.get("campaignBudget") or {}
            cid = str(campaign.get("id", ""))
            if not cid:
                continue
            self.by_campaign_id[cid] = row
            self.by_name.setdefault((campaign.get("name") or "").strip().lower(), []).append(row)
            bid = str(budget.get("id", ""))
            if bid:
                micros = budget.get("amountMicros")
                if micros is not None:
                    self.budget_amount[bid] = Decimal(str(micros)) / 1_000_000
                self.budget_campaigns.setdefault(bid, []).append(cid)
        return self


def current_value(structure: Structure, row: dict) -> "tuple[Decimal | None, str, list[str]]":
    """(value, note, campaign_ids) for the row's metric - or (None, why, [])."""
    metric = (row.get("metric") or "").strip().lower()
    cell = row.get("campaign") or ""

    if metric == "daily_budget":
        embedded = _BUDGET_ID.search(cell)
        if embedded:
            bid = embedded.group(1)
            if bid not in structure.budget_amount:
                return None, f"budget id {bid} not in {CAMPAIGNS_CURRENT}", []
            return (structure.budget_amount[bid],
                    f"budget {bid} (shared across "
                    f"{len(structure.budget_campaigns[bid])} campaigns)",
                    list(structure.budget_campaigns[bid]))
        matches = structure.by_name.get(cell.strip().lower(), [])
        if not matches:
            return None, "campaign not found in current-state pull", []
        budget_ids = {str((m.get("campaignBudget") or {}).get("id", "")) for m in matches}
        budget_ids.discard("")
        if len(budget_ids) != 1:
            return None, "shared-pool ambiguity: name maps to more than one budget id", []
        bid = budget_ids.pop()
        if bid not in structure.budget_amount:
            return None, f"budget id {bid} carries no amount in the pull", []
        pool = structure.budget_campaigns[bid]
        note = f"budget {bid}" + (f" (pool of {len(pool)})" if len(pool) > 1 else "")
        return structure.budget_amount[bid], note, list(pool)

    if metric in ("target_roas", "target_cpa"):
        matches = structure.by_name.get(cell.strip().lower(), [])
        if len(matches) != 1:
            why = "campaign not found in current-state pull" if not matches else \
                  "campaign name matches more than one campaign"
            return None, why, []
        campaign = matches[0].get("campaign") or {}
        cid = str(campaign.get("id"))
        if metric == "target_roas":
            value = ((campaign.get("maximizeConversionValue") or {}).get("targetRoas")
                     or (campaign.get("targetRoas") or {}).get("targetRoas"))
            if value is None:
                return None, "no target ROAS set on either field", [cid]
            return Decimal(str(value)), f"campaign {cid}", [cid]
        micros = ((campaign.get("maximizeConversions") or {}).get("targetCpaMicros")
                  or (campaign.get("targetCpa") or {}).get("targetCpaMicros"))
        if micros is None:
            return None, "no target CPA set on either field", [cid]
        return Decimal(str(micros)) / 1_000_000, f"campaign {cid}", [cid]

    return None, f"metric {metric!r} is not one this tool can read", []


# --------------------------------------------------------------------------
# Parsing the row's own text. Defensive on purpose: a value that does not
# parse is a needs_review disposition, never a guess (the pack's rule).
# --------------------------------------------------------------------------

def parse_number(text: str) -> "Decimal | None":
    cleaned = (text or "").replace(",", "")
    match = _NUMBER.search(cleaned)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:  # pragma: no cover - _NUMBER guarantees a parse
        return None


def recommended_direction(row: dict) -> "tuple[Decimal | None, int | None]":
    """(target value, direction) from the recommendation text.

    direction: +1 up, -1 down, None unknown. "tighten" is metric-relative:
    a tighter tROAS is a higher target, a tighter tCPA a lower one.
    """
    text = row.get("recommendation") or ""
    to_value = _TO_VALUE.search(text.replace(",", ""))
    target = None
    if to_value:
        try:
            target = Decimal(to_value.group(1).replace(",", ""))
        except InvalidOperation:
            target = None
    direction = None
    if _UP_WORDS.search(text):
        direction = +1
    elif _DOWN_WORDS.search(text):
        direction = -1
    elif re.search(r"\btighten\b", text, re.I):
        metric = (row.get("metric") or "").strip().lower()
        direction = +1 if metric == "target_roas" else -1
    return target, direction


def find_change_event(raw_dir: "Path | None", metric: str,
                      campaign_ids: "list[str]") -> "tuple[str, str] | None":
    """(date, description) of the latest matching change event, if any."""
    results = _results(raw_dir, CHANGE_EVENTS)
    if not results:
        return None
    wanted = set(campaign_ids)
    best = None
    for row in results:
        event = row.get("changeEvent") or {}
        campaign = (event.get("campaign") or "").rsplit("/", 1)[-1]
        if campaign not in wanted:
            continue
        changed = event.get("changedFields") or ""
        resource = event.get("changeResourceType") or ""
        if metric == "daily_budget":
            if resource != "CAMPAIGN_BUDGET" or "amountMicros" not in changed:
                continue
        elif metric == "target_roas":
            if "targetRoas" not in changed:
                continue
        elif metric == "target_cpa":
            if "targetCpa" not in changed:
                continue
        else:
            continue
        when = (event.get("changeDateTime") or "")[:10]
        if best is None or when > best[0]:
            best = (when, f"change_event {when} {resource} {changed}")
    return best


# --------------------------------------------------------------------------
# The three passes. Each appends report lines and mutates rows in memory;
# write_log persists them only under --apply.
# --------------------------------------------------------------------------

def _age_days(from_date: "str | None", today: date) -> "int | None":
    match = _DATE.search(from_date or "")
    if not match:
        return None
    return (today - date.fromisoformat(match.group(0))).days


def _fmt(value: Decimal, metric: str) -> str:
    if metric == "daily_budget":
        return f"CAD {value:.2f}/day"
    return format(value.normalize(), "f")  # no scientific notation, ever


@dataclass
class Report:
    proposed_applied: list = field(default_factory=list)
    still_open: list = field(default_factory=list)
    long_open: list = field(default_factory=list)
    needs_review: list = field(default_factory=list)
    manual_only: list = field(default_factory=list)
    malformed: list = field(default_factory=list)
    backfilled: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    insufficient: list = field(default_factory=list)
    migration_needed: bool = False


def detection_pass(rows: "list[dict]", structure: "Structure | None",
                   raw_dir: "Path | None", obs_date: "str | None",
                   report: Report, today: date) -> None:
    for index, row in enumerate(rows, start=1):
        if (row.get("status") or "").strip() != "open":
            continue
        label = f"[{index}] {row.get('campaign', '?')} {row.get('metric', '?')}"
        # Classify the metric before anything else. A compound or judged-by-hand
        # row is dispositioned on the row alone: it needs no current-state pull,
        # and a missing before_value is not what is wrong with it.
        kind, detail = classify_metric(row.get("metric") or "")
        if kind == "compound":
            report.malformed.append(f"{label}: {detail}")
            continue
        if kind == "manual":
            age = _age_days(row.get("date_recommended"), today)
            aged = f" [open {age}d]" if age is not None else ""
            report.manual_only.append(f"{label}{aged}: {detail}")
            continue
        if kind == "unknown":
            # Names the readable vocabulary, so the writer can fix the row
            # rather than rediscovering the same dead end next run.
            report.needs_review.append(f"{label}: {detail}")
            continue
        if structure is None:
            report.needs_review.append(
                f"{label}: no current-state pull provided "
                f"(--raw-dir with {CAMPAIGNS_CURRENT})")
            continue
        before = parse_number(row.get("before_value") or "")
        if before is None:
            report.needs_review.append(
                f"{label}: before_value {row.get('before_value')!r} has no "
                "parseable number")
            continue
        current, note, campaign_ids = current_value(structure, row)
        if current is None:
            report.needs_review.append(f"{label}: {note}")
            continue
        if current == before:
            age = _age_days(row.get("date_recommended"), today)
            line = f"{label}: unchanged at {_fmt(current, row['metric'])}" + (
                f" - open {age} days" if age is not None else "")
            report.still_open.append(line)
            if age is not None and age >= LONG_OPEN_DAYS:
                report.long_open.append(line)
            continue

        target, direction = recommended_direction(row)
        moved_up = current > before
        if target is not None:
            toward = (target > before) == moved_up
            if not toward:
                report.needs_review.append(
                    f"{label}: value moved {_fmt(before, row['metric'])} -> "
                    f"{_fmt(current, row['metric'])}, opposite to the "
                    f"recommendation ({row.get('recommendation')!r})")
                continue
            # A move far past the recommended figure more likely reflects a
            # different intervention than this recommendation being applied.
            # Direction-only matching stays the contract; overshoot beyond
            # 2x the recommended delta is a human call, never an auto-flip.
            recommended_delta = abs(target - before)
            if recommended_delta > 0 and abs(current - before) > 2 * recommended_delta:
                report.needs_review.append(
                    f"{label}: value moved {_fmt(before, row['metric'])} -> "
                    f"{_fmt(current, row['metric'])}, overshooting the "
                    f"recommended {_fmt(target, row['metric'])} by more than "
                    "2x the recommended delta - likely a different "
                    "intervention, not this recommendation")
                continue
            exact = current == target
        elif direction is not None:
            if (direction > 0) != moved_up:
                report.needs_review.append(
                    f"{label}: value moved {_fmt(before, row['metric'])} -> "
                    f"{_fmt(current, row['metric'])}, opposite to the "
                    f"recommendation ({row.get('recommendation')!r})")
                continue
            exact = False
        else:
            report.needs_review.append(
                f"{label}: value changed ({_fmt(before, row['metric'])} -> "
                f"{_fmt(current, row['metric'])}) but the recommendation's "
                "direction could not be parsed - flip by hand if this was it")
            continue

        event = find_change_event(raw_dir, (row.get("metric") or "").strip().lower(),
                                  campaign_ids)
        if event:
            applied_date, event_note = event
        else:
            applied_date = f"on-or-before {obs_date or 'unknown-date'}"
            event_note = "no matching change_event in the 14-day pull"
        evidence = (
            f"{row['metric']} {_fmt(before, row['metric'])} -> "
            f"{_fmt(current, row['metric'])}"
            + ("" if exact else " (moved as recommended, not to the exact figure)")
            + f"; {note}; {CAMPAIGNS_CURRENT} {obs_date or 'undated'}; {event_note}"
        )
        row["status"] = "applied"
        row["applied_date"] = applied_date
        row["applied_evidence"] = evidence
        report.proposed_applied.append(f"{label}: open -> applied. {evidence}")


def backfill_pass(rows: "list[dict]", structure: "Structure | None",
                  obs_date: "str | None", report: Report, today: date) -> None:
    if structure is None:
        return
    for index, row in enumerate(rows, start=1):
        if (row.get("status") or "").strip() != "applied":
            continue
        if (row.get("after_value") or "").strip():
            continue  # never overwrite a filled cell
        basis = row.get("applied_date") or row.get("after_date") or row.get("date_recommended")
        age = _age_days(basis, today)
        if age is None or age < BACKFILL_MIN_DAYS:
            continue
        current, note, _ = current_value(structure, row)
        if current is None:
            continue  # nothing readable to backfill; detection already reported why
        row["after_value"] = _fmt(current, row["metric"])
        row["after_date"] = obs_date or ""
        report.backfilled.append(
            f"[{index}] {row.get('campaign', '?')}: after_value = "
            f"{row['after_value']} ({note}, {obs_date or 'undated'})")


def _metric_sides(raw_dir: "Path | None", campaign_ids: "list[str]",
                  metric: str) -> "tuple[dict | None, str | None]":
    """Pre/post cost+conversions for the campaigns, or (None, what's missing)."""
    missing = [name for name in OUTCOME_FILES.values()
               if raw_dir is None or not (raw_dir / name).exists()]
    if missing:
        return None, "needs " + ", ".join(missing)
    wanted = set(campaign_ids)
    sides = {}
    for side, cost_key, conv_key in (("pre", "cost_pre", "conv_pre"),
                                     ("post", "cost_post", "conv_post")):
        cost = Decimal(0)
        for row in _results(raw_dir, OUTCOME_FILES[cost_key]) or []:
            if str((row.get("campaign") or {}).get("id")) in wanted:
                cost += Decimal(str((row.get("metrics") or {}).get("costMicros", 0))) / 1_000_000
        conversions = Decimal(0)
        value = Decimal(0)
        for row in _results(raw_dir, OUTCOME_FILES[conv_key]) or []:
            if str((row.get("campaign") or {}).get("id")) in wanted:
                metrics = row.get("metrics") or {}
                conversions += Decimal(str(metrics.get("conversions", 0)))
                value += Decimal(str(metrics.get("conversionsValue", 0)))
        sides[side] = {"cost": cost, "conversions": conversions, "value": value}
    return sides, None


def outcome_pass(rows: "list[dict]", structure: "Structure | None",
                 raw_dir: "Path | None", obs_date: "str | None",
                 report: Report, today: date) -> None:
    floor = account_config.threshold("conversion_floor")
    no_change_band = account_config.threshold("wow_move_pct")
    for index, row in enumerate(rows, start=1):
        if (row.get("status") or "").strip() != "applied":
            continue
        if (row.get("outcome_verdict") or "").strip():
            continue
        basis = row.get("applied_date") or row.get("after_date")
        age = _age_days(basis, today)
        if age is None or age < OUTCOME_MIN_DAYS:
            continue
        label = f"[{index}] {row.get('campaign', '?')} {row.get('metric', '?')}"
        metric = (row.get("metric") or "").strip().lower()
        if structure is None:
            report.insufficient.append(
                f"{label}: insufficient_data: needs {CAMPAIGNS_CURRENT} to "
                "resolve the campaign")
            continue
        _, _, campaign_ids = current_value(structure, row)
        if not campaign_ids:
            report.insufficient.append(
                f"{label}: insufficient_data: campaign not resolvable from "
                "the current-state pull")
            continue
        sides, missing = _metric_sides(raw_dir, campaign_ids, metric)
        if sides is None:
            report.insufficient.append(f"{label}: insufficient_data: {missing}")
            continue
        if sides["pre"]["conversions"] < floor or sides["post"]["conversions"] < floor:
            report.insufficient.append(
                f"{label}: insufficient_data: conversions below the floor of "
                f"{floor} on at least one side "
                f"(pre {sides['pre']['conversions']}, post {sides['post']['conversions']})")
            continue

        if metric == "target_roas":
            name = "assumed-value ROAS"
            pre = sides["pre"]["value"] / sides["pre"]["cost"]
            post = sides["post"]["value"] / sides["post"]["cost"]
            better_when_up = True
        else:  # daily_budget and target_cpa are judged on CPA
            name = "assumed-value CPA"
            pre = sides["pre"]["cost"] / sides["pre"]["conversions"]
            post = sides["post"]["cost"] / sides["post"]["conversions"]
            better_when_up = False
        delta_pct = (post - pre) / pre * 100
        if abs(delta_pct) < no_change_band:
            verdict = "no_change"
        elif (delta_pct > 0) == better_when_up:
            verdict = "improved"
        else:
            verdict = "worsened"

        applied_match = _DATE.search(row.get("applied_date") or "")
        confounded = False
        for event_row in _results(raw_dir, CHANGE_EVENTS) or []:
            event = event_row.get("changeEvent") or {}
            campaign = (event.get("campaign") or "").rsplit("/", 1)[-1]
            when = (event.get("changeDateTime") or "")[:10]
            if campaign in set(campaign_ids) and applied_match and when > applied_match.group(0):
                confounded = True
                break
        if confounded:
            verdict = f"confounded: {verdict}"

        pre_window = manifest_window(raw_dir, OUTCOME_FILES["cost_pre"])
        post_window = manifest_window(raw_dir, OUTCOME_FILES["cost_post"])
        window = (f"pre {pre_window} vs post {post_window}"
                  if pre_window and post_window else "pre/post windows undeclared")
        row["outcome_verdict"] = verdict
        row["outcome_delta"] = (
            f"{delta_pct:+.1f}% ({name} {pre:.2f} -> {post:.2f})")
        row["outcome_window"] = window
        row["outcome_date"] = obs_date or ""
        report.outcomes.append(
            f"{label}: {verdict} - {row['outcome_delta']} [{window}]")


# --------------------------------------------------------------------------
# Report and CLI.
# --------------------------------------------------------------------------

def render(report: Report, log_path: Path, raw_dir: "Path | None",
           obs_date: "str | None", row_count: int, apply: bool) -> str:
    lines = [
        f"Reconciliation report - {log_path} ({row_count} data rows)",
        f"Raw dir: {raw_dir or '(none - detection and outcome limited)'} "
        f"(observation date {obs_date or 'unknown'})",
        f"Mode: {'APPLIED - CSV updated' if apply else 'report-only (re-run with --apply to write)'}",
        "",
        "Dispositions:",
        f"  propose open -> applied .. {len(report.proposed_applied)}",
        f"  still open ............... {len(report.still_open)}"
        f"   ({len(report.long_open)} long-open, >= {LONG_OPEN_DAYS} days)",
        f"  needs review ............. {len(report.needs_review)}",
        f"  judged by hand ........... {len(report.manual_only)}"
        "   (no account field to read - not pending work)",
        f"  malformed rows ........... {len(report.malformed)}",
        f"  after_value backfilled ... {len(report.backfilled)}",
        f"  outcome verdicts ......... {len(report.outcomes)} proposed, "
        f"{len(report.insufficient)} insufficient data",
        f"  schema migration ......... "
        + ("needed (6 columns to append)" if report.migration_needed else "not needed"),
    ]
    for title, entries in (
        ("Open -> applied:", report.proposed_applied),
        ("Needs review (never auto-flipped):", report.needs_review),
        ("Malformed rows - fix at the write site, then re-run:",
         report.malformed),
        ("Judged by hand (no account field can ever confirm these):",
         report.manual_only),
        ("Still open:", report.still_open),
        (f"Long-open (>= {LONG_OPEN_DAYS} days) - the standing signal:", report.long_open),
        ("Backfilled:", report.backfilled),
        ("Outcomes:", report.outcomes),
        ("Outcomes not yet measurable:", report.insufficient),
    ):
        if entries:
            lines += ["", title] + [f"  {entry}" for entry in entries]
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("outputs_dir", type=Path)
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--today", default=None, help=argparse.SUPPRESS)  # tests
    args = parser.parse_args(argv)

    log_path = args.outputs_dir / LOG_NAME
    if not log_path.exists():
        print(f"{log_path}: not found. Nothing to reconcile - the log is "
              "created by the first skill that logs a recommendation "
              "(schema in budget-optimize step 8).", file=sys.stderr)
        return 2
    try:
        header, rows = read_log(log_path)
    except MalformedLog as err:
        print(str(err), file=sys.stderr)
        return 1

    today = date.fromisoformat(args.today) if args.today else date.today()
    report = Report()
    missing_columns = [col for col in NEW_COLUMNS if col not in header]
    report.migration_needed = bool(missing_columns)
    header_out = header + missing_columns
    for row in rows:
        for col in missing_columns:
            row.setdefault(col, "")

    structure = Structure.load(args.raw_dir)
    obs_date = observation_date(args.raw_dir)
    detection_pass(rows, structure, args.raw_dir, obs_date, report, today)
    backfill_pass(rows, structure, obs_date, report, today)
    outcome_pass(rows, structure, args.raw_dir, obs_date, report, today)

    print(render(report, log_path, args.raw_dir, obs_date, len(rows), args.apply))
    if args.apply:
        write_log(log_path, header_out, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
