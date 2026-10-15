#!/usr/bin/env python3
"""The skill file, its builder, and `pull_specs.py` must agree on which pulls exist.

Run:  python3 tools/test_skill_builder_contract.py

Why this file exists
--------------------
On 2026-08-20 `mine-search-terms` shipped a negatives CSV without its mandatory
90-day do-not-negate clearance, and a search term went back onto a negation list
it had been removed from 15 days earlier. Nothing was broken in either the code
or the prose on its own. **They had drifted apart:** step 1b said
`LAST_30_DAYS`, two earlier runs had pulled 90 days and called it mandatory, and
no run in between could see the difference.

The first version of this test compared SKILL.md prose directly against each
builder's module constants, which worked but put the contract in no single
place and grew waiver lists. `pull_specs.py` is now the hub: **the spec is the
contract**, the builders import their filenames from it, and this file checks
both sides against the spec - builders at import level (no regex), SKILL.md
prose by the same filename regex as before. It also covers
`tools/declare_pull.py`, which writes manifest entries from the spec's
temporality kinds, using the committed fixture.

It needs no account data, so unlike most of this pack's tests it runs on a
fresh clone.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import pull_specs  # noqa: E402

FIXTURE = HERE / "fixtures" / "2026-08-08"


def skill_doc_path(skill: str) -> Path:
    """Find `<skill>/SKILL.md`, layout-independent (see `pulls.outputs_root`).

    Standalone repo: `tools/` sits at the repo root, skills are its siblings —
    `REPO/<skill>/SKILL.md`. Consolidation monorepo: skills sit one level
    deeper, under `skills/` — `REPO/skills/<skill>/SKILL.md`. Try the first,
    fall back to the second, raise rather than guess.
    """
    direct = REPO / skill / "SKILL.md"
    if direct.exists():
        return direct
    nested = REPO / "skills" / skill / "SKILL.md"
    if nested.exists():
        return nested
    raise FileNotFoundError(
        f"no SKILL.md for {skill!r} at {direct} or {nested}"
    )


#: builder module stem -> the skill directory it implements.
PAIRS = {
    "build_weekly_review_facts": "weekly-review",
    "build_budget_optimize_facts": "budget-optimize",
    "build_mine_search_terms_facts": "mine-search-terms",
    "build_audit_extensions_facts": "audit-extensions",
    "build_audit_ad_copy_facts": "audit-ad-copy",
}

#: A builder's own output is not a pull, so it is never expected in a spec.
OUTPUT_SUFFIX = "-facts.json"

PULL_RE = re.compile(r'"([a-z0-9][a-z0-9-]*\.json)"')

#: The manifest is infrastructure, not a pull.
INFRASTRUCTURE = {"_manifest.json"}

#: Filenames a SKILL.md may name beyond its spec, each with a reason.
SKILL_EXTRAS = {
    # The superseded 2026-08-20 cost pull, named only in its own
    # supersession note. Deliberately not read by anything.
    "mine-search-terms": {"search-terms-cost-30d.no-adgroup-name.bak.json"},
}


def _interesting(name: str) -> bool:
    return (
        name.endswith(".json")
        and not name.endswith(OUTPUT_SUFFIX)
        and name not in pull_specs.SHARED_PREFLIGHT
        and name not in INFRASTRUCTURE
    )


def builder_pulls(stem: str) -> set:
    """Every pull filename a builder resolves at runtime, minus its output.

    Imports the module and reads its module-level strings rather than regexing
    the source, because the builders take their filenames from `pull_specs.py`
    (directly, or via `scopes.py` for `build_audit_ad_copy_facts`). A source
    scan misses that indirection entirely and reports a builder that reads
    five pulls as reading none - which is how the first draft of this test
    almost cleared a gap it should have caught.
    """
    module = importlib.import_module(stem)
    found = {
        value
        for value in vars(module).values()
        if isinstance(value, str) and _interesting(value)
    }
    # Literals used inline rather than bound to a module constant.
    text = (HERE / f"{stem}.py").read_text(encoding="utf-8")
    found |= {n for n in PULL_RE.findall(text) if _interesting(n)}
    # Legacy read-only aliases (audit-ad-copy's old asset-view name) are
    # accepted for reading, not part of the contract.
    scopes = importlib.import_module("scopes")
    return found - set(scopes.ASSET_VIEW_LEGACY_ALIASES)


def skill_pulls(skill: str) -> set:
    """Every `*.json` filename named anywhere in the skill's SKILL.md."""
    text = skill_doc_path(skill).read_text(encoding="utf-8")
    return {
        name
        for name in PULL_RE.findall(text.replace("`", '"'))
        if _interesting(name)
    }


class SpecIsTheHubTests(unittest.TestCase):
    """pull_specs.py is the single source; both sides must agree with it."""

    def test_every_builder_reads_exactly_what_its_spec_says(self):
        """Import-level, no regex: the builder's filename constants are the
        spec's read_by_builder set - nothing more, nothing less.

        More = an undocumented read nobody knows to pull. Less = a spec'd
        check the builder silently dropped (the 90-day regression's shape).
        """
        for stem, skill in PAIRS.items():
            with self.subTest(skill=skill):
                self.assertEqual(
                    builder_pulls(stem),
                    set(pull_specs.read_filenames(skill)),
                    f"{stem}.py and pull_specs.py disagree about which pulls "
                    f"the {skill} builder reads. The spec is the contract: "
                    "change it first, then both sides.",
                )

    def test_every_skill_documents_exactly_its_spec(self):
        """The SKILL.md names every spec'd pull, and no unexplained others.

        A spec'd pull the skill never names is a pull nobody knows to make.
        A skill-named pull outside the spec is either drift or belongs in
        SKILL_EXTRAS with a reason.
        """
        for skill in PAIRS.values():
            with self.subTest(skill=skill):
                documented = skill_pulls(skill)
                spec = set(pull_specs.documented_filenames(skill))
                extras = SKILL_EXTRAS.get(skill, set())
                self.assertFalse(
                    spec - documented,
                    f"{skill}/SKILL.md never names {sorted(spec - documented)} "
                    "although pull_specs.py requires them. Document the pull "
                    "in step 1, or remove it from the spec.",
                )
                self.assertFalse(
                    documented - spec - extras,
                    f"{skill}/SKILL.md names "
                    f"{sorted(documented - spec - extras)} which pull_specs.py "
                    "does not know. Add the pull to the spec, or the waiver "
                    "to SKILL_EXTRAS with a reason.",
                )

    def test_unread_spec_entries_are_real_gaps(self):
        """A read_by_builder=False entry is a promise the pack does not keep.

        Each must be genuinely unread (else flip the flag) and still
        documented in the skill (else delete the entry). This replaces the
        old DOCUMENTED_BUT_UNREAD waiver list.
        """
        for stem, skill in PAIRS.items():
            reads = builder_pulls(stem)
            for spec in pull_specs.specs()[skill]:
                if spec.read_by_builder:
                    continue
                with self.subTest(skill=skill, pull=spec.filename):
                    self.assertNotIn(
                        spec.filename, reads,
                        f"{stem}.py now reads {spec.filename}; set "
                        "read_by_builder=True in pull_specs.py.",
                    )

    def test_the_pairs_map_covers_every_builder_that_has_a_skill(self):
        """A new builder must not be able to opt out by not being listed."""
        for path in sorted(HERE.glob("build_*_facts.py")):
            stem = path.stem
            skill = stem.replace("build_", "").replace("_facts", "").replace("_", "-")
            try:
                skill_doc_path(skill)
            except FileNotFoundError:
                continue
            self.assertIn(
                stem,
                PAIRS,
                f"{stem}.py has a matching skill at {skill}/ but is absent "
                "from PAIRS, so its pull contract is unchecked.",
            )
        for skill in pull_specs.specs():
            self.assertIn(
                skill, set(PAIRS.values()),
                f"pull_specs.py declares {skill!r} but PAIRS does not test it.",
            )

    def test_builder_filter_labels_come_from_the_spec(self):
        """Where a spec pins a filter label, the builder's constant matches."""
        budget = importlib.import_module("build_budget_optimize_facts")
        mst = importlib.import_module("build_mine_search_terms_facts")
        ext = importlib.import_module("build_audit_extensions_facts")
        for skill, filename, actual in (
            ("budget-optimize", pull_specs.CAMPAIGNS_CURRENT, budget.ENABLED),
            ("budget-optimize", pull_specs.CONV_7D, budget.ENABLED_8),
            ("budget-optimize", pull_specs.CHANGE_EVENTS_14D, budget.CHANGE_SCOPE),
            ("mine-search-terms", pull_specs.ST_COST_30D, mst.COST_SCOPE),
            ("mine-search-terms", pull_specs.ST_CONV_30D, mst.CONVERSIONS_SCOPE),
            ("audit-extensions", pull_specs.CAMPAIGN_ASSETS_CURRENT,
             ext.PULL_CAMPAIGN_ASSET),
            ("audit-extensions", pull_specs.CUSTOMER_ASSETS_CURRENT,
             ext.PULL_CUSTOMER_ASSET),
        ):
            with self.subTest(skill=skill, pull=filename):
                spec = pull_specs.spec_for(skill, filename)
                self.assertEqual(spec.filter, actual)

    def test_the_audit_extensions_cost_ranking_is_wired(self):
        """The gap that made a 24-campaign recommendation wrong on 2026-08-20.

        The skill's selection rule said to rank an absent_everywhere table by
        7-day cost. The builder did not read the cost pull, so the report
        recommended a rollout to 24 campaigns of which 23 had spent nothing.
        """
        name = pull_specs.COST_7D
        self.assertIn(name, builder_pulls("build_audit_extensions_facts"))
        self.assertIn(name, skill_pulls("audit-extensions"))
        self.assertIsNotNone(pull_specs.spec_for("audit-extensions", name))

    def test_the_90_day_clearance_specifically_is_wired_both_ways(self):
        """The regression that motivated this file, pinned by name.

        Not a substitute for the general tests above: this one names the file,
        so deleting the guard fails loudly rather than quietly shrinking a set.
        """
        name = pull_specs.DONOTNEGATE_90D
        self.assertIn(name, builder_pulls("build_mine_search_terms_facts"))
        self.assertIn(name, skill_pulls("mine-search-terms"))
        self.assertIsNotNone(pull_specs.spec_for("mine-search-terms", name))
        skill = skill_doc_path("mine-search-terms").read_text(encoding="utf-8")
        self.assertIn(
            "BETWEEN",
            skill,
            "the 90-day window needs the BETWEEN form; LAST_90_DAYS is not a "
            "valid GAQL constant",
        )
        self.assertNotRegex(
            skill,
            r"90[- ]day[^.]{0,80}LAST_30_DAYS",
            "the skill describes the 90-day clearance with a 30-day window",
        )

    def test_spec_temporality_kinds_are_well_formed(self):
        for skill, entries in pull_specs.specs().items():
            for spec in entries:
                with self.subTest(skill=skill, pull=spec.filename):
                    self.assertIn(spec.temporality, ("current", "window"))
                    if spec.is_current:
                        self.assertFalse(spec.window_form)
                    else:
                        self.assertTrue(spec.window_form)


class FactMandateTests(unittest.TestCase):
    """A skill's prose promise and its builder's emission cannot drift apart.

    The bucket-ROAS defect (MAINTENANCE 2026-08-27): weekly-review step 5
    mandated a non-brand assumed-value ROAS the builder never emitted, so the
    report substituted the blended figure guardrail 2 forbids and named the
    wrong cause. The pull contract above was green the whole time - the drift
    was one level up, at the fact. These tests enforce `MANDATED_FACTS` in
    both directions: every mandated id exists in a fixture build (a null fact
    exists; existence, not non-null, is the mandate), and every mandate's
    promising sentence still exists in the SKILL.md.
    """

    @classmethod
    def setUpClass(cls):
        import unittest.mock as mock
        import make_fixture
        import build_weekly_review_facts as WR
        import build_budget_optimize_facts as BO

        # The fixture's synthetic campaign ids sit outside account-config's
        # bucket sets, so patch the fixture's Brand/TOF campaigns into their
        # intended buckets - same pattern as BucketRoasFixtureTests.
        patches = [
            mock.patch.object(WR, "BRAND_CAMPAIGN_IDS",
                              frozenset({make_fixture.BRAND})),
            mock.patch.object(WR, "TOF_CAMPAIGN_IDS",
                              frozenset({make_fixture.TOF})),
        ]
        for p in patches:
            p.start()
            cls.addClassCleanup(p.stop)
        cls.factsets = {
            "weekly-review": WR.build(FIXTURE, Path("unused-plan-path")),
            "budget-optimize": BO.build(FIXTURE),
        }

    def test_every_mandated_fact_is_emitted_by_its_builder(self):
        for skill, mandates in pull_specs.MANDATED_FACTS.items():
            fs = self.factsets[skill]
            emitted = {fact.id for fact in fs}
            for mandate in mandates:
                for fact_id in mandate.fact_ids:
                    with self.subTest(skill=skill, fact=fact_id):
                        self.assertIn(
                            fact_id, emitted,
                            f"{skill}'s builder no longer emits {fact_id}, "
                            f"but the skill still promises it: {mandate.note}",
                        )

    def test_every_mandate_prose_anchor_still_matches_the_skill(self):
        for skill, mandates in pull_specs.MANDATED_FACTS.items():
            text = skill_doc_path(skill).read_text(encoding="utf-8")
            for mandate in mandates:
                with self.subTest(skill=skill, note=mandate.note):
                    self.assertTrue(
                        re.search(mandate.prose_anchor, text, re.I | re.S),
                        f"{skill}/SKILL.md no longer carries the sentence "
                        f"behind this mandate: {mandate.note}. Either restore "
                        "the prose or retire the mandate in pull_specs.py - "
                        "never leave the two disagreeing.",
                    )

    def test_mandates_only_name_skills_the_contract_covers(self):
        """A mandate on an unspecced or untested skill would never run."""
        for skill in pull_specs.MANDATED_FACTS:
            self.assertIn(skill, pull_specs.specs(),
                          f"{skill!r} has mandates but no pull spec")
            self.assertIn(skill, set(PAIRS.values()),
                          f"{skill!r} has mandates but PAIRS does not test it")
            self.assertIn(skill, self.factsets,
                          f"{skill!r} has mandates but no fixture build here; "
                          "add it to FactMandateTests.setUpClass")

    def test_mandates_are_well_formed(self):
        for skill, mandates in pull_specs.MANDATED_FACTS.items():
            for mandate in mandates:
                with self.subTest(skill=skill, note=mandate.note):
                    self.assertTrue(mandate.fact_ids)
                    self.assertTrue(mandate.note.strip())
                    re.compile(mandate.prose_anchor)  # must be a valid regex
                    for fact_id in mandate.fact_ids:
                        self.assertRegex(fact_id, r"^[a-z0-9_.]+$")

    def test_the_bucket_roas_pair_specifically_is_mandated(self):
        """The motivating defect, pinned by name - the mandate that would
        have caught 2026-08-24's wrong-cause report before it shipped."""
        weekly = {
            fact_id
            for mandate in pull_specs.mandates_for("weekly-review")
            for fact_id in mandate.fact_ids
        }
        self.assertIn("roas.nonbrand.assumed_value.7d", weekly)
        self.assertIn("cpa.nonbrand.assumed_value.7d", weekly)


class DeclarePullTests(unittest.TestCase):
    """tools/declare_pull.py against the committed fixture."""

    def setUp(self):
        import declare_pull
        self.dp = declare_pull
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "2026-08-08"
        shutil.copytree(FIXTURE, self.raw)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self) -> dict:
        return json.loads((self.raw / "_manifest.json").read_text("utf-8"))

    def test_a_during_query_declares_its_window(self):
        result = self.dp.declare(
            self.raw, "campaign-cost-is-7d.json",
            "SELECT campaign.id, metrics.cost_micros FROM campaign WHERE "
            "campaign.status = 'ENABLED' AND segments.date DURING LAST_7_DAYS",
            warn=lambda m: None,
        )
        entry = result["entry"]
        self.assertEqual(entry["temporality"], "window:LAST_7_DAYS")
        self.assertEqual(entry["rows"], 6)
        self.assertFalse(entry["truncated"])
        # Operator spacing is tidied to the pack's manifest convention.
        self.assertEqual(
            entry["filter"],
            "campaign.status='ENABLED' AND segments.date DURING LAST_7_DAYS",
        )

    def test_a_between_query_declares_its_range(self):
        entry = self.dp.declare(
            self.raw, "account-spend-mtd.json",
            "SELECT segments.date, metrics.cost_micros FROM customer WHERE "
            "segments.date BETWEEN '2026-08-01' AND '2026-08-07'",
            warn=lambda m: None,
        )["entry"]
        self.assertEqual(entry["temporality"], "window:2026-08-01..2026-08-07")

    def test_an_undated_query_declares_current_state(self):
        entry = self.dp.declare(
            self.raw, "campaigns-current.json",
            "SELECT campaign.id FROM campaign WHERE campaign.status = 'ENABLED'",
            warn=lambda m: None,
        )["entry"]
        self.assertEqual(entry["temporality"], "current-as-of-2026-08-08")

    def test_a_windowed_query_on_a_current_state_filename_is_refused(self):
        with self.assertRaises(self.dp.DeclareError):
            self.dp.declare(
                self.raw, "campaigns-current.json",
                "SELECT campaign.id FROM campaign WHERE campaign.status = "
                "'ENABLED' AND segments.date DURING LAST_7_DAYS",
                warn=lambda m: None,
            )

    def test_an_undated_query_on_a_windowed_filename_is_refused(self):
        with self.assertRaises(self.dp.DeclareError):
            self.dp.declare(
                self.raw, "campaign-cost-is-7d.json",
                "SELECT campaign.id FROM campaign WHERE campaign.status = 'ENABLED'",
                warn=lambda m: None,
            )

    def test_an_unknown_filename_warns_but_writes(self):
        shutil.copy(self.raw / "campaigns-current.json",
                    self.raw / "exploratory-thing.json")
        warnings = []
        self.dp.declare(
            self.raw, "exploratory-thing.json",
            "SELECT campaign.id FROM campaign WHERE campaign.status = 'ENABLED'",
            warn=warnings.append,
        )
        self.assertTrue(any("not in any skill's pull spec" in w for w in warnings))
        self.assertIn("exploratory-thing.json", self.manifest()["files"])

    def test_a_missing_required_field_warns(self):
        warnings = []
        self.dp.declare(
            self.raw, "campaign-cost-is-7d.json",
            "SELECT campaign.id, metrics.cost_micros FROM campaign WHERE "
            "campaign.status = 'ENABLED' AND segments.date DURING LAST_7_DAYS",
            warn=warnings.append,
        )
        # The fixture's saved fieldMask has every field, so no warning here -
        # now strip the mask and re-declare.
        path = self.raw / "campaign-cost-is-7d.json"
        payload = json.loads(path.read_text("utf-8"))
        payload["data"]["fieldMask"] = "campaign.id,metrics.costMicros"
        path.write_text(json.dumps(payload), "utf-8")
        warnings = []
        self.dp.declare(
            self.raw, "campaign-cost-is-7d.json",
            "SELECT campaign.id, metrics.cost_micros FROM campaign WHERE "
            "campaign.status = 'ENABLED' AND segments.date DURING LAST_7_DAYS",
            warn=warnings.append,
        )
        self.assertTrue(any("fieldMask is missing" in w for w in warnings))

    def test_truncation_is_detected_from_a_next_page_token(self):
        path = self.raw / "campaign-cost-is-7d.json"
        payload = json.loads(path.read_text("utf-8"))
        payload["data"]["nextPageToken"] = "abc123"
        path.write_text(json.dumps(payload), "utf-8")
        entry = self.dp.declare(
            self.raw, "campaign-cost-is-7d.json",
            "SELECT campaign.id FROM campaign WHERE campaign.status = "
            "'ENABLED' AND segments.date DURING LAST_7_DAYS",
            warn=lambda m: None,
        )["entry"]
        self.assertTrue(entry["truncated"])

    def test_declaring_is_idempotent(self):
        query = (
            "SELECT campaign.id FROM campaign WHERE campaign.status = "
            "'ENABLED' AND segments.date DURING LAST_7_DAYS"
        )
        self.dp.declare(self.raw, "campaign-cost-is-7d.json", query,
                        warn=lambda m: None)
        second = self.dp.declare(self.raw, "campaign-cost-is-7d.json", query,
                                 warn=lambda m: None)
        self.assertEqual(second["changed"], {})

    def test_a_note_survives_regeneration(self):
        before = self.manifest()["files"]["campaign-cost-is-7d.json"]["note"]
        entry = self.dp.declare(
            self.raw, "campaign-cost-is-7d.json",
            "SELECT campaign.id FROM campaign WHERE campaign.status = "
            "'ENABLED' AND segments.date DURING LAST_7_DAYS",
            warn=lambda m: None,
        )["entry"]
        self.assertEqual(entry["note"], before)

    def test_audit_all_is_clean_on_the_fixture(self):
        updated, undeclared = self.dp.audit_all(self.raw, warn=lambda m: None)
        self.assertEqual(updated, [])
        self.assertEqual(undeclared, [])

    def test_audit_all_reports_an_undeclared_pull(self):
        shutil.copy(self.raw / "campaigns-current.json",
                    self.raw / "mystery-pull.json")
        updated, undeclared = self.dp.audit_all(self.raw, warn=lambda m: None)
        self.assertEqual(undeclared, ["mystery-pull.json"])

    def test_the_tool_written_entry_satisfies_the_builder(self):
        """End-to-end: a manifest whose entries came from declare_pull still
        passes the weekly builder's consolidated validation."""
        import build_weekly_review_facts as W
        self.dp.declare(
            self.raw, "campaign-cost-is-7d.json",
            "SELECT campaign.id, metrics.cost_micros FROM campaign WHERE "
            "campaign.status = 'ENABLED' AND segments.date DURING LAST_7_DAYS",
            warn=lambda m: None,
        )
        self.dp.declare(
            self.raw, "account-spend-mtd.json",
            "SELECT segments.date, metrics.cost_micros FROM customer WHERE "
            "segments.date BETWEEN '2026-08-01' AND '2026-08-07'",
            warn=lambda m: None,
        )
        W.build(self.raw, Path("unused-plan-path"))  # raises on any problem


class ConsolidatedValidationTests(unittest.TestCase):
    """Several defects are reported in ONE build, not one per build cycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "2026-08-08"
        shutil.copytree(FIXTURE, self.raw)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_defects_surface_together(self):
        import build_weekly_review_facts as W
        import pulls
        manifest = json.loads((self.raw / "_manifest.json").read_text("utf-8"))
        manifest["files"]["campaign-cost-is-7d.json"]["rows"] = 999
        manifest["files"]["account-spend-mtd.json"]["rows"] = 999
        (self.raw / "_manifest.json").write_text(json.dumps(manifest), "utf-8")
        with self.assertRaises(pulls.BuildError) as caught:
            W.build(self.raw, Path("unused"))
        message = str(caught.exception)
        self.assertIn("2 validation contradictions", message)
        self.assertIn("campaign-cost-is-7d.json", message)
        self.assertIn("account-spend-mtd.json", message)

    def test_a_single_defect_keeps_its_original_type_and_message(self):
        import build_weekly_review_facts as W
        import scopes as S
        manifest = json.loads((self.raw / "_manifest.json").read_text("utf-8"))
        manifest["files"]["campaign-cost-is-7d.json"]["rows"] = 999
        (self.raw / "_manifest.json").write_text(json.dumps(manifest), "utf-8")
        with self.assertRaises(S.ManifestError) as caught:
            W.build(self.raw, Path("unused"))
        self.assertIn("declares 999 rows", str(caught.exception))

    def test_nothing_is_written_when_validation_fails(self):
        import build_weekly_review_facts as W
        manifest = json.loads((self.raw / "_manifest.json").read_text("utf-8"))
        manifest["files"]["campaign-cost-is-7d.json"]["rows"] = 999
        (self.raw / "_manifest.json").write_text(json.dumps(manifest), "utf-8")
        try:
            W.build(self.raw, Path("unused"))
        except Exception:
            pass
        self.assertFalse(list(self.raw.glob("*-facts.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
