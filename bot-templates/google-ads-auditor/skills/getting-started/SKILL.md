---
name: getting-started
description: "First-run guide for the Google Ads Auditor bot. Use when the user first opens the bot or asks for an audit."
---

# Getting started: Google Ads Auditor

1. Say what the bot does: "I check your Google Ads account and give you a short
   list of fixes, with the numbers behind each one. I never change your account."
2. Run `account-setup` (brand campaign IDs, target cost per conversion, the
   four negative keyword thresholds).
3. Ask for exports: Campaigns, Ads, Assets, Search terms (30 and 90 days).
   Point to `csv-import` for where each one lives.
4. Run the `csv-audit` skill. If the user has API access, also offer the full
   `run-review` audit with the verifier agents.
5. Send audit.md, attach change-list.csv, and offer to walk through the top 3.
