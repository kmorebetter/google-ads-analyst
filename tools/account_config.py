#!/usr/bin/env python3
"""Loader for `account-config.json` - the single source for account constants.

Every account or calendar constant the builders used to bake in (campaign-ID
buckets, the 8 conversion actions, stated monthly budgets, thresholds,
elasticity bands) lives in `account-config.json` beside this file. Builders
import from here so a new month, a renamed campaign or a re-tuned threshold is
a config edit, not a code edit.

The stated-budget contract (VF-14, campaign-guardrails rule 8):

- `stated_budget("2026-08")` returns a `StatedBudget` with Decimal figures.
- A month listed with a null figure returns one with `cad=None` and the
  config's `no_split_reason` - the builder emits null-with-reason pacing
  facts for it and keeps running. It never derives a figure and never carries
  another month's forward.
- A month absent from the config entirely raises `UnknownMonthError`: nothing
  is known about it, not even that its split is unstated, so refusing loudly
  beats guessing.

Stdlib only. Values that feed arithmetic are stored as JSON strings and
converted to Decimal here, exactly once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "account-config.json"


class UnknownMonthError(Exception):
    """A month the config says nothing about at all."""


@dataclass(frozen=True)
class StatedBudget:
    """One month's stated Google Ads budget, or its absence with the reason."""

    month: str                  # "2026-08"
    cad: "Decimal | None"
    usd: "Decimal | None"
    source: "str | None"        # where the stated figure was read from
    reason: "str | None"        # why there is no figure, when cad is None

    @property
    def stated(self) -> bool:
        return self.cad is not None


@lru_cache(maxsize=1)
def _raw() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _dec(value) -> "Decimal | None":
    return None if value is None else Decimal(str(value))


# --------------------------------------------------------------------------
# Accessors. Names mirror the config sections.
# --------------------------------------------------------------------------

def customer_id() -> str:
    return _raw()["customer_id"]


def eight_actions() -> "tuple[str, ...]":
    return tuple(_raw()["conversion_actions"]["eight"])


def team_actions() -> "tuple[str, ...]":
    return tuple(_raw()["conversion_actions"]["priority"])


def brand_campaign_ids() -> "frozenset[str]":
    return frozenset(_raw()["campaign_buckets"]["brand_campaign_ids"])


def brand_core_campaign_id() -> str:
    return _raw()["campaign_buckets"]["brand_core_campaign_id"]


def tof_campaign_ids() -> "frozenset[str]":
    return frozenset(_raw()["campaign_buckets"]["tof_campaign_ids"])


def tof_channels() -> "frozenset[str]":
    return frozenset(_raw()["campaign_buckets"]["tof_channels"])


def tof_channels_ordered() -> "tuple[str, ...]":
    """Config order preserved, for scope strings that print the tuple."""
    return tuple(_raw()["campaign_buckets"]["tof_channels"])


def legacy_campaign_ids() -> "frozenset[str]":
    return frozenset(_raw()["campaign_buckets"]["legacy_campaign_ids"])


def conquest_tokens() -> "tuple[str, ...]":
    """Name fragments that mark a conquesting/competitor campaign."""
    return tuple(_raw()["campaign_buckets"].get(
        "conquest_tokens", ["conquest", "competitor", "competitive"]))


def brand_tokens() -> "tuple[str, ...]":
    return tuple(_raw()["campaign_buckets"]["brand_tokens"])


def fx_usd_to_cad() -> Decimal:
    return Decimal(_raw()["budgets"]["fx_usd_to_cad"])


def fx_note() -> str:
    return _raw()["budgets"]["fx_note"]


def stated_budget(month: str) -> StatedBudget:
    """The stated budget for `month` ("YYYY-MM"), or its absence with reason."""
    months = _raw()["budgets"]["months"]
    if month not in months:
        raise UnknownMonthError(
            f"account-config.json knows nothing about month {month!r} - not "
            "even that its Google Ads split is unstated. Add it to "
            "budgets.months (with the stated figure from the account owner, "
            "or null) before building against this month's raw directory."
        )
    entry = months[month]
    cad = _dec(entry.get("cad"))
    return StatedBudget(
        month=month,
        cad=cad,
        usd=_dec(entry.get("usd")),
        source=entry.get("source"),
        reason=None if cad is not None else _raw()["budgets"]["no_split_reason"],
    )


def months_with_no_stated_split() -> "tuple[str, ...]":
    months = _raw()["budgets"]["months"]
    return tuple(m for m in sorted(months) if months[m].get("cad") is None)


def no_split_reason() -> str:
    return _raw()["budgets"]["no_split_reason"]


def planned_step_down_pct() -> Decimal:
    return Decimal(_raw()["budgets"]["planned_step_down"]["pct"])


def planned_step_down_window() -> str:
    return _raw()["budgets"]["planned_step_down"]["window"]


def elasticity_bands() -> "tuple[tuple[str, Decimal | None, Decimal | None, str], ...]":
    """(name, max_increase_usd, cpa_degradation_pct, note) per band."""
    return tuple(
        (
            band["name"],
            _dec(band["max_increase_usd_per_month"]),
            _dec(band["cpa_degradation_pct"]),
            band["note"],
        )
        for band in _raw()["budgets"]["elasticity_bands"]
    )


def elasticity_scope() -> str:
    return _raw()["budgets"]["elasticity_scope"]


def cac_anchor() -> dict:
    """The real-money anchor, Decimals for the two dollar figures (VF-8)."""
    raw = dict(_raw()["budgets"]["cac_anchor"])
    raw["cac_usd"] = Decimal(raw["cac_usd"])
    raw["cost_per_free_trial_usd"] = Decimal(raw["cost_per_free_trial_usd"])
    return raw


def threshold(name: str):
    """A threshold by config name; Decimal for string values, int for ints."""
    value = _raw()["thresholds"][name]
    if isinstance(value, list):
        return tuple(Decimal(v) for v in value)
    if isinstance(value, int):
        return value
    return Decimal(value)
