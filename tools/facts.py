"""Facts-file harness for the Google Ads skills pack (VERIFICATION-RULES VF-15).

Reports must stop doing arithmetic in prose. A build script reads the saved raw
JSON and writes a facts file. Every figure in a report must trace to a fact id.

The load-bearing part of this module is the comparison guard. Two facts may only
be compared, differenced or summed if `unit`, `scope`, `window` and `basis` all
match. Anything else raises. This exists because of one specific, expensive
error: on 2026-08-10 a $49,210.77 figure that included conquesting insertion ad
groups was compared against an 2026-08-05 figure of $48,323.11 that excluded
them, and the difference was reported as growth. Both numbers were individually
correct. Only a scope label catches that.

Python 3 standard library only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Sequence

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: The four attributes that must match before two facts may be related.
GUARDED_ATTRS = ("unit", "scope", "window", "basis")

#: Legal values for `basis` (VF-15). `8-action` is the 8 conversion actions of
#: VF-1. `broad-goal-set` is the ~20-action account goal set, which is all
#: `ad_group_ad_asset_view` can ever report on (VF-3).
LEGAL_BASES = ("8-action", "broad-goal-set", "n/a")

#: `metrics.search_impression_share` returns this sentinel to mean "< 10%".
#: A value computed from it is a floor, not a value (VF-11).
SEARCH_IMPRESSION_SHARE_SENTINEL = 0.0999

#: Units whose facts are credited to every asset present in a serve, so they
#: must never be summed and never be compared across ads (VF-4 rules 1 and 2).
#:
#: `serve_credits_within_ad` is itself the result of a controlled within-ad sum,
#: used only as the denominator for a share-of-serve figure. It is not the ad's
#: impressions and summing it any further is meaningless, so it is blocked here
#: too. The builder writes that caveat into the fact's scope string.
#: The two percentage units are here for a different reason: a share of one ad's
#: serve credits and a within-ad CTR are both ratios with an ad-specific
#: denominator, so adding them across ranks or across ads produces a number with
#: no referent at all.
NON_ADDITIVE_UNITS = frozenset(
    {
        "impressions_asset_credited",
        "clicks_asset_credited",
        "serve_credits_within_ad",
        "percent_of_ad_serve_credits",
        "percent_ctr_within_ad",
    }
)

#: VF-17 rule 4. A current-state fact's window reads `current-as-of-<date>`, so
#: the guard blocks comparing it against a date-range fact.
CURRENT_STATE_WINDOW_RE = re.compile(r"^current-as-of-\d{4}-\d{2}-\d{2}$")


def current_state_window(as_of: str) -> str:
    """The only legal window for a fact that answers "is X true right now"."""
    window = f"current-as-of-{as_of}"
    if not CURRENT_STATE_WINDOW_RE.match(window):
        raise FactsError(
            f"{as_of!r} is not a YYYY-MM-DD date, so {window!r} is not a legal "
            "current-state window (VF-17 rule 4)"
        )
    return window


def is_current_state_window(window: str) -> bool:
    return bool(CURRENT_STATE_WINDOW_RE.match(window or ""))

MICROS_PER_UNIT = Decimal(1_000_000)

# --------------------------------------------------------------------------
# Percentage conventions
# --------------------------------------------------------------------------
#: A share can be stored two ways and printed one way. `52.0%` in a report may
#: be backed by a fact holding `52.0` or a fact holding `0.52`, and the two are
#: not distinguishable from the value alone: `0.52` is also a perfectly ordinary
#: 0.52% figure. The checker must therefore read the convention off the fact's
#: `unit`, never guess it from the magnitude, and never try both.
#:
#: Trying both is not a harmless convenience. It doubles every percentage's
#: matching surface, and a doubled surface in a 6,448-fact file holding 1,504
#: share and CTR percentages is precisely the coincidence that let a fabricated
#: `10.6` and `9.6` pass on 2026-08-19.
#:
#: Matched as substrings, because the pack's real unit names are compounds
#: (`percent_of_ad_serve_credits`, `percent_ctr_within_ad`), not bare words.
PERCENT_UNIT_MARKERS = ("percent", "pct")
RATIO_UNIT_MARKERS = ("ratio", "fraction")

#: The two conventions, named so an error message and a test can say which.
PERCENT_SCALE = "percent"   # the value runs 0-100, e.g. 52.0 means 52%
RATIO_SCALE = "ratio"       # the value runs 0-1,   e.g. 0.52 means 52%

#: Deliberately NOT classified: `share`, `rate`, `ctr`, `proportion`. Each is
#: used at both scales in the wild - Google returns `search_impression_share`
#: as 0-1 and prints CTR as 0-100 - so a unit named only `share` genuinely does
#: not say which. Guessing there is the failure this function exists to stop, so
#: it returns None and the caller must refuse the comparison out loud.


def percentage_convention(unit: str | None) -> str | None:
    """Which scale a fact's value uses, or None when the unit does not say.

    Returns `PERCENT_SCALE` (0-100), `RATIO_SCALE` (0-1), or None. A unit that
    matches both families is None too: `percent_ratio` declares nothing.

    >>> percentage_convention("percent_of_ad_serve_credits")
    'percent'
    >>> percentage_convention("ratio")
    'ratio'
    >>> percentage_convention("share") is None
    True
    >>> percentage_convention("percent_ratio") is None
    True
    """
    text = (unit or "").lower()
    is_percent = any(marker in text for marker in PERCENT_UNIT_MARKERS)
    is_ratio = any(marker in text for marker in RATIO_UNIT_MARKERS)
    if is_percent and is_ratio:
        return None
    if is_percent:
        return PERCENT_SCALE
    if is_ratio:
        return RATIO_SCALE
    return None


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class FactsError(Exception):
    """Base class for every error this module raises."""


class UnknownFactError(FactsError):
    """A fact id was requested that is not in the set."""


class DuplicateFactError(FactsError):
    """A fact id was added twice."""


class FactMismatchError(FactsError):
    """Two facts differ on a guarded attribute, so they may not be related."""


class NullFactValueError(FactsError):
    """A fact deliberately carries no value, so it cannot be used in arithmetic."""


class NonAdditiveFactError(FactsError):
    """A fact's unit forbids summing it (VF-4)."""


class SentinelValueError(FactsError):
    """A figure was derived from the < 10% impression-share sentinel (VF-11)."""


class MissingJustificationError(FactsError):
    """compare_unsafe() was called without a written justification."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def micros_to_currency(micros: int | str | None) -> Decimal | None:
    """Convert a Google Ads micros field to a currency amount.

    Returns full precision as a Decimal. Round only when printing. The account
    is CAD; this helper does not know or check the currency.

    >>> micros_to_currency("620000000005")
    Decimal('48213.904417')
    """
    if micros is None:
        return None
    return Decimal(str(micros)) / MICROS_PER_UNIT


def money(value: Decimal | float | int | None) -> Decimal | None:
    """Quantize to 2 decimal places for printing. Half-up, like a report."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Google clamps an impression-share field at both ends. `0.0999` means "< 10%"
# and is observed constantly in this account. `0.9001` means "> 90%" and is
# documented API behaviour but has NOT been observed here — checked the
# 2026-08-20 seven-day pull across rank-lost, budget-lost and absolute-top and
# found none. Guarded anyway, because the cost of guarding is nothing and the
# cost of missing it is a ratio printed as a point estimate. Labelled honestly:
# one sentinel is measured, the other is anticipated.
SEARCH_IMPRESSION_SHARE_HIGH_SENTINEL = 0.9001


def is_sentinel_share(share: float | None) -> bool:
    """True if `share` is either clamp Google returns (VF-11).

    Both ends, not just the low one. A share pinned at a clamp is a bound, not a
    value, whichever end it sits at, and dividing by it produces a figure that
    looks precise and is not.
    """
    if share is None:
        return False
    v = float(share)
    return (abs(v - SEARCH_IMPRESSION_SHARE_SENTINEL) < 1e-9
            or abs(v - SEARCH_IMPRESSION_SHARE_HIGH_SENTINEL) < 1e-9)


def available_impressions(impressions: int, search_impression_share: float | None):
    """available_impressions = impressions / search_impression_share (VF-11).

    Refuses when the share is missing or is the 0.0999 sentinel. A value
    computed off the sentinel is a floor, not a value, and must never be
    printed as a point estimate.
    """
    if search_impression_share is None:
        raise SentinelValueError(
            "search_impression_share is null, so available_impressions cannot "
            "be computed. Suppress the figure and state why (VF-11)."
        )
    if is_sentinel_share(search_impression_share):
        raise SentinelValueError(
            "search_impression_share is the "
            f"{SEARCH_IMPRESSION_SHARE_SENTINEL} sentinel, meaning '< 10%%'. "
            "Any available_impressions computed from it is a floor, not a "
            "value. Suppress the figure and state why (VF-11)."
        )
    share = float(search_impression_share)
    if share <= 0:
        raise SentinelValueError(
            f"search_impression_share is {share}, which cannot be a divisor."
        )
    return float(impressions) / share


def _numeric(value: Any) -> Decimal:
    """Coerce a fact value to Decimal for arithmetic, or raise."""
    if value is None:
        raise NullFactValueError(
            "fact value is null (deliberately unavailable), so it cannot be "
            "used in arithmetic"
        )
    if isinstance(value, bool):
        raise FactsError("fact value is a bool, which is not a figure")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    raise FactsError(f"fact value {value!r} is not numeric")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        # A true integer stays an integer; anything with a fractional exponent
        # (every micros-derived currency amount) stays a JSON float, so a $0.00
        # spend serialises as 0.0 and reads back as currency, not a count.
        if obj == obj.to_integral_value() and obj.as_tuple().exponent >= 0:
            return int(obj)
        return float(obj)
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


# --------------------------------------------------------------------------
# Fact
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    """One row of a facts file. Fields are exactly those VF-15 specifies.

    id      dotted identifier a report cites, e.g. `rsa.servable.count`
    value   the figure, or None when deliberately unavailable
    unit    count / currency_cad / impressions / conversions / ratio / text ...
    scope   MANDATORY. The literal filter used. Never a paraphrase.
    window  the date range, or "n/a"
    source  the raw JSON filename the fact was computed from
    basis   "8-action", "broad-goal-set" or "n/a" (VF-15)
    """

    id: str
    value: Any
    unit: str
    scope: str
    window: str
    source: str
    basis: str

    def __post_init__(self) -> None:
        if not self.id:
            raise FactsError("fact id is required")
        if not self.unit:
            raise FactsError(f"{self.id}: unit is required")
        if not self.scope:
            raise FactsError(
                f"{self.id}: scope is mandatory and must be the literal filter "
                "used (VF-15)"
            )
        if not self.window:
            raise FactsError(f"{self.id}: window is required (use 'n/a')")
        if not self.source:
            raise FactsError(f"{self.id}: source is required")
        if self.basis not in LEGAL_BASES:
            raise FactsError(
                f"{self.id}: basis {self.basis!r} is not one of {LEGAL_BASES}"
            )

    @property
    def is_null(self) -> bool:
        return self.value is None

    def as_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        return (
            f"{self.id} = {self.value} {self.unit} "
            f"[scope: {self.scope}] [window: {self.window}] "
            f"[basis: {self.basis}] [source: {self.source}]"
        )


@dataclass(frozen=True)
class Comparison:
    """The result of a permitted comparison."""

    id_a: str
    id_b: str
    value_a: Any
    value_b: Any
    delta: Any  # value_a - value_b
    pct_change: Any  # (a - b) / b * 100, or None when b == 0
    unit: str
    scope: str
    window: str
    basis: str
    justification: str | None = None
    mismatches: tuple = ()

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# FactSet
# --------------------------------------------------------------------------


class FactSet:
    """An ordered collection of facts, plus the guard that relates them."""

    def __init__(self, meta: dict | None = None) -> None:
        self._facts: dict[str, Fact] = {}
        self.meta: dict = dict(meta or {})
        self._unsafe: list[dict] = []

    # -- building ----------------------------------------------------------

    def add(
        self,
        id: str,
        value: Any,
        unit: str,
        scope: str,
        window: str,
        source: str,
        basis: str,
    ) -> Fact:
        """Add one fact. Refuses a duplicate id."""
        if id in self._facts:
            raise DuplicateFactError(
                f"fact id {id!r} added twice. Existing: "
                f"{self._facts[id].describe()}"
            )
        fact = Fact(
            id=id,
            value=value,
            unit=unit,
            scope=scope,
            window=window,
            source=source,
            basis=basis,
        )
        self._facts[id] = fact
        return fact

    def add_fact(self, fact: Fact) -> Fact:
        if fact.id in self._facts:
            raise DuplicateFactError(f"fact id {fact.id!r} added twice")
        self._facts[fact.id] = fact
        return fact

    # -- reading -----------------------------------------------------------

    def get(self, id: str) -> Fact:
        try:
            return self._facts[id]
        except KeyError:
            known = ", ".join(sorted(self._facts)) or "<none>"
            raise UnknownFactError(
                f"no fact with id {id!r}. Known ids: {known}"
            ) from None

    def value(self, id: str) -> Any:
        return self.get(id).value

    def ids(self) -> list[str]:
        return list(self._facts)

    def facts(self) -> list[Fact]:
        return list(self._facts.values())

    @property
    def unsafe_comparisons(self) -> list[dict]:
        return list(self._unsafe)

    def __len__(self) -> int:
        return len(self._facts)

    def __contains__(self, id: object) -> bool:
        return id in self._facts

    def __iter__(self):
        return iter(self._facts.values())

    # -- the guard ---------------------------------------------------------

    def mismatches(self, id_a: str, id_b: str) -> list[tuple[str, Any, Any]]:
        """Return the guarded attributes on which two facts differ."""
        a, b = self.get(id_a), self.get(id_b)
        out = []
        for attr in GUARDED_ATTRS:
            va, vb = getattr(a, attr), getattr(b, attr)
            if va != vb:
                out.append((attr, va, vb))
        return out

    def assert_comparable(self, id_a: str, id_b: str) -> None:
        """Raise FactMismatchError unless the two facts may be related.

        The error names every attribute that mismatched and shows both values,
        because "these are not comparable" without saying why is what let the
        Aug-10 error through.
        """
        bad = self.mismatches(id_a, id_b)
        if not bad:
            return
        a, b = self.get(id_a), self.get(id_b)
        lines = [
            f"REFUSED: {id_a} and {id_b} may not be compared, differenced or "
            f"summed. {len(bad)} guarded attribute(s) mismatched (VF-15):"
        ]
        for attr, va, vb in bad:
            lines.append(f"  {attr} mismatch")
            lines.append(f"    {id_a}.{attr} = {va!r}")
            lines.append(f"    {id_b}.{attr} = {vb!r}")
            if attr == "window" and (
                is_current_state_window(str(va)) != is_current_state_window(str(vb))
            ):
                lines.append(
                    "    One of these is a current-state fact and the other is "
                    "a windowed metric. A windowed metric cannot answer a "
                    "current-state question (VF-17). This is the check that the "
                    "removed GDRP headline defeated."
                )
        lines.append(
            "Two facts may only be related if unit, scope, window and basis "
            "all match. Fix the query so both figures share a scope, or state "
            "both figures separately and never subtract them."
        )
        lines.append(f"  a: {a.describe()}")
        lines.append(f"  b: {b.describe()}")
        raise FactMismatchError("\n".join(lines))

    def compare(self, id_a: str, id_b: str) -> Comparison:
        """Compare two facts. Refuses unless all four guarded attrs match."""
        self.assert_comparable(id_a, id_b)
        a, b = self.get(id_a), self.get(id_b)
        va, vb = _numeric(a.value), _numeric(b.value)
        delta = va - vb
        pct = None if vb == 0 else (delta / vb) * Decimal(100)
        return Comparison(
            id_a=id_a,
            id_b=id_b,
            value_a=a.value,
            value_b=b.value,
            delta=delta,
            pct_change=pct,
            unit=a.unit,
            scope=a.scope,
            window=a.window,
            basis=a.basis,
        )

    def diff(self, id_a: str, id_b: str):
        """value(id_a) - value(id_b). Refuses on any guarded mismatch."""
        return self.compare(id_a, id_b).delta

    def sum(self, ids: Sequence[str]):
        """Sum facts. Refuses unless every pair shares all four guarded attrs.

        Also refuses any unit in NON_ADDITIVE_UNITS: Google credits every asset
        present in a serve with the whole ad's metrics, so summing asset rows
        inflates the total (VF-4 rule 1, measured at ~3.4x).
        """
        ids = list(ids)
        if not ids:
            raise FactsError("sum() needs at least one fact id")
        first = ids[0]
        for other in ids[1:]:
            self.assert_comparable(first, other)
        for id in ids:
            unit = self.get(id).unit
            if unit in NON_ADDITIVE_UNITS:
                raise NonAdditiveFactError(
                    f"REFUSED: {id} has unit {unit!r}, which must never be "
                    "summed. Google credits every asset present in a serve "
                    "with the whole ad's metrics, so the sum overstates the "
                    "truth (VF-4 rule 1). Rank within one ad instead."
                )
        total = Decimal(0)
        for id in ids:
            total += _numeric(self.get(id).value)
        return total

    def compare_unsafe(
        self, id_a: str, id_b: str, justification: str
    ) -> Comparison:
        """Compare two facts that fail the guard, on the record.

        `justification` is required, must be substantive, and is written into
        the facts file under `unsafe_comparisons` so the override is visible to
        anyone reading the output. There is no way to override the guard
        without leaving a trace.
        """
        if not isinstance(justification, str) or not justification.strip():
            raise MissingJustificationError(
                "compare_unsafe() requires a written justification explaining "
                "why two facts on different scope/unit/window/basis may be "
                "related. Empty or whitespace is not a justification."
            )
        if len(justification.strip()) < 20:
            raise MissingJustificationError(
                "compare_unsafe() justification is too short to be a real "
                f"justification ({len(justification.strip())} chars, need 20). "
                "Say which attribute differs and why it is safe anyway."
            )
        a, b = self.get(id_a), self.get(id_b)
        bad = tuple(
            {"attribute": attr, "a": str(va), "b": str(vb)}
            for attr, va, vb in self.mismatches(id_a, id_b)
        )
        va, vb = _numeric(a.value), _numeric(b.value)
        delta = va - vb
        pct = None if vb == 0 else (delta / vb) * Decimal(100)
        record = {
            "id_a": id_a,
            "id_b": id_b,
            "value_a": a.value,
            "value_b": b.value,
            "delta": delta,
            "pct_change": pct,
            "justification": justification.strip(),
            "mismatches": list(bad),
            "recorded_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
        }
        self._unsafe.append(record)
        return Comparison(
            id_a=id_a,
            id_b=id_b,
            value_a=a.value,
            value_b=b.value,
            delta=delta,
            pct_change=pct,
            unit=a.unit,
            scope=a.scope,
            window=a.window,
            basis=a.basis,
            justification=justification.strip(),
            mismatches=bad,
        )

    # -- persistence -------------------------------------------------------

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "contract": "VERIFICATION-RULES VF-15",
            "meta": self.meta,
            "fact_count": len(self._facts),
            "facts": [f.as_dict() for f in self._facts.values()],
            "unsafe_comparisons": self._unsafe,
        }
        path.write_text(
            json.dumps(payload, indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "FactSet":
        path = Path(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        fs = cls(meta=payload.get("meta") or {})
        for row in payload.get("facts", []):
            fs.add_fact(
                Fact(
                    id=row["id"],
                    value=row.get("value"),
                    unit=row["unit"],
                    scope=row["scope"],
                    window=row["window"],
                    source=row["source"],
                    basis=row["basis"],
                )
            )
        fs._unsafe = list(payload.get("unsafe_comparisons") or [])
        return fs


# --------------------------------------------------------------------------
# Report checking lives in citations.py, not here
# --------------------------------------------------------------------------
# This module used to carry a second checker, `verify_report`, which asked
# "does this number appear as some fact's value?" That is a search, and a
# search over a large facts file matches by coincidence — a fabricated `10.6`
# and `9.6` passed it on 2026-08-19, and a claimed `99` passed against a true
# `126`. It was removed 2026-08-27 (V2 overhaul, Wave 2). The single
# mechanical checker is `tools/citations.py`: every stated figure must cite a
# fact id, and the cited fact must back it up. Do not reintroduce a
# value-search here.


__all__ = [
    "Fact",
    "FactSet",
    "Comparison",
    "FactsError",
    "FactMismatchError",
    "NullFactValueError",
    "NonAdditiveFactError",
    "SentinelValueError",
    "MissingJustificationError",
    "DuplicateFactError",
    "UnknownFactError",
    "micros_to_currency",
    "money",
    "is_sentinel_share",
    "percentage_convention",
    "available_impressions",
    "GUARDED_ATTRS",
    "LEGAL_BASES",
    "NON_ADDITIVE_UNITS",
    "SEARCH_IMPRESSION_SHARE_SENTINEL",
    "SEARCH_IMPRESSION_SHARE_HIGH_SENTINEL",
    "PERCENT_UNIT_MARKERS",
    "RATIO_UNIT_MARKERS",
    "PERCENT_SCALE",
    "RATIO_SCALE",
    "CURRENT_STATE_WINDOW_RE",
    "current_state_window",
    "is_current_state_window",
]


if __name__ == "__main__":
    import sys

    print(
        "facts.py is a library. Report checking moved to citations.py:\n"
        "  python3 tools/citations.py <report.md> <facts.json>",
        file=sys.stderr,
    )
    raise SystemExit(2)
