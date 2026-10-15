"""The single definition of "not a claim about the account".

`citations.py` is the pack's one mechanical report checker, and this module is
where its exemptions live — a closed, named list, defined once. It started as
the peace treaty between two checkers that disagreed (`30-day` passed one and
failed the other, `### 3.3` the reverse, and no report wording passed both);
the second checker (`facts.verify_report`, the value search) was removed in the
2026-08-27 V2 overhaul, but the lesson stands: exemptions are defined HERE and
nowhere else. Do not add a local exemption to any checker.

`masked_lines()` is the one entry point: it does the structural masking (fenced
blocks, inline code, URLs, markdown link targets, `[fact: id]` tokens) and the
non-claim masking in one pass, length preserving.

**Masking is length preserving.** Every match becomes a run of spaces of the
same length. `citations.py` reads the characters either side of a figure
(its STATED_FIGURE lookarounds) to decide whether it is part of a longer
token, so deleting text would move those, and reported line/column positions
would stop lining up with the file.

**The blind spot here is over-masking, not under-masking.** Every exemption
makes the checker greener, and a green checker standing in for judgement is
the exact failure VF-19 warns about. So the exemptions are a closed, named list,
each one addressable from the tests, and `tools/test_facts.py` carries a negative
corpus asserting that real claims (`126 dead headline slots`, `61 servable
RSAs`) still fail when uncited.

What is exempt, and why each one is not a claim about the account:

1.  Fenced code blocks and inline code spans - the report's two structurally
    uncitable figures live there by contract (audit-ad-copy step 9).
2.  URLs and markdown link targets - `ocid` and object ids inside an href.
3.  `[fact: <id>]` citation tokens - a fact id can contain digits
    (`...adgroup.620000000011.ad.620000000010.headline.rank_1...`). The id is
    the proof, not the claim.
4.  Markdown heading numbers (`### 3.3`, `### 4. Classify ...`) and ordered
    list markers (`1.`, `2)`) - document structure.
5.  Dotted section labels with three or more parts (`1.2.3`).
6.  A numeral hyphenated or underscored into a word: `30-day`, `8-action`,
    `14-day`, `step 1a`, `H3`, and every GAQL window token (`LAST_30_DAYS`).
7.  Known GAQL date-range tokens, named separately from 6 so they are
    testable on their own.
8.  A number following a label word: `section 5`, `rule 6`, `Tier 2`,
    `step 3.3`, `top 10`, `bottom 5`, `rank 3`. These point at a place in a
    document or a position in a ranking. They measure nothing. `version 2.4` is
    NOT on the list, deliberately - see the note beside LABEL_WORDS.
9.  Dates and times in every form the two checkers already handled between
    them: `2026-08-19`, `Aug 14`, `14 August`, `14:56`, `Aug 5/10`.
10. Ordinals (`the 14th`).
11. Years (`2026`).
12. Object ids. Three forms: after an object noun (`ad group 620000000012`,
    `criterion 620000000013`), alone in parentheses (`(10363430)`), or as a
    bare run of 8 or more digits.

    The bare-run threshold was 9. This account has **8-digit criterion ids**
    (`10363430` is real, and `evidence-links` requires printing an id beside
    every keyword claim), so 9 made a compliant report impossible. Lowering it
    to 8 is free here: the largest integer-valued fact in the 2026-08-19 file
    is 194,056 impressions, six digits, and every money figure is a decimal.
    No legitimate figure in this pack reaches eight digits.

Python 3 standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Structural
# --------------------------------------------------------------------------

#: A fenced block opener or closer. Both fence syntaxes: honouring one and not
#: the other is how the two pre-unification checkers came to disagree, and a
#: `~~~` fence that silently stops being a fence would put its contents in
#: scope as claims.
FENCE = re.compile(r"^\s*(?:```|~~~)")

INLINE_CODE = re.compile(r"`[^`]*`")

#: A code span holding a figure, immediately followed by a `[fact: ...]`
#: citation. **This is the one code span that is NOT exempt**, and it has to be
#: un-spanned before `INLINE_CODE` blanks it.
#:
#: Why. Code spans are exempt because a report legitimately prints figures that
#: are not account claims: rule thresholds, object ids, GAQL window tokens. But
#: writing `` `167.008535` `` beside `[fact: st.top_table.rank_1.conversions]` is
#: not that. It is the author asserting *this number comes from that fact* - and
#: the exemption meant neither checker looked. Measured 2026-08-21: a report
#: shipped four fabricated table cells, one of them a wrong DECISION, all citing
#: facts that said otherwise, and both checkers passed. A probe confirmed the
#: hole was total: `` `42` `` cited to a fact holding `167.260587` also passed.
#:
#: The exemption survives wherever it was earning its keep, because it only
#: lapses on direct adjacency to a citation.
SPANNED_FIGURE_WITH_CITATION = re.compile(
    r"`(?P<value>[^`\n]*\d[^`\n]*)`(?P<gap>\s*)(?=`?\[fact:)"
)
URL = re.compile(r"https?://\S+")
LINK_TARGET = re.compile(r"\]\([^)]*\)")

#: A `[fact: <id>]` citation token. `citations.py` reads these separately, off
#: the raw text, before the masking is applied.
CITATION_TOKEN = re.compile(r"\[fact:\s*[A-Za-z0-9_.\-]+\s*\]")

# --------------------------------------------------------------------------
# Document structure
# --------------------------------------------------------------------------

#: `### 3.3`, `## 4.`, `# 5 Something`. Anchored: only a number that opens a
#: heading is structure. A number later on the heading line is still a claim.
HEADING_NUMBER = re.compile(r"^\s*#{1,6}\s*\d+(?:\.\d+)*\.?(?=\s|$)")

#: `1.` / `2)` opening a list item.
LIST_MARKER = re.compile(r"^\s*\d+[.)](?=\s)")

#: `1.2.3` - three or more dot-separated parts is a label, never a decimal.
DOTTED_LABEL = re.compile(r"(?<![\w.])\d+(?:\.\d+){2,}(?![\w.])")

#: Words that make a following number a reference rather than a measurement.
#: Deliberately tight. `note`, `row` and `point` were considered and dropped:
#: they read as ordinary prose often enough that "note 2 ads changed" would have
#: hidden a real claim. Over-masking is this module's failure mode, so a word
#: earns its place only if the pack's own vocabulary uses it as a pointer.
LABEL_WORDS = (
    "section", "sections", "step", "steps", "rule", "rules", "tier", "tiers",
    "part", "parts", "appendix", "item", "items", "figure", "fig", "table",
    "chapter", "phase", "question", "footnote", "column",
    "vf", "top", "bottom", "rank", "ranks",
)
#: `version` was added here and then taken back out. Decimals became claims in
#: 2026-08-19's citation fix, which put `version 2.4` and a bare `3.11` in scope
#: for the first time. Neither appears anywhere in this pack's prose, so
#: exempting a word the pack does not use would have made the checker greener
#: on a claim nobody makes. The bar above is the bar: a word earns its place by
#: being used as a pointer here. `v21.1` is already covered by
#: WORD_WITH_NUMERAL and `1.2.3` by DOTTED_LABEL, which is every version form
#: the pack has ever written.
LABELLED_NUMBER = re.compile(
    r"\b(?:" + "|".join(LABEL_WORDS) + r")\s+\d+(?:\.\d+)*[a-z]?\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# Numerals that are part of a word
# --------------------------------------------------------------------------

#: A token of letters, digits, `_` and `-` that holds at least one letter and at
#: least one digit: `30-day`, `8-action`, `LAST_30_DAYS`, `1a`, `H3`, `Aug-10`.
#: A token of digits and hyphens only is NOT matched, so `60-40 split` stays two
#: figures, which is what `facts.py` documented.
WORD_WITH_NUMERAL = re.compile(
    r"(?<![\w-])(?=[\w-]*[A-Za-z])(?=[\w-]*\d)[A-Za-z0-9_-]+(?![\w-])"
)

#: Named separately from WORD_WITH_NUMERAL, which already covers them, so a test
#: can assert this class directly. These appear in every printed scope filter.
GAQL_WINDOW_TOKEN = re.compile(
    r"\b(?:LAST|NEXT)_\d+_(?:DAYS|MONTHS|WEEKS)\b|\bLAST_\d+_DAYS\b"
)

# --------------------------------------------------------------------------
# Dates, times, ordinals, years
# --------------------------------------------------------------------------

_MONTH = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?"
)

ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

DATEY = re.compile(
    rf"(?:{_MONTH})\s+\d{{1,2}}\b"          # Aug 14
    rf"|\b\d{{1,2}}\s+(?:{_MONTH})\b"       # 14 August
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b",       # 14:56, 14:56:03
    re.IGNORECASE,
)

#: `Aug 5/10`, `08/19`, `2026/08/19`. The month-name half is taken by DATEY; this
#: takes the slash-joined numerals, which `facts.py` skipped by adjacency and
#: `citations.py` did not skip at all.
SLASH_NUMERALS = re.compile(r"(?<![\w/])\d{1,4}(?:/\d{1,4})+(?![\w/])")

ORDINAL = re.compile(r"\b\d{1,4}(?:st|nd|rd|th)\b", re.IGNORECASE)

#: The trailing guard is split so a year that ENDS A SENTENCE is still a year.
#: It was `(?![\d.,%])`, which refused `2026.` because of the full stop. That
#: was invisible while only bare integers were checked - the integer scanner
#: refused a trailing `.` too, so the year was skipped for the wrong reason -
#: and surfaced the moment decimals came into scope. A `.` only continues the
#: token when a digit follows it.
YEAR = re.compile(r"(?<![\d.,])(?:19|20)\d{2}(?![\d,%])(?!\.\d)")

# --------------------------------------------------------------------------
# Object ids
# --------------------------------------------------------------------------

#: Nouns that name a Google Ads object, so digits after one are its id. Kept to
#: true object nouns: a label word like `rule` is handled by LABELLED_NUMBER and
#: must not license an id here.
OBJECT_WORDS = (
    "ad group", "ad groups", "adgroup", "adgroups", "ad_group",
    "ad", "ads", "campaign", "campaigns", "criterion", "criteria",
    "keyword", "keywords", "asset", "assets", "customer", "account",
    "ocid", "resource", "id", "ids",
)
OBJECT_ID_IN_CONTEXT = re.compile(
    r"\b(?:" + "|".join(w.replace(" ", r"\s+") for w in OBJECT_WORDS)
    + r")\s*[:#=]?\s*`?\d{6,}`?\b",
    re.IGNORECASE,
)

#: `Templates (620000000011)` - the inline form `evidence-links` prescribes.
PARENTHESISED_ID = re.compile(r"\(\s*\d{6,}\s*\)")

#: A bare run of digits long enough to be an object id and too long to be any
#: figure this pack reports. See the module docstring, point 12.
#:
#: The `(?<!\d\.)` half of the lookbehind is load-bearing and was missing until
#: 2026-08-21. Without it the FRACTIONAL PART of a long decimal reads as an
#: object id: `2.4134329359732405` masked to `2.`, because `4134329359732405` is
#: 16 digits. Both checkers then compared the literal `2` against the fact, which
#: is a mismatch, so a correct figure looked wrong. The workaround adopted at the
#: time was to wrap such figures in code spans - and code spans are exempt, which
#: silently disabled the value check on every long decimal in the pack. That is
#: how four fabricated table cells shipped past both checkers on 2026-08-21.
#:
#: An object id never follows a decimal point, so excluding that case costs
#: nothing and restores value checking on ratios and long-precision conversions.
BARE_OBJECT_ID = re.compile(r"(?<!\d)(?<!\d\.)\d{8,}(?!\d)")

#: The minimum digits a bare run needs before it is read as an object id.
#: Named so the reason for the number is testable, not folklore.
BARE_OBJECT_ID_MIN_DIGITS = 8

# --------------------------------------------------------------------------
# The ordered pipeline
# --------------------------------------------------------------------------
#: Order matters. Anchored document-structure patterns run first, on the line as
#: written. URLs and code spans run before the content patterns so digits inside
#: them are never reinterpreted.
NON_CLAIM_PATTERNS: tuple = (
    ("heading_number", HEADING_NUMBER),
    ("list_marker", LIST_MARKER),
    ("url", URL),
    ("link_target", LINK_TARGET),
    ("inline_code", INLINE_CODE),
    ("citation_token", CITATION_TOKEN),
    ("iso_date", ISO_DATE),
    ("slash_numerals", SLASH_NUMERALS),
    ("datey", DATEY),
    ("ordinal", ORDINAL),
    ("object_id_in_context", OBJECT_ID_IN_CONTEXT),
    ("parenthesised_id", PARENTHESISED_ID),
    ("dotted_label", DOTTED_LABEL),
    ("labelled_number", LABELLED_NUMBER),
    ("gaql_window_token", GAQL_WINDOW_TOKEN),
    ("word_with_numeral", WORD_WITH_NUMERAL),
    ("bare_object_id", BARE_OBJECT_ID),
    ("year", YEAR),
)


def _blank(text: str, pattern: re.Pattern) -> str:
    """Replace every match with spaces of the same length."""
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def unspan_cited_figures(line: str) -> str:
    """Strip the backticks off a figure that cites a fact right after it.

    Runs before every exemption, so `INLINE_CODE` cannot hide a figure whose
    author explicitly claimed a fact backs it. Replaces the two backticks with
    spaces rather than deleting them, so every column offset is preserved and a
    reported line still lines up with the file.
    """
    def swap(match: "re.Match") -> str:
        return " " + match.group("value") + " " + match.group("gap")

    return SPANNED_FIGURE_WITH_CITATION.sub(swap, line)


def mask_non_claims(line: str) -> str:
    """Blank every run of characters that is not a claim about the account."""
    line = unspan_cited_figures(line)
    for _, pattern in NON_CLAIM_PATTERNS:
        line = _blank(line, pattern)
    return line


def which_patterns_match(line: str) -> list:
    """Names of the exemptions that fire on `line`. For tests and debugging."""
    return [name for name, pattern in NON_CLAIM_PATTERNS if pattern.search(line)]


@dataclass(frozen=True)
class MaskedLine:
    """One report line, as written and as both checkers see it."""

    number: int
    masked: str
    raw: str
    in_fence: bool


def masked_lines(text: str) -> list:
    """Every line of a report, masked for the checker.

    A fenced block's interior, and the fence markers themselves, come back with
    an empty `masked` string: no figure is found there, and `citations.py`
    treats the fence as a block boundary.
    """
    out: list = []
    fenced = False
    for number, raw in enumerate(text.splitlines(), start=1):
        if FENCE.match(raw):
            fenced = not fenced
            out.append(MaskedLine(number=number, masked="", raw=raw, in_fence=True))
            continue
        if fenced:
            out.append(MaskedLine(number=number, masked="", raw=raw, in_fence=True))
            continue
        out.append(
            MaskedLine(
                number=number, masked=mask_non_claims(raw), raw=raw, in_fence=False
            )
        )
    return out


__all__ = [
    "MaskedLine",
    "masked_lines",
    "mask_non_claims",
    "which_patterns_match",
    "NON_CLAIM_PATTERNS",
    "CITATION_TOKEN",
    "FENCE",
    "LABEL_WORDS",
    "OBJECT_WORDS",
    "BARE_OBJECT_ID_MIN_DIGITS",
]
