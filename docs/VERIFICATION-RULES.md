# Verification rules (VF-1 to VF-20)

The skills cite these by number. Each is a lesson learned the hard way on a
live account; the short form is the rule.

| Rule | Short form |
| :- | :- |
| VF-1 | Only the conversion actions listed in `account-config.json` drive CPA/ROAS. A pull with any other action aborts the build. |
| VF-2 | Assumed conversion values are not revenue. ROAS on them is a plan-mix signal. |
| VF-3 | Two-query rule: metrics and conversion-action segments come from separate pulls; aggregate per key before joining. |
| VF-4 | Per-headline data: compare assets only within one ad, ranked by impressions. Key on the full (ad group, ad, asset, field) tuple; watch the fan-out trap (4a). Serve data is a window, the ad is a current state; intersect them (4b). |
| VF-5 | RSA audits filter on ad type and enabled status at every level. |
| VF-6 | Trademark-restricted ad groups are out of Quality Score scope (per ad, not per campaign type). |
| VF-7 | A keyword with no Quality Score is not a finding. |
| VF-8 | Every CPA/ROAS figure carries its basis and value type in the same sentence. |
| VF-9 | Brand is not a growth lever. |
| VF-10 | Learning windows: 14 days Search, 28 days Performance Max, scoped to the change that opened them. |
| VF-11 | Available impressions are derived from impression share only with the stated formula and its caveats. |
| VF-12 | Auction Insights is not available through the API. Do not claim it. |
| VF-13 | Evidence links use only verified URL paths. |
| VF-14 | Budgets are read from the owner's stated figure, never derived. |
| VF-15 | The facts-file contract: every number in a report cites a fact id from a builder; counts cite the id, not just a matching value (15a). |
| VF-16 | Tier 2/3 verification agents are dispatched, not assumed. |
| VF-17 | A windowed metric cannot answer a current-state question. Persist an undated current-state pull. |
| VF-18 | Do not call an account "frozen" from one quiet window; check change history. |
| VF-19 | IDs can be reused across parents. Always key on the (parent, child) pair. Manifest text and fact scope must agree semantically, not byte for byte (19a). |
| VF-20 | Never assert a defect as expected behaviour. |
