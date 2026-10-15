# Google Ads deep-link constants (fill in during account-setup)

Account: <your business>, customer `<10-digit customer ID>`.

    OCID = <your ocid>

How to find it: open any campaign in Google Ads in your browser and copy the
URL. The `ocid=` value in it is your OCID. It identifies the account, not your
login, so links built with it work for anyone with access.

## Template

    https://ads.google.com/aw/<view>?ocid=<OCID>&<object params>

| Target | Path | Params |
| :- | :- | :- |
| Campaign list | `/aw/campaigns` | none |
| Ad groups in a campaign | `/aw/adgroups` | `campaignId` |
| Ads in an ad group | `/aw/ads` | `campaignId`, `adGroupId` |
| Keywords in an ad group | `/aw/keywords` | `campaignId`, `adGroupId` |
| Search terms in a campaign | `/aw/keywords/searchterms` | `campaignId` |

## Params to drop from a pasted URL

`__u`, `euid`, `authuser`, `workspaceId` identify one person's login and break
the link for others. `__c`, `uscid` and `subid` are not needed.

## Known limitation

No URL parameter selects a single ad. An ad-level link goes to the ad group's
ad list, so always print the ad ID next to the link.
