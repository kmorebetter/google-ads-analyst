#!/usr/bin/env python3
"""Generate a pull's `_manifest.json` entry from the query that ran.

Run:
    python3 tools/declare_pull.py <raw_dir> <filename> --gaql '<the exact query that ran>'
    python3 tools/declare_pull.py <raw_dir> --all

Why this exists. VF-17 rule 5 says temporality is **declared by whatever ran
the pull, never inferred** - and until now the declaration was hand-written,
which produced its own failure class: five of eleven pulls undeclared on
2026-08-19, row counts that drifted from the file, a `truncated` field the
response contradicted. This tool keeps the declaration rule intact - the
caller supplies the literal query that ran, which *is* the declaration - and
generates the mechanical parts from it and from the saved file:

- **rows** - counted from the saved response.
- **truncated** - a `nextPageToken` anywhere in the payload, or a row count at
  exactly the 10,000-row page cap.
- **filter** - the query's WHERE clause, verbatim.
- **temporality** - derived from the query text per `gaql.py`'s own
  clause rules (`DURING X` -> `window:X`, `BETWEEN 'a' AND 'b'` ->
  `window:a..b`, no date predicate -> `current-as-of-<raw dir date>`), then
  cross-checked with `gaql.check_declared_temporality` so the two manifest
  fields can never disagree.

It also checks the filename against `pull_specs.py`: an unknown filename is a
warning (hand-made exploratory pulls are legitimate), a temporality that
contradicts the spec's kind is an error and nothing is written, and a
fieldMask missing a spec's `required_fields` is a warning naming the fields -
the class of omission that silently killed a week-over-week row on
2026-08-20.

`--all` is an audit pass over an existing raw dir: it recomputes `rows` and
`truncated` for every declared file (the saved response does not record its
own query, so it cannot declare an undeclared file - those are listed as
errors, each needing one `--gaql` invocation).

Hand-written manifests remain valid everywhere; this is the paved path, not a
new gate. Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gaql  # noqa: E402
import pull_specs  # noqa: E402
import scopes as S  # noqa: E402

PAGE_CAP_ROWS = 10_000

_WHERE = re.compile(
    r"\bWHERE\b(.*?)(?:\bORDER\s+BY\b|\bLIMIT\b|\bPARAMETERS\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_BETWEEN_DATES = re.compile(
    r"BETWEEN\s+'?(\d{4}-\d{2}-\d{2})'?\s+AND\s+'?(\d{4}-\d{2}-\d{2})'?",
    re.IGNORECASE,
)
_DURING = re.compile(r"\bDURING\s+([A-Z_0-9]+)", re.IGNORECASE)


class DeclareError(Exception):
    """Refuse to write a manifest entry. The message says why."""


_OPERATOR_SPACING = re.compile(r"\s*(!=|>=|<=|=|>|<)\s*")


def extract_filter(gaql_text: str) -> str:
    """The WHERE clause of the query (or the whole text when the caller
    passed a bare filter with no SELECT).

    Whitespace around comparison operators is collapsed - the same lexical
    tidying `gaql.normalise` documents, nothing semantic - so a tool-written
    entry matches the spelling convention every hand-written manifest in this
    pack already uses (`campaign.status='ENABLED'`). Clause content, quotes
    and order are kept verbatim: the entry is still the audit trail.
    Comparison-operator tidying deliberately spares `>`/`<` inside quoted
    strings - none of this pack's filters quote them, and the builders
    re-verify every clause against the rows anyway.
    """
    text = gaql_text.strip().rstrip(";")
    m = _WHERE.search(text)
    if m:
        clause = m.group(1)
    elif not re.search(r"\bSELECT\b", text, re.IGNORECASE):
        clause = text
    else:
        return ""
    clause = " ".join(clause.split())
    return _OPERATOR_SPACING.sub(r"\1", clause)


def derive_temporality(filter_text: str, pulled: str) -> str:
    """`window:<spec>` from the date predicate, else `current-as-of-<date>`.

    Not an inference from the response (VF-17 rule 5 forbids that) - the
    caller declared the query, and the temporality is read from that
    declaration.
    """
    date_clauses = [c for c in gaql.clauses(filter_text) if gaql.is_temporality(c)]
    if not date_clauses:
        return f"current-as-of-{pulled}"
    if len(date_clauses) > 1:
        raise DeclareError(
            f"the query carries {len(date_clauses)} date predicates "
            f"({'; '.join(date_clauses)}); declare this one by hand."
        )
    clause = date_clauses[0]
    m = _BETWEEN_DATES.search(clause)
    if m:
        return f"window:{m.group(1)}..{m.group(2)}"
    m = _DURING.search(clause)
    if m:
        return f"window:{m.group(1).upper()}"
    raise DeclareError(
        f"the date predicate {clause!r} is neither DURING <token> nor "
        "BETWEEN '<date>' AND '<date>'; declare this one by hand."
    )


def read_payload(raw_dir: Path, filename: str) -> dict:
    path = raw_dir / filename
    if not path.exists():
        raise DeclareError(f"{filename}: missing from {raw_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def _find_next_page_token(payload) -> bool:
    """True when a nextPageToken appears anywhere in the saved response."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "nextPageToken" and value:
                return True
            if _find_next_page_token(value):
                return True
    elif isinstance(payload, list):
        return any(_find_next_page_token(v) for v in payload)
    return False


def row_count(payload: dict) -> int:
    return len(((payload.get("data") or {}).get("results")) or [])


def is_truncated(payload: dict) -> bool:
    return _find_next_page_token(payload) or row_count(payload) == PAGE_CAP_ROWS


def spec_check(filename: str, temporality: str, payload: dict,
               warn) -> None:
    """Check the derived entry against `pull_specs`. Kind mismatch = error."""
    entries = pull_specs.all_filenames().get(filename)
    if filename in pull_specs.SHARED_PREFLIGHT:
        return
    if not entries:
        warn(
            f"{filename}: not in any skill's pull spec (pull_specs.py). "
            "Declared anyway - exploratory pulls are legitimate - but a "
            "builder will not read it under this name."
        )
        return
    windowed = temporality.startswith("window:")
    if windowed and all(e.is_current for e in entries):
        raise DeclareError(
            f"{filename}: the query is windowed ({temporality}) but the spec "
            "says this pull is CURRENT-STATE. A windowed pull cannot answer a "
            "current-state question (VF-17); either the query or the filename "
            "is wrong."
        )
    if not windowed and all(e.is_windowed for e in entries):
        raise DeclareError(
            f"{filename}: the query has no date predicate but the spec says "
            "this pull is WINDOWED. Either the query or the filename is wrong."
        )
    if windowed:
        token = temporality.split(":", 1)[1]
        pinned = [e.window_form for e in entries
                  if e.is_windowed and re.fullmatch(r"[A-Z_0-9]+", e.window_form)]
        is_range = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}",
                                     token))
        # A concrete BETWEEN range is always acceptable where a token is
        # pinned: the token is only the DURING spelling of the same window.
        if pinned and token not in pinned and not is_range:
            raise DeclareError(
                f"{filename}: the query's window is {token} but the spec "
                f"pins {sorted(set(pinned))}. Wrong window, or wrong filename."
            )
    mask = set(
        ((payload.get("data") or {}).get("fieldMask") or "").split(",")
    )
    for entry in entries:
        missing = [f for f in entry.required_fields if f not in mask]
        if missing:
            warn(
                f"{filename}: fieldMask is missing {missing}, which a builder "
                "depends on. If this pull was made for that skill, re-pull "
                "with the field selected - an omitted field silently kills "
                "its report row."
            )
            break


def declare(raw_dir: Path, filename: str, gaql_text: str,
            note: "str | None" = None, warn=None) -> dict:
    """Compute and write one file's manifest entry. Returns the entry."""
    warn = warn or (lambda msg: print(f"WARNING: {msg}", file=sys.stderr))
    pulled = raw_dir.name
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pulled):
        raise DeclareError(
            f"{raw_dir} is not a YYYY-MM-DD directory, so a current-state "
            "window cannot be formed (VF-17 rule 4)."
        )
    payload = read_payload(raw_dir, filename)
    filter_text = extract_filter(gaql_text)
    temporality = derive_temporality(filter_text, pulled)
    # The two fields are generated from one source, so this can only fail on
    # a bug in this tool - run it anyway, the same check the builders run.
    gaql.check_declared_temporality(filter_text, temporality)
    spec_check(filename, temporality, payload, warn)

    entry = {
        "present": True,
        "rows": row_count(payload),
        "truncated": is_truncated(payload),
        "temporality": temporality,
        "filter": filter_text,
    }

    path = raw_dir / S.MANIFEST
    manifest = (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "raw_dir": str(raw_dir),
            "pulled": pulled,
            "temporality_is_declared_not_inferred": True,
        }
    )
    files = manifest.setdefault("files", {})
    old = files.get(filename) or {}
    if note is not None:
        entry["note"] = note
    elif "note" in old:
        entry["note"] = old["note"]
    changed = {
        k: (old.get(k), v) for k, v in entry.items() if old.get(k) != v
    }
    files[filename] = {**old, **entry}
    path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return {"entry": files[filename], "changed": changed}


def audit_all(raw_dir: Path, warn=None) -> "tuple[list, list]":
    """Recompute rows/truncated for every declared file. Returns
    (updated filenames, undeclared filenames)."""
    warn = warn or (lambda msg: print(f"WARNING: {msg}", file=sys.stderr))
    path = raw_dir / S.MANIFEST
    if not path.exists():
        raise DeclareError(
            f"no {S.MANIFEST} in {raw_dir}. --all can only audit an existing "
            "manifest; the saved responses do not record their own queries, "
            "so declare each pull with --gaql first."
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest.setdefault("files", {})
    updated, undeclared = [], []
    for pull_path in sorted(raw_dir.glob("*.json")):
        name = pull_path.name
        if name == S.MANIFEST or name.endswith("-facts.json"):
            continue
        if name not in files:
            undeclared.append(name)
            continue
        payload = read_payload(raw_dir, name)
        fresh = {"rows": row_count(payload), "truncated": is_truncated(payload)}
        old = files[name]
        if any(old.get(k) != v for k, v in fresh.items()):
            files[name] = {**old, **fresh, "present": True}
            updated.append(name)
    if updated:
        path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return updated, undeclared


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("raw_dir", type=Path)
    parser.add_argument("filename", nargs="?")
    parser.add_argument("--gaql", help="the exact query that ran, verbatim")
    parser.add_argument("--note", help="optional note for the entry")
    parser.add_argument(
        "--all", action="store_true",
        help="audit mode: recompute rows/truncated for every declared file",
    )
    args = parser.parse_args(argv)

    try:
        if args.all:
            if args.filename or args.gaql:
                parser.error("--all takes no filename and no --gaql")
            updated, undeclared = audit_all(args.raw_dir)
            for name in updated:
                print(f"{name}: rows/truncated refreshed from the saved file")
            if not updated:
                print("manifest agrees with every declared file - no changes")
            if undeclared:
                print(
                    f"ERROR: {len(undeclared)} pull(s) have no manifest entry "
                    f"and no recorded query: {', '.join(undeclared)}. Declare "
                    "each with: declare_pull.py <raw_dir> <filename> --gaql "
                    "'<the query that ran>'",
                    file=sys.stderr,
                )
                return 2
            return 0
        if not args.filename or not args.gaql:
            parser.error("either --all, or <filename> --gaql '<query>'")
        result = declare(args.raw_dir, args.filename, args.gaql, args.note)
        entry = result["entry"]
        state = "updated" if result["changed"] else "unchanged"
        print(
            f"{args.filename}: {state} - rows={entry['rows']} "
            f"truncated={entry['truncated']} temporality={entry['temporality']!r} "
            f"filter={entry['filter']!r}"
        )
        return 0
    except (DeclareError, gaql.FilterMismatch) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
