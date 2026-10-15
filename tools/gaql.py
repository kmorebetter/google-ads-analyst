"""Comparing an executable GAQL filter against a normalised scope label (VF-19a).

The contradiction this module resolves. `audit-ad-copy` used to demand that the
canonical filter match "character for character" across the skill header, every
fact `scope` and every manifest `filter`. That is impossible, and a run correctly
reported that it could not satisfy it:

    manifest (executable GAQL)  ad_group_ad.status='ENABLED' ... ad_group_ad.ad.type='RESPONSIVE_SEARCH_AD'
    fact scope (label)          ad_group_ad.status=ENABLED   ... ad.type=RESPONSIVE_SEARCH_AD

The manifest stores the literal query that ran, verbatim, because that is the
audit trail. The fact scope stores the normalised label. VF-19a settles it: the
requirement is **clause-for-clause semantic equality**, never string equality.

**The normaliser, and nothing more than this.**

1. Strip the quotes from string literals.
2. Rewrite `ad_group_ad.ad.type` to `ad.type`.
3. Collapse whitespace, including the spaces around a comparison operator, so
   `status = 'ENABLED'` and `status=ENABLED` are the same clause. This is lexical
   tidying, not a semantic rewrite; it is listed so the normaliser has no
   undocumented behaviour.

Anything else is a real difference. **A missing or extra clause is a hard fail.**

**Temporality is not a scope clause.** `AND segments.date DURING LAST_30_DAYS` is
a pull-specific clause that VF-17 records in the fact's `window`, not its
`scope`, so it is held out of the clause comparison and checked against the
declared temporality instead. `audit-ad-copy` states the same thing: a
pull-specific clause is appended and sits outside the comparison. That carve-out
is narrow on purpose - it matches date and date-range predicates only - because a
loose one would quietly become a hole through which a genuinely extra clause
could pass. `ClauseComparison.extra` still hard-fails on anything non-temporal.

**What may be compared.** Point this at a declared GAQL filter and a canonical
scope label whose clauses are all real predicates - the ad-level pulls against
`scopes.VF5_FULL_FILTER`. Do not point it at `ASSET_PULL_FILTER` or
`ASSET_RANKING_FILTER`: those deliberately carry English annotations ("NO status
filter (spans non-servable ad groups)", "then keyed on ...") which are notes for
a reader, not clauses, and teaching a parser to ignore prose is not a job this
module wants.

Python 3 standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Step 2 of the normaliser. GAQL selects the ad's type through `ad_group_ad`;
#: the label writes it as the ad field it is. The only alias in this account.
FIELD_ALIASES = {
    "ad_group_ad.ad.type": "ad.type",
}

#: A date or date-range predicate. Temporality lives in the fact's `window`
#: (VF-17), so these are held out of the scope comparison. Kept deliberately
#: narrow: named date fields and the two range operators, nothing else.
TEMPORALITY_CLAUSE = re.compile(
    r"^(?:segments\.date|segments\.week|segments\.month|segments\.quarter"
    r"|segments\.year|change_event\.change_date_time|change_status\."
    r"last_change_date_time)\s*(?:DURING|BETWEEN|>=|<=|>|<|=)",
    re.IGNORECASE,
)

_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_AND = re.compile(r"\s+AND\s+", re.IGNORECASE)
_OPERATOR_SPACING = re.compile(r"\s*(!=|>=|<=|=|>|<)\s*")
_WHITESPACE = re.compile(r"\s+")


class FilterMismatch(Exception):
    """A declared GAQL filter and a scope label do not carry the same clauses."""


def strip_quotes(text: str) -> str:
    """Step 1. `status='ENABLED'` -> `status=ENABLED`, `IN ('A','B')` -> `IN (A,B)`."""
    return _QUOTED.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2), text)


def apply_aliases(text: str) -> str:
    """Step 2. Rewrite the GAQL spelling of a field to its label spelling."""
    for gaql_name, label_name in FIELD_ALIASES.items():
        text = re.sub(
            r"(?<![\w.])" + re.escape(gaql_name) + r"(?![\w.])", label_name, text
        )
    return text


def normalise(text: str) -> str:
    """The whole normaliser: strip quotes, apply aliases, tidy whitespace."""
    text = strip_quotes(text)
    text = apply_aliases(text)
    text = _OPERATOR_SPACING.sub(r"\1", text)
    return _WHITESPACE.sub(" ", text).strip()


# `BETWEEN 'a' AND 'b'` carries an AND that is part of the operator, not a
# clause separator. Splitting on it turned one correctly-declared window into two
# clauses and made a matching temporality read as a mismatch. Found 2026-08-20,
# while it was actively aborting two builders against raw/2026-08-20.
_BETWEEN = re.compile(
    r"(\S+\s+BETWEEN\s+'[^']*'\s+AND\s+'[^']*')", re.IGNORECASE
)


def split_literal(text: str) -> list:
    """Split a filter on its top-level `AND`s, keeping each clause **literal**.

    `BETWEEN 'a' AND 'b'` is protected first: its `AND` belongs to the operator.
    Splitting on it produced a phantom second clause and failed a window that
    was declared correctly.

    No normalisation — quotes and spellings survive — because VF-15 wants a
    scope label to carry the literal filter that ran. `clauses()` builds on
    this and normalises for comparison; scope-label builders use this
    directly. (weekly-review carried its own copy of this split while the
    upstream one was buggy; the bug was fixed 2026-08-20 and the copy is
    retired — one split, one home.)

    Splitting on `AND` is otherwise sound for these filters because none nests
    an `OR` or a parenthesised conjunction. If one ever does, this returns a
    clause holding the whole group, which reads as a mismatch rather than
    silently dropping half of it.
    """
    if not text or not text.strip():
        return []
    held, out = [], []
    def _hold(m):
        held.append(m.group(1))
        return f"\x00{len(held) - 1}\x00"
    guarded = _BETWEEN.sub(_hold, text.strip())
    for part in _AND.split(guarded):
        for i, original in enumerate(held):
            part = part.replace(f"\x00{i}\x00", original)
        part = part.strip()
        if part:
            out.append(part)
    return out


def clauses(text: str) -> list:
    """Split a filter into normalised clauses on the top-level `AND`s."""
    out = []
    for part in split_literal(text):
        c = normalise(part)
        if c:
            out.append(c)
    return out


def is_temporality(clause: str) -> bool:
    return bool(TEMPORALITY_CLAUSE.match(clause.strip()))


@dataclass(frozen=True)
class ClauseComparison:
    """The clause-for-clause result. `ok` is the only thing a caller must check."""

    declared: tuple      # every normalised clause in the GAQL
    label: tuple         # every normalised clause in the scope label
    matched: tuple       # clauses present in both
    missing: tuple       # in the label, absent from the GAQL - HARD FAIL
    extra: tuple         # in the GAQL, absent from the label - HARD FAIL
    temporality: tuple   # date clauses, held out of the comparison (VF-17)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.extra

    def describe(self) -> str:
        parts = [
            f"{len(self.matched)} clause(s) matched after normalising "
            f"(VF-19a: semantic equality, not byte equality)"
        ]
        if self.temporality:
            parts.append(
                "temporality clause(s) held out of the scope comparison and "
                f"checked against the window instead: {', '.join(self.temporality)}"
            )
        if self.missing:
            parts.append(f"MISSING from the query: {', '.join(self.missing)}")
        if self.extra:
            parts.append(f"EXTRA in the query: {', '.join(self.extra)}")
        return "; ".join(parts)


def compare(declared_gaql: str, scope_label: str) -> ClauseComparison:
    """Compare an executable GAQL filter against a normalised scope label."""
    declared_all = clauses(declared_gaql)
    temporality = [c for c in declared_all if is_temporality(c)]
    declared = [c for c in declared_all if not is_temporality(c)]
    label = [c for c in clauses(scope_label) if not is_temporality(c)]

    declared_set, label_set = set(declared), set(label)
    return ClauseComparison(
        declared=tuple(declared),
        label=tuple(label),
        matched=tuple(c for c in label if c in declared_set),
        missing=tuple(c for c in label if c not in declared_set),
        extra=tuple(c for c in declared if c not in label_set),
        temporality=tuple(temporality),
    )


def assert_semantic_match(what: str, declared_gaql: str, scope_label: str) -> str:
    """Raise `FilterMismatch` on a missing or extra clause. Return an audit line.

    A spelling difference the normaliser accounts for is not an error and never
    raises. That is the whole point of VF-19a.
    """
    result = compare(declared_gaql, scope_label)
    if result.ok:
        return f"{what}: {result.describe()}"
    lines = [
        f"{what}: the declared query and the scope label do not carry the same "
        "clauses (VF-19a). Semantic equality is required; a spelling difference "
        "the normaliser handles is not an error, a missing or extra clause is.",
        f"  declared GAQL: {declared_gaql}",
        f"  scope label:   {scope_label}",
    ]
    for clause in result.missing:
        lines.append(
            f"  MISSING from the query: {clause!r}. The label claims a filter "
            "the pull did not apply, so every fact under it is mislabelled."
        )
    for clause in result.extra:
        lines.append(
            f"  EXTRA in the query: {clause!r}. The pull narrowed the rows "
            "further than the label says, so the label overstates the scope."
        )
    raise FilterMismatch("\n".join(lines))



_BETWEEN_DATES = re.compile(
    r"BETWEEN\s+'?(\d{4}-\d{2}-\d{2})'?\s+AND\s+'?(\d{4}-\d{2}-\d{2})'?", re.I
)


def _spec_matches(spec: str, clause: str) -> bool:
    """Does a declared temporality spec describe this date predicate?

    Two spellings, and the substring test only ever handled one. A `DURING`
    predicate embeds its token (`LAST_7_DAYS`), so a substring works. A range is
    written `BETWEEN '2026-08-01' AND '2026-08-19'` in GAQL and
    `2026-08-01..2026-08-19` in the manifest, and no substring test can bridge
    that — it failed every correctly-declared range. Found 2026-08-20 by a
    builder that had to re-implement the check locally to get past it.
    """
    spec = spec.strip()
    m = _BETWEEN_DATES.search(clause)
    if m:
        start, end = m.group(1), m.group(2)
        normalised = spec.replace("..", " ").replace("-–", " ").split()
        return [start, end] == [t.strip("'\"") for t in normalised]
    return spec.upper() in clause.upper()


def check_declared_temporality(declared_gaql: str, declared_temporality: str) -> str:
    """Cross-check the manifest's own two fields against each other.

    VF-17 rule 5 says temporality is declared, never inferred. This does not
    infer it - it checks the declaration against the query text that was also
    declared, and disagreement between the two means the manifest is wrong.

    A filter carrying a date predicate must be declared `window:...`. A filter
    carrying none must be declared `current-as-of-...`. This is exactly the case
    field-mask inference got wrong on 2 of 6 files: a `change_event` pull has no
    `segments.*` or `metrics.*` field yet is windowed by
    `change_event.change_date_time`, which the clause test sees and a field mask
    cannot.
    """
    windowed_clauses = [c for c in clauses(declared_gaql) if is_temporality(c)]
    declared = (declared_temporality or "").strip()
    if windowed_clauses and not declared.startswith("window:"):
        raise FilterMismatch(
            f"the declared filter carries a date predicate "
            f"({', '.join(windowed_clauses)}) but the declared temporality is "
            f"{declared_temporality!r}. A windowed pull cannot answer a "
            "current-state question (VF-17)."
        )
    if not windowed_clauses and declared.startswith("window:"):
        raise FilterMismatch(
            f"the declared temporality is {declared_temporality!r} but the "
            "declared filter carries no date predicate. One of the two fields "
            "is wrong, and a fact's window is taken from the temporality."
        )
    if windowed_clauses:
        spec = declared.split(":", 1)[1]
        if spec and not any(_spec_matches(spec, c) for c in windowed_clauses):
            raise FilterMismatch(
                f"the declared temporality is {declared_temporality!r} but the "
                f"date predicate reads {', '.join(windowed_clauses)}. The two "
                "name different ranges."
            )
        return (
            f"declared temporality {declared_temporality!r} agrees with the "
            f"date predicate {', '.join(windowed_clauses)}"
        )
    return (
        f"declared temporality {declared_temporality!r} agrees with the filter, "
        "which carries no date predicate"
    )


__all__ = [
    "ClauseComparison",
    "FilterMismatch",
    "FIELD_ALIASES",
    "TEMPORALITY_CLAUSE",
    "apply_aliases",
    "assert_semantic_match",
    "check_declared_temporality",
    "clauses",
    "compare",
    "is_temporality",
    "normalise",
    "split_literal",
    "strip_quotes",
]
