# Live proof: Microsoft Ads read-only monitor (2026-11-16)

Account: a real Microsoft Advertising account of a US small business (a Google-login user, so
`provider: "google"`). The account's identifiers are withheld. The campaign was switched on about an hour before this run,
so it had no traffic yet. Every number below comes from a live call, not a mock.

**What ran.** The released code path: `config.load_config` → `tools.build_server` → `tools/call` over the MCP protocol
layer, using real HTTPS to Microsoft and Google.

| Call | Result |
|---|---|
| `adsops_msads_campaigns` | 1 Search campaign, Active, $20/day, MaxClicks with an $8 max-CPC cap |
| `adsops_msads_structure` | 6 ad groups (5 Active), 72 keywords, 6 responsive search ads, 310 campaign negatives |
| `adsops_msads_performance` (3 days; campaign and keyword levels) | 0 rows, totals 0. The report job submitted and polled to Success with no file, which is Microsoft's "no data" answer |
| `adsops_msads_search_terms` (3 days) | 0 terms |
| `adsops_msads_health` | `ok: true`, 1 warning: "Active but 0 impressions in the window (new campaigns can take hours...)". Correct for a campaign live under an hour |
| `adsops_metrics_pull sources=[microsoft_ads]` | window 2026-11-09..15 pulled, 0 rows, recorded in the pull log |

**Also verified live, directly against the REST endpoints.**
- `User/Query` and `Accounts/Search` (Customer Management).
- `Campaigns/QueryByAccountId`, `AdGroups/QueryByCampaignId`, `Keywords/QueryByAdGroupId`, `Ads/QueryByAdGroupId` and
  `NegativeKeywords/QueryByEntityIds`. An earlier guess of `EntityNegativeKeywords/QueryByEntityIds` returned 404 and was
  corrected.
- Report submit and poll for all four column sets (campaign, ad group, keyword, search query). Microsoft accepted every
  column name.
- The Google-provider token (`openid email profile`) works as the Microsoft Ads bearer token.

**What the live run caught that the mocks did not.**
1. **The negatives endpoint name** (above).
2. **Silent zero.** Pinned account objects in the config were treated as allow-list strings by the metrics pull, so it
   pulled zero accounts and reported nothing. That is the worst failure for a monitor, since it looks like "no data".
   Fixed with one shared resolver and a regression test (`metrics_tools.selftest`: "pinned ids pull").
3. **DNS.** The host's system resolver held a stale negative answer for `clientcenter.api.bingads.microsoft.com` while
   `dig` resolved it. Errors now name a DNS failure and the host. Pinned account ids avoid that host entirely during
   daily monitoring.

**Not yet run live.**
- Reports with non-zero rows. The CSV parsing is covered offline with Microsoft's documented zipped-CSV format, including
  thousands separators and the BOM. It will be confirmed on the first day with traffic.
- `provider: "microsoft"` (device-code sign-in). It is covered offline. This run used the Google provider.
