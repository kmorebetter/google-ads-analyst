# Submission-ready summary (NOT submitted)

Submit in a browser at: **https://cursor.com/marketplace/publish**

## Field values

| Field | Value |
| :- | :- |
| Repository URL | https://github.com/kmorebetter/google-ads-analyst |
| Plugin name (manifest `name`) | google-ads-analyst |
| Display name | Google Ads Analyst |
| Version | 1.0.0 |
| License | Apache-2.0 |
| Author / publisher | kmorebetter |
| Short description | Find wasted ad spend, audit your Google Ads account, and get a weekly review. Works from a CSV file. Never changes your account. |
| Long description | Copy the "Long description" section of LISTING.md |
| Category | Marketing / Analytics |
| Keywords | google-ads, ppc, sem, negative-keywords, search-terms, audit, marketing, csv |
| Logo | assets/logo.svg (in repo, relative path) |
| Support | https://github.com/kmorebetter/google-ads-analyst/issues |
| Manifest | .cursor-plugin/plugin.json (single plugin, `.cursor-plugin/marketplace.json` source `./`) |
| Contents | 22 skills, 3 agents, 1 rule, Python tools (standard library only), no MCP servers, no hooks, no variables |
| Data handling | Read-only. Reads CSV exports the user uploads, or the user's own read-only Google Ads API access. Sends data nowhere. No credentials stored by the plugin. |

The form fields may differ slightly from this list; use the closest match.

## Checklist from the Cursor docs
- [x] Valid `.cursor-plugin/plugin.json`, lowercase kebab-case `name`
- [x] Clear `description`
- [x] Every skill has `name` and `description` frontmatter
- [x] Logo committed, referenced by relative path
- [x] README documents usage and setup (plus SETUP.md)
- [x] All manifest paths relative
- [x] `.cursor-plugin/marketplace.json` at repo root, unique name
- [x] Open source (Apache-2.0), public repo
- [ ] Local install test in `~/.cursor/plugins/local/google-ads-analyst`
      (needs a desktop with Cursor): reload, open Customize, confirm 22 skills,
      3 agents, 1 rule
- [ ] Confirm the name `google-ads-analyst` is not already listed

## After submitting
- Review is manual. There is no status page; Cursor replies by email.
- Every later update needs a re-review: ask Cursor for a re-index after pushing.
- Optional: also list on cursor.directory (separate, faster, self-managed).
