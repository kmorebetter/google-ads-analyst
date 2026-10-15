---
name: evidence-links
description: "Reference skill defining how this pack cites its evidence: every object-level claim carries the object name and its numeric ID, the Google Ads deep-link template and the five verified URL paths, raw IDs printed beside every link, login-specific params never emitted, and the one known gap (no single-ad URL). Constants live in account-links.md. Load before writing any report that names a campaign, ad group, ad or keyword. Use whenever linking to the Google Ads UI, citing an object, or reviewing a report's evidence trail."
---

# Evidence Links

Reference skill loaded by all eight analysis skills. It governs one thing:
how a reader of a report gets from a claim to the object the claim is about.

The constants — the `ocid` value, how it was derived and verified, and the
full list of params deliberately dropped — live in `account-links.md` at the
root of this pack. That file is authoritative. This file states the rules;
it does not restate the values, so the two cannot drift.

## Every object-level claim carries name and ID

Any claim about a specific campaign, ad group, ad, keyword or asset states
the **object name and its numeric ID**, together, in the report body. Not
one or the other. A name alone is ambiguous (this account has three
generations of naming, and identical creative shares one ad ID across ad
groups); an ID alone is unreadable.

Write it as `<campaign name> (<campaign id>)` inline, or give the table a
dedicated ID column. Verified example, from `account-links.md`:
`Google - Acme Software - Demand Gen (71000000005)`, ad group
`Lookalike (620000000047)`.

Aggregate claims — account totals, subtotals across a filter — are not
object-level and do not need an ID, but they do need the literal filter
printed beside them (`verification-protocol`'s print-the-filter rule).

## The link template

    https://ads.google.com/aw/<view>?ocid=<OCID>&<object params>

Take `<OCID>` from `account-links.md`. Five paths are verified against the
live account; nothing else is:

| Target | Path | Params |
| :- | :- | :- |
| Campaign list | `/aw/campaigns` | — |
| Ad groups in a campaign | `/aw/adgroups` | `campaignId` |
| Ads in an ad group | `/aw/ads` | `campaignId`, `adGroupId` |
| Keywords in an ad group | `/aw/keywords` | `campaignId`, `adGroupId` |
| Search terms in a campaign | `/aw/keywords/searchterms` | `campaignId` |

## Print the raw IDs beside the link

A link can rot — the `ocid` can change, a view can move, a reader can be
signed into the wrong account. The IDs cannot. So the numeric IDs always
appear as text next to the link, never only inside the href. If the link
breaks, the report still carries the data needed to find the object by hand.

## Never emit login-specific params

`__u`, `euid`, `authuser`, `workspaceId` and the rest of the reader-specific
params listed in `account-links.md` are stripped from every URL this pack
emits. **A report is read by more than one person.** A URL carrying one
person's login identifiers works for that person and fails, silently or
confusingly, for everyone else it gets forwarded to. `ocid` alone resolves
the account.

If a URL is pasted in from a browser to verify a path, strip these before it
goes into a report.

## Known limitation — there is no single-ad URL

Google Ads exposes no URL parameter that selects one ad. An ad-level link
lands on that **ad group's ad list**. So an ad-level citation always prints
the ad ID beside the link and lets the reader spot it in the list. Never
imply the link opens the ad itself.

## Search-term links — campaign-scoped only

The `/aw/keywords/searchterms?campaignId=<c>` form is verified (record
it in `account-links.md`). A search-term citation still prints the
term as text with its campaign and ad-group names and numeric IDs — a
search term has no ID of its own, and the link lands on the campaign's
search-terms list, not on one term. No other variant of the path
(all-campaigns, ad-group-scoped) has been verified; do not guess one.
