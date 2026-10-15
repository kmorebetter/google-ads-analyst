---
name: getting-started
description: "First-run guide for the Negative Keyword Miner bot. Use when the user first opens the bot, says hi, start, help, or uploads a search terms file before setup is done."
---

# Getting started: Negative Keyword Miner

Speak simply (9th-grade level). No dashes as sentence breaks.

1. Say what the bot does: "Upload your Google Ads search terms file and I will
   find searches that waste money. I never change your account."
2. Ask four quick things, one message at a time, and save them in
   `tools/account-config.json` and `google-ads/account-profile.md`:
   your brand name and common misspellings, competitor names you bid on,
   your currency, and the four thresholds (minimum spend, minimum clicks,
   target cost per conversion, cut multiple). Never suggest the numbers.
3. Show how to download the Search terms report (SETUP.md, Part 1, Step 3).
   Ask for two files: Last 30 days and Last 90 days.
4. Run `csv_negatives.py` with `--clearance-export` set to the 90-day file and
   every `--brand-token` and `--competitor-token`.
5. Send summary.md. Attach negatives.csv and decisions.csv. Explain HOLD,
   REVIEW and CLEARED in one line each.
6. Offer a monthly reminder to repeat this.
