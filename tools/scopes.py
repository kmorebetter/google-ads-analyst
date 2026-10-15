"""Scope strings, raw-file names, and manifest reading for the facts harness.

Two jobs, both about labels rather than arithmetic.

`scope` on a fact is the **literal filter** used, word for word (VF-15). All of
those literals live here so the builder reads as logic and the strings can be
checked against `_manifest.json` in one place.

`window` on a fact states temporality, and **temporality is declared by whatever
ran the pull, never inferred** (VF-17 rule 5). Inferring it from the response
field mask got 2 of 6 files wrong on 2026-08-19, because a `change_event` pull
carries no `segments.*` or `metrics.*` field yet is windowed by
`change_event.change_date_time`. So this module reads `_manifest.json` and takes
each file's temporality from it. When a raw dir has no manifest, that fact is
stated in the window string rather than papered over.

Python 3 standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import account_config
import gaql
import pull_specs


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

# --------------------------------------------------------------------------
# Raw file names
# --------------------------------------------------------------------------
# Canonical spellings live in `pull_specs.py` (the single source for which
# pulls exist); these are aliases kept for every existing caller.

MANIFEST = "_manifest.json"
INVENTORY = pull_specs.ADCOPY_INVENTORY
METRICS = pull_specs.ADCOPY_METRICS_30D
CONVERSIONS = pull_specs.ADCOPY_CONVERSIONS_30D
CHANGE_EVENTS = pull_specs.ADCOPY_CHANGE_EVENTS_14D

#: The one canonical name for the `ad_group_ad_asset_view` pull. This is the name
#: the manifest records and the name the skill's step 5 tells the pull to write.
ASSET_VIEW_CANONICAL = pull_specs.ADCOPY_ASSET_SERVES_30D

#: An earlier draft of this harness documented `-asset-view-30d`, and on
#: 2026-08-19 both files sat in the raw directory sharing one md5 while this
#: module accepted either as an alias. Byte-identical copies are harmless right
#: up until they diverge, and then the builder and a human reader silently read
#: different files with no way to tell. The alias survives only so that its
#: presence is detected and compared; it is never silently preferred.
ASSET_VIEW_LEGACY_ALIASES = ("audit-ad-copy-asset-view-30d.json",)

ASSET_VIEW_NAMES = (ASSET_VIEW_CANONICAL,) + ASSET_VIEW_LEGACY_ALIASES
ASSET_VIEW = ASSET_VIEW_CANONICAL

# --------------------------------------------------------------------------
# Scope literals
# --------------------------------------------------------------------------

#: The full VF-5 filter, verbatim from the VF-15 example. Every ad-level query
#: must filter all three statuses. Reports print this beside every count.
VF5_FULL_FILTER = (
    "ad_group_ad.status=ENABLED AND ad_group.status=ENABLED "
    "AND campaign.status=ENABLED AND ad.type=RESPONSIVE_SEARCH_AD "
    "AND campaign.advertising_channel_type=SEARCH"
)

#: The scope of the Aug-10 bug: ad status and campaign status filtered, ad group
#: status never filtered. Named so a future run can show the delta and never
#: silently re-inherit it.
UNFILTERED_AD_GROUP_FILTER = (
    "ad_group_ad.status=ENABLED AND campaign.status=ENABLED "
    "AND ad.type=RESPONSIVE_SEARCH_AD "
    "AND campaign.advertising_channel_type=SEARCH "
    "AND ad_group.status NOT FILTERED (the VF-5 bug)"
)

PAUSED_AG_FILTER = (
    "ad_group_ad.status=ENABLED AND campaign.status=ENABLED "
    "AND ad.type=RESPONSIVE_SEARCH_AD "
    "AND campaign.advertising_channel_type=SEARCH "
    "AND ad_group.status IN (PAUSED, REMOVED)"
)

#: What the asset pull itself filtered. It carries **no status filter**, so it
#: spans non-servable ad groups and one Ad entity appears under several ad
#: groups (VF-4a).
ASSET_PULL_FILTER = (
    "ad_group_ad_asset_view.field_type IN ('HEADLINE','DESCRIPTION') "
    "AND segments.date DURING LAST_30_DAYS "
    "AND NO status filter (spans non-servable ad groups)"
)

#: What the builder narrows the asset pull to before ranking anything: the
#: four-part key of VF-4a, intersected with the servable current inventory of
#: VF-4b.
ASSET_RANKING_FILTER = (
    ASSET_PULL_FILTER
    + ", then keyed on (ad_group.id, ad.id, asset.id, field_type) per VF-4a "
    "and INNER JOINED to the undated current inventory pull ("
    + VF5_FULL_FILTER
    + ") on (ad_group.id, ad.id, headline text) per VF-4b, field_type=HEADLINE"
)

#: VF-3. `ad_group_ad_asset_view` cannot be segmented by
#: `segments.conversion_action_name` at all, so anything from it is on the broad
#: ~20-action goal set and never on the 8.
ASSET_VIEW_BASIS = "broad-goal-set"

#: VF-1. The only conversion actions that may back an `8-action` basis.
#: Read from `account-config.json` - the single source for the list.
EIGHT_ACTIONS = account_config.eight_actions()

ASSET_LABELS = ("BEST", "GOOD", "LEARNING", "LOW", "PENDING", "ABSENT")

#: Said inside every per-ad ranking scope so a rank cannot be lifted out of its
#: ad and set against another ad's.
WITHIN_AD_ONLY = (
    "ranking valid ONLY within this one ad in this one ad group; serve counts "
    "reflect ad count, pinning and ad group volume, not asset quality "
    "(VF-4 rules 2 and 4)"
)

#: Said inside the within-ad denominator's scope. Without it, 40,552 serve
#: credits reads like an impression count, which is the ~3.4x inflation VF-4
#: rule 1 exists to stop.
DENOMINATOR_CAVEAT = (
    "within-ad denominator only: the sum of live headline serve credits for "
    "this one ad. NOT this ad's impressions. Google credits every asset in a "
    "serve with the whole ad's metrics, so this figure is larger than the ad's "
    "real impressions and may only be used as the base of a share-of-serve "
    "percentage inside this ad (VF-4 rule 1)"
)

#: Said inside the removed-asset scope. VF-17 exists because a removed headline
#: was reported as live.
HISTORY_ONLY = (
    "HISTORY ONLY: this asset served during the window but is NOT on the ad "
    "now. It must never be presented as a live problem (VF-4b, VF-17)"
)


def unavailable(reason: str) -> str:
    return f"UNAVAILABLE: {reason}"


# --------------------------------------------------------------------------
# Row keys
# --------------------------------------------------------------------------
# Shared by the builder and by asset_facts, so they live here rather than in
# either one. `adGroupAd.resourceName` is the one field every ad-level pull in
# this pack selects, and it encodes both ids, so it is the reliable source for
# them. `adGroup.id` and `ad.id` are only present when the pull selected them.


def ag_ad_ids(row: dict) -> tuple:
    """`(ad_group.id, ad.id)`, from the ids if selected, else the resource name.

    The unit of comparison for assets is one ad in one ad group (VF-4 rule 4),
    so almost everything is keyed on this pair.
    """
    ag = (row.get("adGroup") or {}).get("id")
    ad = ((row.get("adGroupAd") or {}).get("ad") or {}).get("id")
    if ag and ad:
        return (str(ag), str(ad))
    resource = (row.get("adGroupAd") or {}).get("resourceName") or ""
    tail = resource.rsplit("/", 1)[-1]
    if "~" not in tail:
        raise ValueError(
            f"cannot read (ad_group.id, ad.id) from {resource!r}: the pull "
            "selected neither ad_group.id/ad.id nor a usable "
            "adGroupAd.resourceName"
        )
    ag_id, ad_id = tail.split("~", 1)
    return (ag_id, ad_id)


# --------------------------------------------------------------------------
# Temporality, declared not inferred
# --------------------------------------------------------------------------


class ManifestError(Exception):
    """The manifest and the saved rows disagree."""


def pull_date(raw_dir: Path) -> date | None:
    try:
        return datetime.strptime(raw_dir.name, "%Y-%m-%d").date()
    except ValueError:
        return None


_RELATIVE_WINDOW = re.compile(r"LAST_(\d+)_DAYS", re.IGNORECASE)


def resolve_relative_window(window: str, pulled: "date | None") -> str:
    """Append the resolved date range to a `LAST_N_DAYS` window string.

    `Manifest.window_for()` resolves `LAST_30_DAYS` and nothing else, so
    `LAST_7_DAYS` and `LAST_14_DAYS` come back as the bare declared token.
    That is not wrong, but two facts whose windows read `declared
    window:LAST_7_DAYS` and `declared window:2026-08-06..2026-08-12` are
    harder to read side by side than they need to be.

    Resolving a declared token to dates is arithmetic on a **declaration**,
    not inference from the data, so VF-17 rule 5 permits it. The declared
    string is kept and the resolution appended, never substituted, so the
    audit trail still shows what the pull declared. (Lived in
    build_weekly_review_facts.py as a documented workaround until 2026-08-27;
    this is its shared home.)
    """
    match = _RELATIVE_WINDOW.search(window or "")
    if not match or not pulled:
        return window
    days = int(match.group(1))
    end = pulled - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return f"{window} = {start.isoformat()}..{end.isoformat()}"


def last_30_days_range(pulled: date) -> str:
    """Google's LAST_30_DAYS: the 30 days before the pull date, excluding it."""
    end = pulled - timedelta(days=1)
    start = end - timedelta(days=29)
    return f"{start.isoformat()}..{end.isoformat()}"


class Manifest:
    """`_manifest.json`, or an honest stand-in when a raw dir has none."""

    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir
        self.pulled = pull_date(raw_dir)
        path = raw_dir / MANIFEST
        self.present = path.exists()
        self.payload: dict = (
            json.loads(path.read_text(encoding="utf-8")) if self.present else {}
        )
        self.files: dict = self.payload.get("files") or {}
        self.asset_view_notes: list = []

    # -- names -------------------------------------------------------------

    def asset_view_name(self) -> "str | None":
        """The asset pull's filename. One canonical name, aliases audited.

        Refuses to choose when a legacy alias is present with **different**
        contents from the canonical file: two files disagreeing about the same
        pull is a stale raw directory, and picking one silently is how the
        builder and a reader end up quoting different numbers. Identical copies
        pass, with a trail line naming the duplicate so it can be deleted.
        """
        self.asset_view_notes = []
        canonical = self.raw_dir / ASSET_VIEW_CANONICAL
        aliases = [
            self.raw_dir / name
            for name in ASSET_VIEW_LEGACY_ALIASES
            if (self.raw_dir / name).exists()
        ]

        if canonical.exists():
            for alias in aliases:
                if _digest(alias) == _digest(canonical):
                    self.asset_view_notes.append(
                        f"{alias.name}: a byte-identical duplicate of the "
                        f"canonical {ASSET_VIEW_CANONICAL} (same sha256). Read "
                        "the canonical file and delete the duplicate; two names "
                        "for one pull is a divergence waiting to happen."
                    )
                    continue
                raise ManifestError(
                    f"{ASSET_VIEW_CANONICAL} and {alias.name} both exist in "
                    f"{self.raw_dir.name} and their contents DIFFER "
                    f"(sha256 {_digest(canonical)[:12]} vs "
                    f"{_digest(alias)[:12]}). They are two names for one pull, "
                    "so one of them is stale. Refusing to write a facts file: "
                    "silently preferring either one would let the builder and a "
                    f"reader quote different numbers. Delete the stale copy and "
                    f"keep {ASSET_VIEW_CANONICAL}."
                )
            return ASSET_VIEW_CANONICAL

        if aliases:
            self.asset_view_notes.append(
                f"{aliases[0].name}: read under its LEGACY name because "
                f"{ASSET_VIEW_CANONICAL} is absent. Rename it; the canonical "
                "name is the only one the manifest and the skill use."
            )
            return aliases[0].name
        return None

    # -- temporality -------------------------------------------------------

    def declared_temporality(self, filename: str) -> str | None:
        entry = self.files.get(filename) or {}
        return entry.get("temporality")

    def declared_filter(self, filename: str) -> str | None:
        entry = self.files.get(filename) or {}
        return entry.get("filter")

    def window_for(self, filename: str, *, windowed_fallback: bool) -> str:
        """The `window` string for every fact sourced from `filename`.

        With a manifest, the declared temporality is used verbatim:
        `current-as-of-2026-08-19` for an undated pull, or the declared window.
        Without one, temporality has not been declared and cannot be inferred
        (VF-17 rule 5), so the string says so. A windowed pull still states its
        resolved date range, because dropping a correct date range in favour of
        the word "undeclared" would make the fact less accurate, not more.
        """
        declared = self.declared_temporality(filename)
        if declared:
            if declared.startswith("current-as-of-"):
                return declared
            if declared.startswith("window:"):
                spec = declared.split(":", 1)[1]
                if spec == "LAST_30_DAYS" and self.pulled:
                    return (
                        f"{last_30_days_range(self.pulled)} "
                        f"(declared window:LAST_30_DAYS, pulled "
                        f"{self.pulled.isoformat()})"
                    )
                return (
                    f"declared {declared}"
                    + (f", pulled {self.pulled.isoformat()}" if self.pulled else "")
                )
            return declared
        stamp = self.pulled.isoformat() if self.pulled else "unknown date"
        why = (
            f"{filename} is not declared in {MANIFEST}"
            if self.present
            else f"there is no {MANIFEST} in {self.raw_dir.name}"
        )
        if windowed_fallback and self.pulled:
            return (
                f"{last_30_days_range(self.pulled)} (LAST_30_DAYS as of {stamp}; "
                f"temporality NOT declared because {why} - VF-17 rule 5)"
            )
        return (
            f"temporality NOT declared because {why} (VF-17 rule 5). The pull "
            f"did not record its own query, so this fact is evidence about "
            f"{stamp}, not about today (VF-17 rule 2)."
        )

    def is_current_state(self, filename: str) -> bool:
        declared = self.declared_temporality(filename) or ""
        return declared.startswith("current-as-of-")

    # -- validation --------------------------------------------------------

    def check_row_count(self, filename: str, rows: int) -> str | None:
        """The manifest declares a row count. Disagreement is a hard error."""
        entry = self.files.get(filename)
        if not entry or "rows" not in entry:
            return None
        declared = entry["rows"]
        if declared != rows:
            raise ManifestError(
                f"{filename}: {MANIFEST} declares {declared} rows but the file "
                f"holds {rows}. One of the two is stale; refusing to label facts "
                "from it."
            )
        return f"{filename}: row count {rows} matches {MANIFEST}"

    def verify_filter(self, filename: str, scope_label: str) -> str:
        """Clause-for-clause check of the declared GAQL against a scope label.

        VF-19a. The manifest records executable GAQL; a fact scope records the
        normalised label. Byte equality between them is impossible, so the test
        is semantic: normalise, then diff the clause sets. Raises
        `gaql.FilterMismatch` on a missing or extra clause.
        """
        declared = self.declared_filter(filename)
        if not declared:
            return (
                f"{filename}: no declared filter in {MANIFEST}, so the "
                "clause-for-clause check against the scope label could not run "
                "(VF-19a). The scope label stands DECLARED, not verified."
            )
        return gaql.assert_semantic_match(filename, declared, scope_label)

    def verify_declared_temporality(self, filename: str) -> str:
        """Check the manifest's `filter` and `temporality` fields agree.

        Not an inference (VF-17 rule 5 forbids that). Both fields were declared
        by whatever ran the pull; this only checks they say the same thing.
        """
        declared = self.declared_filter(filename)
        if not declared:
            return (
                f"{filename}: no declared filter in {MANIFEST}, so its declared "
                "temporality could not be cross-checked."
            )
        return (
            f"{filename}: "
            + gaql.check_declared_temporality(
                declared, self.declared_temporality(filename) or ""
            )
        )

    def undeclared_files(self) -> list:
        """Raw pulls on disk that `_manifest.json` says nothing about.

        Temporality and filter are both undeclared for these, so any fact from
        one carries that caveat in its scope rather than a borrowed label.
        """
        if not self.present:
            return []
        skip = {MANIFEST}
        out = []
        for path in sorted(self.raw_dir.glob("*.json")):
            if path.name in skip or path.name.endswith("-facts.json"):
                continue
            if path.name not in self.files:
                out.append(path.name)
        return out

    def audit_line(self, filename: str) -> str:
        if not self.present:
            return f"{filename}: no {MANIFEST}; temporality NOT declared (VF-17 rule 5)"
        entry = self.files.get(filename)
        if not entry:
            return (
                f"{filename}: present on disk but ABSENT from {MANIFEST}; "
                "temporality NOT declared for it (VF-17 rule 5)"
            )
        return (
            f"{filename}: declared temporality {entry.get('temporality')!r}, "
            f"declared filter {entry.get('filter')!r}"
        )
