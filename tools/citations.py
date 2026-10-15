"""Citation-aware report checking — the pack's single mechanical checker.

Why this module exists. The pack's first checker (`facts.verify_report`,
removed in the 2026-08-27 V2 overhaul) asked "does this number appear as some
fact's value?" That is a *search*, and a search over a large facts file finds
a match by coincidence. Demonstrated twice, on the same file:

- 2026-08-19, integers. A 4,865-fact file contains 163 of the 201 integers from
  0 to 200, so a report claiming 99 dead headline slots (real answer 126) passed
  unflagged.
- 2026-08-19, percentages. The file grew to 6,448 rows, 1,504 of them per-rank
  CTR and share percentages. A draft carried four fabricated within-ad share
  percentages on the competitor-campaign rows. Value matching caught two. The
  other two, `10.6` and `9.6`, happened to equal
  `asset_view.adgroup.620000000017.ad.620000000018.headline.rank_3.share` and
  `asset_view.adgroup.620000000019.ad.620000000004.headline.rank_4.share` -
  different ads entirely - and passed **both** checkers clean.

So the claim that value matching "handles percentages reliably" is false. It was
never a property of percentages; it was a property of a small facts file.

The fix is not a cleverer matcher. It is to make the report name the fact it is
quoting, so checking becomes a lookup instead of a search.

    We found 126 dead headline slots [fact: asset_view.dead_slot.total].
    The top 3 take 52.0% [fact: ...headline.top_3.share].

**What must carry a citation.** Every stated figure that is a claim about the
account: bare integers, decimals, and percentages, whether or not a currency
symbol precedes them. What is *not* a claim is defined once, in `claims.py` -
dates, times, years, object ids, code spans, dotted section labels, numerals
hyphenated into words. Do not reintroduce a local copy.

One shape stays exempt on purpose: a **currency-prefixed integer**, `CAD 2,000`
or `CAD 300`. Those are thresholds the report itself chooses and states, not
figures read out of the account, and they have no fact to cite. A currency
*decimal* (`CAD $3,231.15`) is a real account figure and must be cited.

**Percent versus ratio.** A report prints `52.0%`. The fact behind it may hold
`52.0` or `0.52`. Accepting both would double every percentage's matching
surface, which is the coincidence problem again. So the convention is read off
the fact's `unit` via `facts.percentage_convention` - `percent` means 0-100,
`ratio` means 0-1 - and a unit that declares neither (`share`, `rate`, `ctr`) or
both (`percent_ratio`) is refused out loud as `ambiguous_unit`. A figure printed
*without* a `%` compares against the raw fact value only, never against a
scaled one; otherwise the guess creeps back in the other direction.

**Rounding is only allowed where the report showed decimals.** `$3,231.15` may
match a fact holding `3231.148422`, because the report rounded as it printed. A
bare `126` may **not** match a fact holding `126.4`: nothing was rounded, and
letting integers round would give back exactly the slack this module removes.

**Blocks, not lines.** Reports are hard-wrapped, so a citation routinely lands
on the next line from its number:

    The account runs 61 servable RSAs [fact: rsa.servable.count] across 45 ad
    groups [fact: rsa.servable.ad_group.count].

A block is a run of consecutive lines. It ends at a physically blank line or a
fenced code block, both of which are structure. It does **not** end because
masking emptied a line: a line holding nothing but `[fact: ...]` tokens masks to
whitespace, and treating that as blank made a wrapped sentence whose citations
landed on their own line read as uncited - the opposite of what wrapping support
is for.

A markdown table row is a block on its own, because a table is where figures
cluster and pooling a whole table's citations would let a wrong cell borrow a
neighbour's proof. That is not hypothetical either: the fabricated `10.6` and
`9.6` were both table cells.

**Coverage is per figure, not per block.** An earlier version asked only whether
*some* figure on the line matched *some* citation, so this passed:

    126 dead slots across 99 ads [fact: asset_view.dead_slot.total].

The cited fact is 126, that matched, and the wrong 99 rode along uncited. So
every figure must now be accounted for by a citation in its own block.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

import claims
from facts import PERCENT_SCALE, RATIO_SCALE, percentage_convention

#: Two patterns for the same token, and they are not interchangeable.
#: `CITATION` is the shared one `claims.py` uses to MASK a citation before the
#: figure scan, because a fact id carries digits that are not claims. It has no
#: capture group, so `findall` returns whole `[fact: x]` tokens. `_CITATION_ID`
#: is the one that CAPTURES the id, and it is the one `check()` uses.
CITATION = claims.CITATION_TOKEN
_CITATION_ID = re.compile(r"\[fact:\s*([A-Za-z0-9_.\-]+)\s*\]")

# One scanner for every shape of stated figure, so integers, decimals and
# percentages cannot drift apart the way they did when only integers were
# checked.
#
# The `,` in the leading lookbehind matters. Without it, `CAD 2,000` produced a
# phantom claim: the currency rule correctly rejected the whole `2,000`, and the
# engine then matched the `000` after the comma as a figure in its own right. A
# report was told it had stated 000 and could not cite it. A digit run that
# follows a comma is the tail of a thousands-separated number, never a claim of
# its own.
#
# The trailing guard is `(?![\d%])(?!\.\d)`, not `(?![\d.%])`. A trailing `.` is
# usually a sentence full stop - `spend was $48,213.90.` - and rejecting it
# would drop the figure entirely. Only a `.` with a digit after it continues the
# token, and `claims.DOTTED_LABEL` has already blanked `1.2.3` anyway.
STATED_FIGURE = re.compile(
    r"(?<![\w.,])"                            # not mid-token, not a comma tail
    r"(?P<cur>\$|CAD\s*\$?|USD\s*\$?)?"       # optional currency prefix
    r"(?P<sign>-)?"                           # optional sign, no space allowed
    r"(?P<int>\d{1,3}(?:,\d{3})*|\d+)"        # integer part
    r"(?P<frac>\.\d+)?"                       # optional decimal part
    r"\s*(?P<pct>%)?"                         # optional percent sign
    r"(?![\d%])(?!\.\d)"
)


@dataclass(frozen=True)
class StatedFigure:
    """One figure a report states, as written, with the line it was written on."""

    line: int
    text: str          # exactly as printed, e.g. "9.6%" or "48,213.90"
    value: Decimal     # the printed magnitude, signed
    decimals: int      # how many decimal places the report showed
    is_percent: bool

    @property
    def quantum(self) -> Decimal:
        return Decimal(1).scaleb(-self.decimals)


def stated_figures(line_number: int, masked: str) -> list:
    """Every figure claimed on one already-masked line.

    `masked` must come from `claims.masked_lines`, so anything still visible
    here is a claim about the account.
    """
    out: list = []
    for m in STATED_FIGURE.finditer(masked):
        frac = m.group("frac") or ""
        is_percent = m.group("pct") is not None
        # A currency-prefixed integer is a threshold the report chose, not an
        # account figure. See the module docstring.
        if m.group("cur") and not frac and not is_percent:
            continue
        text = m.group(0).strip()
        digits = m.group("int").replace(",", "") + frac
        value = Decimal(digits)
        if m.group("sign"):
            value = -value
        out.append(
            StatedFigure(
                line=line_number,
                text=text,
                value=value,
                decimals=len(frac) - 1 if frac else 0,
                is_percent=is_percent,
            )
        )
    return out


@dataclass(frozen=True)
class CitationProblem:
    line: int
    kind: str          # "uncited" | "unknown_id" | "value_mismatch" | "ambiguous_unit"
                       # | "undeclared_pull_unnamed" | "append_not_made"
                       # The last two are file-level, not figure-level, and
                       # carry line 0. See `check_consistency`.
    detail: str
    text: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.kind} — {self.detail}\n    {self.text.strip()}"


def _load(facts_path: str) -> dict:
    with open(facts_path, encoding="utf-8") as handle:
        raw = json.load(handle)
    facts = raw.get("facts", raw) if isinstance(raw, dict) else raw
    rows = facts.values() if isinstance(facts, dict) else facts
    return {r["id"]: r for r in rows}


def _matches(figure: StatedFigure, expected: Decimal) -> bool:
    """Does the printed figure equal `expected`, allowing for what it rounded?

    Exact equality always counts. Rounding is allowed **only** when the report
    showed decimals: `$3,231.15` may stand for `3231.148422`, but a bare `126`
    must not stand for `126.4`.
    """
    if figure.value == expected:
        return True
    if figure.decimals == 0:
        return False
    quantum = figure.quantum
    if figure.value == expected.quantize(quantum, rounding=ROUND_HALF_UP):
        return True
    return abs(figure.value - expected) <= Decimal("0.005") * quantum


def _blocks(entries: list) -> list:
    """Group `claims.MaskedLine` entries into claim blocks.

    A block ends at structure, never at masking. A physically blank line ends
    it; so does a fenced code block. A line that merely masked to whitespace -
    a line holding only `[fact: ...]` tokens, say - keeps the block open, so a
    wrapped sentence keeps its citations. A markdown table row is a block on
    its own.
    """
    out, current = [], []
    for entry in entries:
        if entry.in_fence or not entry.raw.strip():
            if current:
                out.append(current)
                current = []
            continue
        if entry.raw.lstrip().startswith("|"):
            if current:
                out.append(current)
                current = []
            out.append([entry])
            continue
        current.append(entry)
    if current:
        out.append(current)
    return out


def check(report_path: str, facts_path: str) -> list[CitationProblem]:
    """Every stated figure must cite a fact id, and the fact must back it up.

    Integers, decimals and percentages alike. See the module docstring for what
    is exempt and why.
    """
    idx = _load(facts_path)
    problems: list[CitationProblem] = []

    with open(report_path, encoding="utf-8") as handle:
        report = handle.read()

    for block in _blocks(claims.masked_lines(report)):
        text = "\n".join(entry.raw for entry in block)
        first_line = block[0].number

        stated = [
            figure
            for entry in block
            for figure in stated_figures(entry.number, entry.masked)
        ]
        if not stated:
            continue

        # Citations are read off the RAW lines: the masker blanks them, because
        # a fact id carries digits that are not claims.
        cited = _CITATION_ID.findall("\n".join(entry.raw for entry in block))
        if not cited:
            shown = ", ".join(figure.text for figure in stated)
            problems.append(CitationProblem(
                first_line, "uncited",
                f"figure claim(s) {shown} with no [fact: id]", text))
            continue

        # Resolve each cited id once. An unknown id or a null fact is a problem
        # in its own right, and neither can vouch for any figure.
        #
        # `plain` backs a figure printed without a `%`. `scaled` backs one
        # printed with a `%`, and only exists when the fact's unit declares
        # which scale it uses. `undeclared` is every cited fact whose unit says
        # neither, kept so the refusal can name it.
        plain: dict = {}
        scaled: dict = {}
        undeclared: dict = {}
        for fid in cited:
            if fid not in idx:
                problems.append(CitationProblem(
                    first_line, "unknown_id",
                    f"[fact: {fid}] is not in the facts file", text))
                continue
            row = idx[fid]
            val = row.get("value")
            if val is None:
                problems.append(CitationProblem(
                    first_line, "value_mismatch",
                    f"[fact: {fid}] is a null fact - it cannot support a stated "
                    "number", text))
                continue
            if isinstance(val, bool):
                problems.append(CitationProblem(
                    first_line, "value_mismatch",
                    f"[fact: {fid}] = {val!r}, which is not a number", text))
                continue
            try:
                number = Decimal(str(val))
            except Exception:
                # A text fact (a headline, a label) can never back a figure.
                problems.append(CitationProblem(
                    first_line, "value_mismatch",
                    f"[fact: {fid}] = {val!r}, which is not a number", text))
                continue
            unit = row.get("unit") or ""
            plain[fid] = number
            convention = percentage_convention(unit)
            if convention == PERCENT_SCALE:
                scaled[fid] = (number, unit)
            elif convention == RATIO_SCALE:
                scaled[fid] = (number * 100, unit)
            else:
                undeclared[fid] = unit

        # Every figure must be vouched for by one of this block's citations.
        for figure in stated:
            if figure.is_percent:
                if any(_matches(figure, value) for value, _ in scaled.values()):
                    continue
                if scaled:
                    shown = ", ".join(
                        f"{fid} = {value} (unit {unit!r})"
                        for fid, (value, unit) in scaled.items()
                    )
                    problems.append(CitationProblem(
                        figure.line, "value_mismatch",
                        f"the block states {figure.text}, but on its own unit's "
                        f"scale the fact(s) it cites are {shown}", text))
                elif undeclared:
                    shown = ", ".join(
                        f"{fid} (unit {unit!r})"
                        for fid, unit in undeclared.items()
                    )
                    problems.append(CitationProblem(
                        figure.line, "ambiguous_unit",
                        f"the block states {figure.text}, but {shown} declare(s) "
                        f"neither a {PERCENT_SCALE} (0-100) nor a "
                        f"{RATIO_SCALE} (0-1) scale, so this percentage cannot "
                        "be checked against any of them. Cite a fact whose unit "
                        "names its scale, or fix the unit on the fact you meant.",
                        text))
                else:
                    problems.append(CitationProblem(
                        figure.line, "uncited",
                        f"figure claim {figure.text} is not supported by any "
                        "usable citation in this block", text))
                continue

            if any(_matches(figure, value) for value in plain.values()):
                continue
            if plain:
                shown = ", ".join(
                    f"{fid} = {value}" for fid, value in plain.items()
                )
                problems.append(CitationProblem(
                    figure.line, "value_mismatch",
                    f"the block states {figure.text}, but the fact(s) it cites "
                    f"are {shown}", text))
            else:
                problems.append(CitationProblem(
                    figure.line, "uncited",
                    f"figure claim {figure.text} is not supported by any usable "
                    "citation in this block", text))
    return problems


# --------------------------------------------------------------------------
# Report/metadata self-consistency. Both checks below exist because the
# 2026-08-28 live run's Tier 2 pass caught them BY HAND, as HIGH findings, on
# a draft that `check()` had already passed clean. `check()` asks whether each
# figure is backed by a fact; neither of these is a figure, so neither was in
# its reach. Per the V2 rule that every checkable rule lives in code rather
# than in a reviewer's attention, they are checkable, so they live here.
# --------------------------------------------------------------------------

#: The verbs a report uses when it claims it wrote to the recommendations log.
_APPEND_CLAIM = re.compile(
    r"\b(append(?:ed|s)?|logg(?:ed|s)|added|writ(?:ten|es))\b", re.I)

LOG_NAME = "recommendations-log.csv"


def _meta(facts_path: str) -> dict:
    with open(facts_path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return raw.get("meta", {}) if isinstance(raw, dict) else {}


def check_consistency(report_path: str, facts_path: str,
                      log_path: "str | None" = None) -> list:
    """Two things the report asserts that the artifacts can confirm or refute.

    **Undeclared pulls must be named.** `meta.undeclared_pulls` lists every
    pull that ran outside the manifest's declaration. On 2026-08-28 the facts
    file named one (`conversion-integrity-check.json`) while the report's footer
    said "undeclared in _manifest.json: none" - a direct contradiction between
    two artifacts of the same run, and the sort a reader has no way to catch.
    The check does not parse the footer's prose, which would be brittle; it
    requires only that each undeclared pull's filename appear *somewhere* in
    the report. A run that names the file cannot also be silently claiming it
    away, and the reader gets the string to search for.

    **A claimed append must have happened.** The same run's draft stated its
    recommendations had been appended to the log when they had not been. If
    the report claims an append and `log_path` is given, the log must hold at
    least one row dated `meta.pulled`.

    `log_path` is opt-in: on a machine without the outputs directory - a dev
    box checking a report someone mailed over - there is nothing to verify
    against, and a missing log is silence, never a finding.
    """
    with open(report_path, encoding="utf-8") as handle:
        report = handle.read()
    meta = _meta(facts_path)
    problems = []

    for name in meta.get("undeclared_pulls") or []:
        if name not in report:
            problems.append(CitationProblem(
                0, "undeclared_pull_unnamed",
                f"the facts file's meta.undeclared_pulls lists {name!r}, but "
                "the report never names it. An undeclared pull has to be "
                "disclosed in the report that rests on it - state the file and "
                "why it is outside the manifest, rather than reporting none.",
                f"meta.undeclared_pulls = {meta.get('undeclared_pulls')!r}"))

    if log_path is None:
        return problems
    claim = next((ln for ln in report.splitlines()
                  if LOG_NAME in ln and _APPEND_CLAIM.search(ln)), None)
    if claim is None:
        return problems
    pulled = meta.get("pulled") or ""
    try:
        with open(log_path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except FileNotFoundError:
        problems.append(CitationProblem(
            0, "append_not_made",
            f"the report claims it wrote to {LOG_NAME}, but {log_path} does "
            "not exist.", claim.strip()))
        return problems
    if not any((row.get("date_recommended") or "").strip() == pulled
               for row in rows):
        problems.append(CitationProblem(
            0, "append_not_made",
            f"the report claims it wrote to {LOG_NAME}, but the log holds no "
            f"row dated {pulled!r} (meta.pulled). Either the append did not "
            "happen or it was written under a different date.", claim.strip()))
    return problems


__all__ = [
    "CitationProblem",
    "StatedFigure",
    "STATED_FIGURE",
    "check",
    "check_consistency",
    "stated_figures",
    "percentage_convention",
]


if __name__ == "__main__":
    import sys
    if len(sys.argv) not in (3, 4):
        print("usage: python3 tools/citations.py <report.md> <facts.json> "
              "[outputs_dir]")
        print("  outputs_dir is optional and enables the append check: the "
              f"directory holding {LOG_NAME}.")
        raise SystemExit(2)
    report_path, facts_path = sys.argv[1], sys.argv[2]
    log_path = os.path.join(sys.argv[3], LOG_NAME) if len(sys.argv) == 4 else None
    found = check(report_path, facts_path)
    found += check_consistency(report_path, facts_path, log_path)
    if not found:
        note = "" if log_path else (
            f"\n(append check skipped - pass the outputs dir holding {LOG_NAME} "
            "to run it.)")
        print("every figure claim cites a fact id, every fact backs it up, and "
              "the report agrees with its own metadata." + note)
        raise SystemExit(0)
    print(f"{len(found)} problem(s):\n")
    for p in found:
        print(p)
    raise SystemExit(1)
