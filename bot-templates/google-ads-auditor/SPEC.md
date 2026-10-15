# Template 2: Google Ads Auditor

## Promise
A one-time, evidence-backed account audit: wasted search terms, ad copy
problems, missing extensions, Performance Max brand leakage, and conversion
tracking health. Every number cites pulled data.

## Who it is for
Owners who want a second opinion, and agencies selling a fixed-price audit.

## Contents
- Skills: `account-setup` (full), `verification-protocol`,
  `conversion-measurement`, `campaign-guardrails`, `audit-ad-copy`,
  `audit-extensions`, `pmax-audit`, `mine-search-terms`, `evidence-links`,
  `run-review` (audit preset)
- Agents: `google-ads-verifier`, `google-ads-refuter`,
  `google-ads-blind-diagnostician`
- Memories: read-only rule; "a report number without a fact citation is a
  bug"; which Google Ads connector to use (named, not packed).
- Output: one composite audit report (markdown, optional Notion page) plus a
  change list CSV the owner applies.

## Data
Needs the API path (SETUP.md Part 2). Fallback: CSV mode delivers the search
terms section only and says the rest needs API access.

## Status: built and evaluated (CSV path)
- Engine: `tools/csv_audit.py`; skill `csv-audit`; template files here.
- Evals: audit section of `evals/run_evals.py`.
- Still open: API-path audit still needs a Google Ads connector or CLI; the
  facts builders still assume eight conversion actions.
