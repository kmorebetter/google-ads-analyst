#!/usr/bin/env python3
"""Shared reader for Google Ads UI report exports (CSV or "Excel CSV" UTF-16 TSV).

Google Ads exports start with a report title line and a date-range line, then
the header, then rows, then one or more "Total: ..." rows. Numbers come with
thousands separators, currency symbols, percent signs and "--" for empty.
This module handles all of that once so every CSV tool reads files the same
way. Standard library only.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


class CsvInputError(Exception):
    """The file is not the export this tool expects. The message says why."""


def decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if len(raw) > 3 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00":
        return raw.decode("utf-16-le")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CsvInputError("could not decode the file")


def number(text: str) -> Decimal:
    t = (text or "").strip()
    for junk in (",", "%", "$", "€", "£", "¥", "\u00a0", " "):
        t = t.replace(junk, "")
    t = re.sub(r"^[A-Z]{3}", "", t)  # "CAD123.00"
    if t in ("", "--", "-", "<10%", ">90%"):
        return Decimal("0")
    if t.startswith("<") or t.startswith(">"):
        t = t[1:]
    try:
        return Decimal(t)
    except InvalidOperation:
        raise CsvInputError(f"not a number: {text!r}")


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date_range(line: str) -> "tuple[date, date] | None":
    """'August 1, 2026 - August 30, 2026' or '2026-08-01 - 2026-08-30'."""
    iso = re.findall(r"(\d{4})-(\d{2})-(\d{2})", line)
    if len(iso) >= 2:
        a, b = iso[0], iso[1]
        return date(*map(int, a)), date(*map(int, b))
    words = re.findall(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", line)
    if len(words) >= 2:
        out = []
        for mon, d, y in words[:2]:
            m = _MONTHS.get(mon.lower())
            if not m:
                return None
            out.append(date(int(y), m, int(d)))
        return out[0], out[1]
    return None


@dataclass
class Export:
    header: "list[str]"
    rows: "list[dict]"
    title: str = ""
    date_range: "tuple[date, date] | None" = None
    total_rows: int = 0

    @property
    def days(self) -> "int | None":
        if not self.date_range:
            return None
        return (self.date_range[1] - self.date_range[0]).days + 1


def read_file(path, required, optional=None, report_name="report", forbid=()) -> "Export":
    """Read an export from disk. A missing or unreadable file is a CsvInputError."""
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        raise CsvInputError(f"cannot open {path}: {e.strerror or e}")
    return read(raw, required, optional, report_name, forbid)


def read(raw: bytes, required: "dict[str, tuple]", optional: "dict[str, tuple] | None" = None,
         report_name: str = "report", forbid: "tuple" = ()) -> Export:
    """Read an export. `required`/`optional` map a field key to header aliases
    (lower-case). Each returned row is a dict keyed by field key, raw strings."""
    optional = optional or {}
    text = decode(raw)
    first = text.splitlines()[:30]
    delimiter = "\t" if sum(l.count("\t") for l in first) > sum(l.count(",") for l in first) else ","
    if "\x00" in text or text.startswith("PK"):
        raise CsvInputError(f"this is not a CSV file (it looks like a binary or Excel .xlsx file). "
                            f"In Google Ads, download the {report_name} report as .csv")
    try:
        lines = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as e:
        raise CsvInputError(f"the file is not a readable CSV ({e})")
    aliases = {**required, **optional}
    header_at = None
    for i, line in enumerate(lines[:30]):
        lowered = {c.strip().lower() for c in line}
        if all(any(a in lowered for a in names) for names in required.values()):
            header_at = i
            break
    if header_at is None:
        want = ", ".join(names[0] for names in required.values())
        raise CsvInputError(
            f"this does not look like a Google Ads {report_name} export: no header row "
            f"with all of [{want}] in the first 30 lines")
    preamble = [" ".join(l) for l in lines[:header_at]]
    title = preamble[0].strip() if preamble else ""
    dr = None
    for p in preamble:
        dr = parse_date_range(p)
        if dr:
            break
    header = [c.strip().lower() for c in lines[header_at]]
    for name, other in forbid:
        if name in header:
            raise CsvInputError(f"this is a Google Ads {other} export, not a {report_name} export")
    col = {}
    for key, names in aliases.items():
        for idx, name in enumerate(header):
            if name in names:
                col[key] = idx
                break
    rows, totals = [], 0
    first_key = next(iter(required))
    for line in lines[header_at + 1:]:
        if not line or all(not c.strip() for c in line):
            continue
        cells = [c.strip() for c in line]
        lead = " ".join(cells[:3]).lower()
        if lead.startswith("total") or (cells and cells[0].lower().startswith("total")):
            totals += 1
            continue
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        row = {k: cells[i] for k, i in col.items()}
        if not row.get(first_key):
            continue
        rows.append(row)
    if not rows:
        raise CsvInputError(f"the {report_name} export has a header but no data rows")
    return Export(header=header, rows=rows, title=title, date_range=dr, total_rows=totals)


def md_table(head: "list[str]", rows: "list[list]") -> str:
    def esc(v):
        return str(v).replace("|", "\\|")
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join(":-" for _ in head) + " |"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def money(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01')):,}"
