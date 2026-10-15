---
name: ad-copy-principles
description: "Reference skill encoding ad copy best practices, messaging frameworks, and quality standards for Google Ads RSAs. Loaded by the audit-ad-copy skill. Covers headline structure, description principles, keyword insertion rules, and testing methodology. Use when writing, reviewing, or evaluating ad copy quality."
---

# Ad Copy Principles

Reference skill that defines how to write and evaluate Google Ads responsive search ad (RSA) copy. Used by audit-ad-copy and other skills that need to assess or generate ad creative.

## Headline Principles

### Coverage requirement
Every RSA needs **at least 10 unique headlines** and ideally all 15 slots filled. Fewer than 10 is a red flag — always call it out and recommend adding more. This is non-negotiable because Google's RSA system needs variety to test combinations effectively. With fewer than 10, you're severely limiting the algorithm's ability to find winning combinations.

### Headline categories (aim for coverage across all of these)
- **Keyword-aligned** (2-3): Include the primary keyword naturally
- **Benefit** (2-3): What the user gains (Save 30%, Increase Revenue)
- **Feature** (1-2): Specific product capabilities
- **CTA** (2-3): Action-oriented (Get Started, Request Demo, Try Free)
- **Proof/Credibility** (1-2): Social proof, awards, stats (#1 Rated, Trusted by 10K+)
- **Differentiator** (1-2): What separates you from competitors
- **Pricing/Offer** (1): If applicable (Plans from $29/mo, Free Trial)

If a headline set is missing entire categories, that's an audit finding worth flagging.

### Character limits
RSA headlines have a **30-character limit** (descriptions: 90). When suggesting new headlines, always verify they fit within 30 characters. Include the character count in parentheses after each suggestion so the user can see at a glance. **Count with Bash (`printf '%s' "headline" | wc -m`, under a UTF-8 locale), never by eye** — model character counting is unreliable, and an over-limit suggestion fails at import. Use `wc -m` (characters), not `wc -c` (bytes): Google's limit is characters, and any em-dash, curly quote, or accented name makes byte counts overcount (see `verification-protocol` Tier 1).

### Structure (15 headlines max per RSA)
- **Pin position 1**: Lead with the primary value proposition or keyword-aligned headline
- **Pin position 2**: Supporting benefit or differentiator
- **Pin position 3**: CTA-oriented headline

### Quality standards
- Every headline should be able to stand alone and make sense
- Include the primary keyword naturally in at least 3 headlines
- Use numbers and specifics over vague claims ("Save 30%" not "Save Money")
- Include at least 2 CTA-oriented headlines (Get Started, Request Demo, Try Free)
- Vary headline length — mix short punchy (3-4 words) with longer descriptive ones
- No headline should duplicate another's meaning, even if wording differs
- **Watch for theme clustering**: If 3+ headlines are variations on the same intent (e.g., multiple "request a demo" variants), call this out explicitly. Google will rotate headlines, but similar ones waste slots that could test different angles. Group headlines by theme and flag any theme with more than 2 headlines as over-represented

## Description Principles

### Structure (4 descriptions max per RSA)
- Lead with the strongest benefit or proof point
- Include a clear CTA in at least 2 descriptions
- Address objections where possible (free trial, no commitment, instant access)

### Quality standards
- Front-load the important information (descriptions may be truncated)
- Use the full character count — short descriptions waste real estate
- Include differentiators the competitor can't easily claim
- At least one description should include social proof or credibility (X customers, Y years, rated Z)

## Keyword Insertion Rules

- Use keyword insertion `{KeyWord:Default}` sparingly — only when the ad group is tightly themed
- Never use keyword insertion in broad match ad groups (garbage in, garbage out)
- **Never use keyword insertion on competitor/conquesting campaigns**, regardless of match type — it would insert the competitor's own brand name verbatim into the ad, which reads as confusing or as impersonation rather than as a relevant match. This is a hard prohibition, not a preference. The typical live violation: conquesting ad groups run `{KeyWord:Default Text}` over keyword sets made entirely of competitor brand names, so when insertion fires Google writes a competitor's brand into your headline. **Fix conquesting insertion before any broad-match insertion case, regardless of its smaller spend**: it is the only version of this problem that can misrepresent who the advertiser is
- Always set a sensible default that reads naturally
- **One exception on reporting, not on the rule:** do not write insertion recommendations for an ad that carries a trademark restriction. Those ad groups are out of scope for copy work entirely (see `audit-ad-copy` step 4, which owns the per-ad detection). The exclusion follows the restriction, not the campaign type: a conquesting ad with no restriction on record stays in scope, which is why the two conquesting insertion violations above are still findings. Report the restriction and stop

## Ad Strength: What It Cannot See

Ad strength is a coverage-and-variety score. It counts headlines, checks for
duplication, and rewards keyword breadth. **It does not read your copy for truth,
spelling, or policy status — whether the ad is even allowed to serve.** Never
treat EXCELLENT as reassurance, and never report an EXCELLENT rating as evidence
that an ad is fine. Two patterns seen on real accounts:

- **Policy suppression is invisible to it.** Conquesting RSAs can score
  **EXCELLENT** while a `TRADEMARK_IN_AD_TEXT_SPECIFICS` restriction suppresses
  their entire switching proposition, the whole point of the ad, and spend for
  zero conversions.
- **Spelling errors are invisible to it.** RSAs with a misspelled compliance
  term in a headline can all be rated **EXCELLENT**, shown to an audience that
  types that term daily.

Read ad strength as one input on variety, then check spelling, claims and policy
status separately. A low strength score with clean, serving copy is a smaller
problem than an EXCELLENT score on copy that is misspelled or suppressed.

## Per-Asset Data Rules

These apply to anything sourced from `ad_group_ad_asset_view` — per-headline
serve reports, "which headline is winning" questions, and any copy guidance built
on asset-level numbers. Google credits **every** asset present in a serve with
the whole ad's metrics, so asset numbers do not behave like ad numbers: one ad
group's 17 headlines sum to 117,739 impressions against its own 34,854.
`audit-ad-copy` owns the mechanics (row keys, the current-state intersect, the
checks to print); these are the rules that govern any copy judgement.

1. **Never sum metrics across assets.** Any asset-level total is a fiction.
2. **Never compare an asset in one ad against an asset in another ad**, or across
   ad groups. Serve counts reflect ad count, pinning and ad group volume.
3. **Never report a per-asset conversion count, CPA, ROAS or conversion value.**
   Forbidden, not discouraged: the resource rejects
   `segments.conversion_action_name`, so the only figure obtainable is on the
   broad goal set, never the listed conversion actions — available and wrong.
4. **The only valid unit of comparison is assets within a single ad, ranked by
   impressions.**
5. **`performance_label` is not a ranking signal for headlines** — no headline in
   this account is ever labelled LOW, so the label cannot identify a weak one.
   **Descriptions are the exception:** a LOW description is a usable signal and a
   swap candidate. Never generalise "zero LOW" past headlines.

**The usable signal is the within-ad ranking.** On a typical ad the top few
headlines take about half of the serve credits and the bottom few take almost
none. The bottom of that ranking is the swap candidate; the top 3 are
protected. A live headline with zero serves after a full 30 days is the strongest
single case but a secondary check — in practice dead headlines cluster on
low-spend ads, **rarely on a top-spending ad** — so never build copy guidance as if
dead slots are where the money is.

## Testing Methodology

When auditing or creating copy:
1. Each ad group should have at least 1 active RSA
2. RSAs should have at least 10 unique headlines and 3 unique descriptions
3. Pin sparingly — over-pinning defeats Google's optimization
4. Check ad strength indicator, but don't blindly optimize for "Excellent" — relevance and conversion rate matter more than Google's score. See **Ad Strength: What It Cannot See** above: EXCELLENT ads in this account have run a misspelled compliance claim (since removed) and a fully suppressed competitor proposition
5. Rotate new copy in without pausing proven performers — let Google's algorithm pick winners
6. **Change one thing at a time, per ad.** Recommend **at most one copy change per ad per run** — a headline swap or a description swap, never both on the same ad — so each ad's next read is interpretable. Several ads may each get one; the same ad never gets two in one run. Rank that ad's live headlines by impressions first (see **Per-Asset Data Rules**) and swap from the bottom of the ranking, never a top performer
7. **Check the headline is still there before suggesting a replacement for it.** Read the ad's current contents from an undated pull, not from a saved file or a date-windowed report. This pack once recommended fixing a headline the owner had already deleted nine days earlier
