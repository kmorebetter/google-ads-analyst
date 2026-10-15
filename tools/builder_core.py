"""Machinery shared by the build_*_facts.py builders (V2 overhaul, Wave 2).

Each of these helpers used to live in one builder and be re-implemented, or
quietly needed, in another. The 90-day-guard regression proved what happens
when the same rule lives in two places: the copies drift and the gap ships.
This module is the single home for the pieces that are genuinely shared —
it is a toolbox, not a framework. Builders keep their own build flow and
alias what they use (`share_sentinel = core.share_sentinel`), so their
public names — which the tests and the skill texts reference — are stable.

What lives here and where it came from:

- The impression-share sentinel family (VF-11), in its *reason-string* form.
  `facts.is_sentinel_share` is the boolean predicate (both ends);
  `share_sentinel` here returns the suppression reason a null fact carries.
  One home for the reasons keeps two builders from wording the same
  suppression differently.
- Target reading (the four-field table). Both money builders carried their
  own copy of where a target actually lives; the 2026-08-20 wrong-field
  incident is exactly the knowledge that must not fork.
- Change-event normalisation for guardrail 7. Weekly and budget-optimize
  each parsed `change_event` rows into their own shapes; the parsing is one
  job, the per-skill filtering stays per-skill.
- Campaign-row keying (the segmented-response hard stop) and signed
  percentage change, from weekly-review.

The literal filter split lives in `gaql.split_literal` and relative-window
resolution in `scopes.resolve_relative_window` — those were forks of shared
modules, so the fix belonged in the shared modules, not here.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pulls
from facts import SEARCH_IMPRESSION_SHARE_SENTINEL, micros_to_currency

#: The shared refusal type. Builders alias `pulls.BuildError` too, so a raise
#: from here lands in the same except clauses and the same ProblemLog bucket.
BuildError = pulls.BuildError


# --------------------------------------------------------------------------
# The impression-share sentinel family, both ends (VF-11)
# --------------------------------------------------------------------------

#: `facts.SEARCH_IMPRESSION_SHARE_SENTINEL` is the low end, "< 10%". Google
#: returns the high end for "> 90%", and it appears in this account on
#: `search_rank_lost_impression_share`, which VF-11 does not name.
SHARE_SENTINEL_LOW = Decimal(str(SEARCH_IMPRESSION_SHARE_SENTINEL))
SHARE_SENTINEL_HIGH = Decimal("0.9001")

#: Every impression-share-family field the pack reads. The sentinels are a
#: property of the family, not of one field.
SHARE_FIELDS = (
    "searchImpressionShare",
    "searchBudgetLostImpressionShare",
    "searchRankLostImpressionShare",
    "searchAbsoluteTopImpressionShare",
    "searchTopImpressionShare",
)


def share_sentinel(value) -> "str | None":
    """The reason a share must be suppressed, or None when it is usable."""
    if value is None:
        return "the field is absent from the saved pull"
    dec = Decimal(str(value))
    if dec == SHARE_SENTINEL_LOW:
        return (
            f"the value is the {SHARE_SENTINEL_LOW} sentinel, which means "
            "'< 10%'. Anything computed from it is a bound, not a value (VF-11)"
        )
    if dec == SHARE_SENTINEL_HIGH:
        return (
            f"the value is the {SHARE_SENTINEL_HIGH} sentinel, which means "
            "'> 90%'. Anything computed from it is a bound, not a value. VF-11 "
            "names only the low sentinel and only search_impression_share; the "
            "sentinels are a property of the whole share family"
        )
    return None


# --------------------------------------------------------------------------
# Target reading (VF-16) — where a target actually lives in this account
# --------------------------------------------------------------------------

#: `campaign.target_roas.target_roas` is the legacy field and returns empty
#: for every enabled campaign. The query succeeds, so the empty result reads
#: as "no campaign has a target" when the real answer lives on
#: `campaign.maximize_conversion_value.target_roas`. An empty target field is
#: a field-name bug until proven otherwise, so the legacy fields are pulled
#: deliberately as controls. Order matters: sub-message fields answer first.
TARGET_READERS = (
    ("maximizeConversionValue", "targetRoas", "target_roas", "ratio"),
    ("maximizeConversions", "targetCpaMicros", "target_cpa", "currency_cad"),
    ("targetRoas", "targetRoas", "legacy_target_roas", "ratio"),
    ("targetCpa", "targetCpaMicros", "legacy_target_cpa", "currency_cad"),
)

#: The legacy control fields alone, for "is the legacy field populated" facts.
LEGACY_TARGET_PATHS = (("targetRoas", "targetRoas"),
                       ("targetCpa", "targetCpaMicros"))


def read_target(campaign: dict, *, include_legacy: bool = True):
    """`(kind, value, field)` for the campaign's set target, or `(None, None, None)`.

    `kind` is `target_roas` / `target_cpa` (prefixed `legacy_` when the
    legacy field answered and `include_legacy` is on); `field` is the GAQL
    field that answered, so a report can say which one did. Currency values
    arrive in account currency, micros already divided.
    """
    for container, leaf, kind, unit in TARGET_READERS:
        if not include_legacy and kind.startswith("legacy_"):
            continue
        value = (campaign.get(container) or {}).get(leaf)
        if value is None:
            continue
        field = f"campaign.{container}.{leaf}"
        if unit == "currency_cad":
            return kind, micros_to_currency(value), field
        return kind, Decimal(str(value)), field
    return None, None, None


def legacy_target_present(campaign: dict) -> bool:
    """Is either legacy control field populated? (It should not be.)

    The `targetCpa` container is only a *legacy* signal when the campaign is
    not actually running a TARGET_CPA strategy — on a real TARGET_CPA
    campaign that field is the live target, not a control.
    """
    return any(
        (campaign.get(c) or {}).get(f) is not None
        for c, f in LEGACY_TARGET_PATHS
        if c != "targetCpa"
        or campaign.get("biddingStrategyType") != "TARGET_CPA"
    )


# --------------------------------------------------------------------------
# Change events (guardrail 7) — one parser, per-skill filtering
# --------------------------------------------------------------------------


def parse_change_events(rows: list) -> list:
    """Normalise raw change_event rows into flat dicts.

    Guardrail 7 is scoped — the *skill* matches the change type to the claim
    type — so this keeps every event and every field the downstream filters
    read: `cid` (empty string when the event carries no campaign),
    `date` (YYYY-MM-DD), `resource_type`, `changed_fields` (verbatim),
    `changed_fields_lower`, and `user`.
    """
    out = []
    for row in rows:
        event = row.get("changeEvent") or {}
        fields = event.get("changedFields") or ""
        out.append({
            "cid": (event.get("campaign") or "").rsplit("/", 1)[-1],
            "date": (event.get("changeDateTime") or "")[:10],
            "resource_type": event.get("changeResourceType") or "",
            "changed_fields": event.get("changedFields"),
            "changed_fields_lower": fields.lower(),
            "changed_fields_raw": fields,
            "user": event.get("userEmail"),
        })
    return out


# --------------------------------------------------------------------------
# Campaign-row keying (VF-19's root-object corollary)
# --------------------------------------------------------------------------


def campaign_id(row: dict) -> str:
    cid = (row.get("campaign") or {}).get("id")
    if not cid:
        raise BuildError(
            "a row has no campaign.id. Every campaign-level join in this skill "
            "keys on it; add it to the SELECT and re-pull."
        )
    return str(cid)


def one_row_per_campaign(filename: str, rows: list) -> dict:
    """Key a campaign-level pull on `campaign.id`, refusing a segmented response.

    Returns `{campaign.id: metrics}`. VF-19 is about child ids reused across
    parents; the campaign is the root object here, so the equivalent hazard
    is a **segmented** response, where the same campaign returns several rows
    and a naive dict keeps only the last one. That is the same
    last-write-wins instability, so it is a hard stop.
    """
    out: dict = {}
    for row in rows:
        cid = campaign_id(row)
        if cid in out:
            raise BuildError(
                f"{filename}: campaign {cid} appears more than once. The "
                "response is segmented, so a dict keyed on campaign.id keeps "
                "only the last row and two runs over identical data can "
                "disagree. Aggregate per campaign before keying (VF-19)."
            )
        out[cid] = row.get("metrics") or {}
    return out


# --------------------------------------------------------------------------
# Signed percentage change
# --------------------------------------------------------------------------


def pct_change(later, earlier):
    """Signed percentage change, or None when the base is zero."""
    later, earlier = Decimal(str(later)), Decimal(str(earlier))
    if earlier == 0:
        return None
    return (later - earlier) / earlier * Decimal(100)


# --------------------------------------------------------------------------
# Week-over-week window pairs (rolling or anchored)
# --------------------------------------------------------------------------

_WINDOW_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
_WINDOW_TOKEN = re.compile(r"^LAST_(\d+)_DAYS$", re.IGNORECASE)


def _parse_window(declared: str):
    """`("range", start, end)` / `("token", days)` / `(None, reason)`."""
    if not (declared or "").startswith("window:"):
        return None, f"declared {declared!r}, which is not a window"
    spec = declared.split(":", 1)[1].strip()
    m = _WINDOW_RANGE.match(spec)
    if m:
        start = dt.date.fromisoformat(m.group(1))
        end = dt.date.fromisoformat(m.group(2))
        if end < start:
            return None, f"range {spec} runs backwards"
        return ("range", start, end), None
    m = _WINDOW_TOKEN.match(spec)
    if m:
        return ("token", int(m.group(1))), None
    return None, f"window spec {spec!r} is neither LAST_N_DAYS nor a..b"


def verify_week_pair(declared_current: str, declared_prior: str,
                     pulled: "dt.date | None", current_name: str,
                     prior_name: str) -> str:
    """Check a this-week/prior-week pull pair is a valid WoW comparison.

    Two accepted shapes. **Rolling**: the current pull declares
    `window:LAST_N_DAYS` (resolved against the pull date) and the prior pull
    declares the adjacent, equal-length `window:a..b` immediately before it.
    **Anchored** (preferred since 2026-08-27 - it kills the
    overlapping-rolling-window artifact class): both declare explicit
    `window:a..b` ranges. Either way the two windows must be equal length and
    adjacent (prior ends the day before current starts), and the current
    window must end before the pull date. A prior week declared as a
    LAST_N_DAYS token is a hard stop: that token resolves to the SAME
    trailing window as the current pull, so the "pair" is one week counted
    twice - the mixed-form contradiction.
    """
    cur, why = _parse_window(declared_current)
    if cur is None:
        raise BuildError(f"{current_name}: {why}, so no week pair exists.")
    pri, why = _parse_window(declared_prior)
    if pri is None:
        raise BuildError(f"{prior_name}: {why}, so no week pair exists.")
    if pri[0] == "token":
        raise BuildError(
            f"{prior_name} declares window:LAST_{pri[1]}_DAYS, which resolves "
            f"to the same trailing window as {current_name}. A prior week "
            "must be an explicit a..b range (the mixed-form contradiction: "
            "one week counted twice is not a pair)."
        )
    _, p_start, p_end = pri
    if cur[0] == "token":
        if pulled is None:
            raise BuildError(
                f"{current_name} declares a relative window and the raw "
                "directory name carries no date to resolve it against."
            )
        c_end = pulled - dt.timedelta(days=1)
        c_start = c_end - dt.timedelta(days=cur[1] - 1)
        shape = "rolling"
    else:
        _, c_start, c_end = cur
        if pulled is not None and c_end >= pulled:
            raise BuildError(
                f"{current_name}: the declared window {c_start}..{c_end} does "
                f"not end before the pull date {pulled}. A week still in "
                "progress is not comparable against a finished one."
            )
        shape = "anchored"
    cur_days = (c_end - c_start).days + 1
    pri_days = (p_end - p_start).days + 1
    if cur_days != pri_days:
        raise BuildError(
            f"{current_name} covers {cur_days} day(s) ({c_start}..{c_end}) "
            f"and {prior_name} covers {pri_days} ({p_start}..{p_end}). A "
            "week-over-week delta needs equal-length windows."
        )
    if p_end != c_start - dt.timedelta(days=1):
        overlap = p_end >= c_start and p_start <= c_end
        raise BuildError(
            f"{current_name} ({c_start}..{c_end}) and {prior_name} "
            f"({p_start}..{p_end}) are not adjacent"
            + (" - the windows OVERLAP, so part of the data is counted on "
               "both sides of the delta" if overlap else "")
            + ". The prior window must end the day before the current one "
            "starts."
        )
    return (
        f"window pair OK ({shape}): {current_name} {c_start}..{c_end} and "
        f"{prior_name} {p_start}..{p_end} are adjacent, equal-length, and "
        "end before the pull date"
    )


__all__ = [
    "BuildError",
    "SHARE_SENTINEL_LOW",
    "SHARE_SENTINEL_HIGH",
    "SHARE_FIELDS",
    "share_sentinel",
    "TARGET_READERS",
    "LEGACY_TARGET_PATHS",
    "read_target",
    "legacy_target_present",
    "parse_change_events",
    "campaign_id",
    "one_row_per_campaign",
    "pct_change",
    "verify_week_pair",
]
