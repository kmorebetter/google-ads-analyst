# Google Ads Analyst

A Grok Bot plugin that reviews a Google Ads account the careful way. It finds
wasted search terms, checks ad copy and extensions, tracks budget pacing, and
writes a weekly review. Every number in a report traces back to data that was
actually pulled. It never changes your account. It hands you reports and CSV
files, and you decide what to apply.

New here? Read [SETUP.md](SETUP.md).

## Two ways to feed it data

1. **CSV upload (no setup).** Download a report from the Google Ads website and
   upload it in chat. Works today for negative keyword mining.
2. **Google Ads API (optional).** A read-only connection with your own
   developer token. Unlocks every skill.

## What's included

| Folder | What it is |
| :- | :- |
| `.cursor-plugin/` | Plugin manifest and marketplace entry |
| `skills/` | 22 skills (list below) |
| `agents/` | Three checker agents that fact-check and attack a draft report |
| `rules/` | Data-access rule (CSV first, read-only always) |
| `tools/` | Python (standard library only) facts builders, citation checker, CSV tool, and tests |
| `bot-templates/` | Specs for three ready-made bots |
| `google-ads/account-profile.md`, `budget-plan.md`, `tools/account-config.json` | Your account facts. Ship as a SYNTHETIC example; `account-setup` replaces them |

### Skills

- Start here: `account-setup`, `csv-import`, `csv-audit`, `csv-weekly-review`
- Action: `mine-search-terms`, `weekly-review`, `audit-ad-copy`, `audit-extensions`,
  `budget-optimize`, `investigate-campaign`, `pmax-audit`, `performance-analysis`,
  `outcomes-review`, `run-review` (runs several in one session)
- Reference (loaded by the others): `account-conventions`, `conversion-measurement`,
  `campaign-guardrails`, `verification-protocol`, `search-term-methodology`,
  `ad-copy-principles`, `evidence-links`, `notion-publishing`

## Tests and evals

    python3 evals/run_evals.py     # graded scenarios for the 3 bot templates

    cd tools && python3 -m unittest

All tests run on committed synthetic data (`tools/fixtures/`). No account data
is needed or included.

## Known limits

- The facts builders expect exactly eight counted conversion actions and label
  money fields `cad`. Accounts with a different count or currency need a small
  config and code change (tracked as an open item).
- CSV mode covers negative keywords, a one-time audit and the weekly review. The other skills need the API path.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
