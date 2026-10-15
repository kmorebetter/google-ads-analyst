"""Reading a saved raw pull, and checking it against the filter it claims.

A facts file is only as trustworthy as its `scope` labels, so every clause of
every declared filter is re-checked against every row before any fact is
written. Checks are tri-state: a row answers true, false, or
unknown-because-the-field-was-not-saved.

  any false      -> BuildError. The label is stale. Write nothing.
  all unknown    -> the clause is recorded as DECLARED, not VERIFIED.
  all true       -> the clause is recorded as VERIFIED.

A right number under a wrong label is the failure VF-15 exists to prevent, so
the false case is a hard stop rather than a warning.

Python 3 standard library only.
"""

from __future__ import annotations

import collections
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import gaql
import scopes as S


class BuildError(Exception):
    """A declared scope did not match the data. Refuse to write a facts file."""


class ProblemLog:
    """Collects validation contradictions so one build reports them all.

    The old behaviour - abort on the first contradiction - cost one full
    pull->build->fail->fix cycle per defect: a raw directory with three bad
    pulls took three builds to diagnose. This collector runs every independent
    validation check, records each contradiction, and raises once at the end.
    Nothing is ever written on any failure; only the *reporting* is batched.

    Two rules keep the old tests and semantics intact:

    - **A file's checks stop at its first contradiction.** A missing file's
      row count is not a second finding, so `run(..., file=name)` skips any
      check for a file already marked broken.
    - **A single contradiction re-raises the original exception**, preserving
      its type (`BuildError` subclass, `ManifestError`, `FilterMismatch`) and
      message exactly. Only two or more are wrapped into one `BuildError`
      listing all of them.
    """

    #: Everything a validation check legitimately raises. Anything else is a
    #: bug in the builder and propagates immediately.
    CATCHES = (BuildError, S.ManifestError, gaql.FilterMismatch)

    def __init__(self) -> None:
        self.problems: list[BaseException] = []
        self._broken: set[str] = set()

    def run(self, fn: Callable, *args, file: "str | None" = None, **kwargs):
        """Run one check; record its contradiction instead of raising.

        Returns the check's value, or None when it failed or when `file` is
        already broken (so dependent checks for that file are skipped).
        """
        if file is not None and file in self._broken:
            return None
        try:
            return fn(*args, **kwargs)
        except self.CATCHES as exc:
            self.record(exc, file=file)
            return None

    def record(self, exc: BaseException, file: "str | None" = None) -> None:
        self.problems.append(exc)
        if file is not None:
            self._broken.add(file)

    def mark_broken(self, file: str) -> None:
        """Skip a file's later checks without adding another problem for it."""
        self._broken.add(file)

    def broken(self, file: str) -> bool:
        return file in self._broken

    def raise_if_any(self, context: str) -> None:
        """Raise the single original exception, or one BuildError listing all."""
        if not self.problems:
            return
        if len(self.problems) == 1:
            raise self.problems[0]
        lines = [
            f"{len(self.problems)} validation contradictions in {context}, "
            "all reported in one pass so one re-pull can fix them together "
            "(each used to abort the build on its own):"
        ]
        for i, exc in enumerate(self.problems, 1):
            kind = type(exc).__name__
            lines.append(f"[{i}] ({kind}) {exc}")
        raise BuildError("\n\n".join(lines))


# --------------------------------------------------------------------------
# Loading and tri-state clause validation
# --------------------------------------------------------------------------


def load_results(raw_dir: Path, filename: str) -> list[dict]:
    path = raw_dir / filename
    if not path.exists():
        raise BuildError(f"{filename}: missing from {raw_dir}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("success", True):
        raise BuildError(f"{filename}: the saved pull records success=false")
    data = payload.get("data") or {}
    results = data.get("results")
    if results is None:
        raise BuildError(
            f"{filename}: no `data.results` array. The pull returned only a "
            "fieldMask, which means the query matched zero rows."
        )
    if not results:
        raise BuildError(f"{filename}: zero rows")
    return results


def field_mask(raw_dir: Path, filename: str) -> str:
    payload = json.loads((raw_dir / filename).read_text(encoding="utf-8"))
    return ((payload.get("data") or {}).get("fieldMask")) or ""


#: A clause predicate returns True (holds), False (contradicted) or
#: None (the field was not saved, so it cannot be checked).
Clause = tuple[str, Callable[[dict], "bool | None"]]


def validate_clauses(filename: str, rows: list[dict], clauses: list[Clause]) -> list[str]:
    """Re-check each declared clause against every row. Raise on contradiction."""
    trail = []
    for text, predicate in clauses:
        answers = [predicate(row) for row in rows]
        false_rows = [i for i, a in enumerate(answers) if a is False]
        if false_rows:
            raise BuildError(
                f"{filename}: declared filter clause {text!r} is FALSE for "
                f"{len(false_rows)} of {len(rows)} rows (first offending row "
                f"index {false_rows[0]}). The scope label in this script is "
                "stale. Fix the declared filter before writing a facts file."
            )
        known = sum(1 for a in answers if a is True)
        if known == 0:
            trail.append(f"DECLARED (field not in saved field mask): {text}")
        elif known == len(rows):
            trail.append(f"VERIFIED on all {len(rows)} rows: {text}")
        else:
            trail.append(
                f"VERIFIED on {known} of {len(rows)} rows, field absent on the "
                f"rest: {text}"
            )
    return trail


def _status_is(container: str, expected: str) -> Callable[[dict], "bool | None"]:
    def check(row: dict) -> "bool | None":
        value = (row.get(container) or {}).get("status")
        return None if value is None else value == expected
    return check


def assert_unique_join_key(filename: str, rows: list[dict]) -> None:
    keys = []
    for row in rows:
        key = (row.get("adGroupAd") or {}).get("resourceName")
        if not key:
            raise BuildError(
                f"{filename}: a row has no adGroupAd.resourceName. That is the "
                "only safe join key; add it to the SELECT and re-pull."
            )
        keys.append(key)
    dupes = [k for k, n in collections.Counter(keys).items() if n > 1]
    if dupes:
        raise BuildError(
            f"{filename}: {len(dupes)} duplicate adGroupAd.resourceName values "
            f"(e.g. {dupes[0]}). The response is segmented; aggregate per "
            "resource name before summing, or the totals double count."
        )


def ad_key(row: dict) -> str:
    """The only safe ad-level join key. See the module docstring, point 5."""
    return row["adGroupAd"]["resourceName"]


#: `(ad_group.id, ad.id)`, read from the resource name when the pull did not
#: select the id fields. See scopes.ag_ad_ids.
ag_ad = S.ag_ad_ids


# --------------------------------------------------------------------------
# Which filter did the inventory pull actually use?
# --------------------------------------------------------------------------


@dataclass
class InventoryScope:
    """What the saved inventory rows can and cannot prove about ad group status.

    The Aug-10 pull did not filter `ad_group.status`, so it contains PAUSED and
    REMOVED ad groups and both the servable count and the bug count are
    derivable. A corrected pull filters it, and may not even select the field;
    then the servable count is derivable but the bug count is not, and saying
    otherwise would relabel a filtered number as unfiltered.
    """

    rows: list[dict]
    servable: list[dict]
    non_servable: list[dict]
    status_selected: bool
    unfiltered_proven: bool
    servable_provenance: str
    notes: list[str] = field(default_factory=list)

    @property
    def servable_keys(self) -> set:
        return {ad_key(r) for r in self.servable}

    @property
    def scope(self) -> str:
        if self.status_selected:
            return S.VF5_FULL_FILTER
        return f"{S.VF5_FULL_FILTER} [{self.servable_provenance}]"


def read_inventory_scope(rows: list[dict]) -> InventoryScope:
    statuses = [(row.get("adGroup") or {}).get("status") for row in rows]
    selected = any(s is not None for s in statuses)
    if not selected:
        return InventoryScope(
            rows=rows,
            servable=list(rows),
            non_servable=[],
            status_selected=False,
            unfiltered_proven=False,
            servable_provenance=(
                "ad_group.status is DECLARED, not verified: the pull did not "
                "select the field, so the servable count rests on the query's "
                "own WHERE clause"
            ),
            notes=[
                "ad_group.status was not selected by the inventory pull. Add it "
                "to the SELECT so the servable scope can be verified from the "
                "saved JSON rather than trusted."
            ],
        )
    unknown = [s for s in statuses if s is None]
    if unknown:
        raise BuildError(
            f"{S.INVENTORY}: ad_group.status is present on some rows and absent "
            f"on {len(unknown)}. A mixed field mask cannot be scoped honestly."
        )
    unexpected = sorted(
        {s for s in statuses if s not in ("ENABLED", "PAUSED", "REMOVED")}
    )
    if unexpected:
        raise BuildError(
            f"{S.INVENTORY}: unexpected ad_group.status values {unexpected}. The "
            "servable/non-servable split would silently drop those rows."
        )
    servable = [r for r in rows if (r.get("adGroup") or {}).get("status") == "ENABLED"]
    servable_ids = {id(r) for r in servable}
    non_servable = [r for r in rows if id(r) not in servable_ids]
    return InventoryScope(
        rows=rows,
        servable=servable,
        non_servable=non_servable,
        status_selected=True,
        unfiltered_proven=bool(non_servable),
        servable_provenance="ad_group.status=ENABLED VERIFIED on every counted row",
        notes=(
            []
            if non_servable
            else [
                "Every row is in an ENABLED ad group. That is consistent with a "
                "pull that filtered ad_group.status and with one that did not "
                "but had nothing to exclude. The unfiltered-scope counts are "
                "therefore emitted as null."
            ]
        ),
    )

def outputs_root(start: "Path | None" = None) -> "Path":
    """Find the `google-ads/outputs` directory from wherever this file lives.

    Layout-independent on purpose. This pack lives in three places with three
    different shapes:

      standalone repo      tools/            outputs at ./google-ads/outputs
      consolidation repo   google-ads/tools/ outputs at ./outputs (one level up)
      deployed copy        .claude/skills/   outputs elsewhere again

    Every caller used to compute `Path(__file__).parent.parent / "google-ads" /
    "outputs"`, which is only right in the first shape. In the monorepo it
    resolved to `google-ads/google-ads/outputs` and every data-backed test
    errored with a path that had the segment twice. Found 2026-08-20 while
    porting.

    Walks up from `start` (default: this file) and returns the first match of
    either `<dir>/google-ads/outputs` or `<dir>/outputs` where `<dir>` is itself
    named `google-ads`. Raises rather than returning a guess, because a silently
    wrong data directory reads as "no data" and skips the tests that matter.
    """
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if not parent.is_dir():
            continue
        candidate = parent / "google-ads" / "outputs"
        if candidate.is_dir():
            return candidate
        if parent.name == "google-ads" and (parent / "outputs").is_dir():
            return parent / "outputs"
    raise FileNotFoundError(
        "could not locate a google-ads/outputs directory above "
        f"{here}. Runtime pulls are gitignored, so a fresh clone has none - "
        "callers should treat that as 'skip the data-backed tests', never as "
        "'the data is empty'."
    )


def raw_dir(date: str, start: "Path | None" = None) -> "Path":
    """`outputs_root()/raw/<date>`, resolved the layout-independent way."""
    return outputs_root(start) / "raw" / date
