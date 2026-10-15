#!/usr/bin/env python3
"""Shared test-data resolution: real pulls when present, synthetic otherwise.

Why this exists
---------------
Runtime pulls are gitignored, so the data-backed suites used `skipUnless` on a
raw directory that a fresh clone never has. Measured 2026-08-20: **265 of 505
tests skipped**, and `test_budget_optimize_facts.py` executed **0 of its 71**.
That is not partial coverage, it is a suite that reports OK while testing
nothing.

The fix is not to delete the gate but to give it something to run on.
`raw_or_fixture()` returns the real dated directory when it is on disk and the
committed synthetic fixture when it is not, so:

* **On a machine with real pulls, nothing changes.** Same directory, same
  numbers, same assertions. This deliberately introduces no risk to a live run.
* **On a fresh clone, the behavioural tests run** against synthetic data, and
  only the handful that assert this specific account's figures skip.

Tests that genuinely need real account data mark themselves with
`@requires_account_data`. That decorator is the honest boundary: everything
above it is behaviour that must hold on any data, everything below it is a
claim about one specific real account.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import pulls

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "2026-08-08"


def real_raw(date: str) -> "Path | None":
    """The real dated pull directory, or None if this clone has no pulls."""
    try:
        path = pulls.raw_dir(date)
    except FileNotFoundError:
        return None
    return path if path.is_dir() else None


def raw_or_fixture(date: str) -> Path:
    """Real pulls if present, else the committed synthetic fixture.

    Never raises and never returns a nonexistent path, so a suite using it does
    not need a skip guard for "no data at all" - only for "needs *this* data".
    """
    return real_raw(date) or FIXTURE


def using_real_data(date: str) -> bool:
    return real_raw(date) is not None


def requires_account_data(date: str = "2026-08-20"):
    """Skip unless the real pulls for `date` are on disk.

    Use this **only** on a test whose assertion is about this
    account - a specific campaign id, a specific total, a claim about what this
    account's data happens to contain. A test about how the builder behaves
    belongs above this line and should run on the fixture too.
    """
    return unittest.skipUnless(
        using_real_data(date),
        f"needs real account pulls for {date}; this assertion is about the "
        "account, not about builder behaviour",
    )
