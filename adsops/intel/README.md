# AdRails Intel v0: local competitor Google Ads intelligence

What it does: it samples Google results pages for a tenant's own keywords in the tenant's own towns, using a licensed
SERP vendor. It records every ad it sees and turns those records into per-competitor numbers: appearance share,
position, top-of-page share, ad copy and landing pages. It then estimates each competitor's monthly spend as a
**band**, and writes a weekly markdown report. It is the method SpyFu and Semrush use, run deep on one local market.
The plan is in `../../ROADMAP-COMPETITOR-INTEL.md`.

**Status (2026-11-01): BUILT, NOT DEPLOYED.** Selftests pass with every network call mocked. Nothing is scheduled
and no money has been spent. The tenant config ships with `collector_enabled: false`.

**Founder 11-01: paid sources are ON HOLD ("hold the deposit, use the free sources").** The DataForSEO collector
stays dark. The weekly report now joins three FREE sources first (next section) and shows "paid source on hold" for
the DataForSEO sections when there is no data. The one real call so far: a 3-request Transparency smoke run
(Bahia 16 creatives, RPM Tampa St. Pete 9, Arrico 2 advertisers), matching the 10-29 manual pull.

## Rules (ethics and terms of service)

1. **We never scrape Google search results ourselves.** DataForSEO is the licensed source: we pay them per SERP, and
   they own the collection.
2. **We never scrape SpyFu or Semrush.** SpyFu numbers enter only as a hand-typed calibration CSV covering about 10
   domains from one paid month. They're compared by hand and never stored with our data (`calibrate.py`).
3. **Google Ads Transparency Center:** used minimally and politely (`transparency.py`): hard cap 40 requests per run,
   3-8 s jitter, 7-day disk cache, stop on any captcha/429, and a 6 h cooldown after a block. It captcha'd us at
   about 150 requests on 10-29. Live calls only with `--live`.
4. **Runs on the user's machine.** The tenant supplies their own DataForSEO key, and the data stays in their SQLite
   file. There is no hosted collection service, in line with the AdRails "no hosted Ads MCP" ruling.
5. **No secrets in output.** Credentials come from 1Password through `bin/op-sa`. The item is read by ID, with
   `stdin=/dev/null` and a 30 s timeout, and the values are never printed, logged or stored.
   `Creds.__repr__` and `Client.__repr__` redact them, and error text goes through `adsops.errors.scrub`.

## Free sources (added 2026-11-01; stdlib only, every network call mocked in the selftests)

| Source | Module | Table(s) | What it answers |
|---|---|---|---|
| Google Ads Auction Insights (our account, exported or pasted) | `auction_insights.py` | `ai_rows` | who beats us, per campaign / ad group / keyword |
| Google Ads Transparency Center (public) | `transparency.py` | `tr_advertisers`, `tr_creatives`, `tr_runs` | what they run: creative counts by format, first/last shown, decoded text |
| Our own Google Ads API stats (read-only GAQL) | `ownstats.py` | `os_campaign`, `os_adgroup`, `os_keyword` | our impression share, lost to rank/budget, quality score, week over week |

### 1. Auction Insights (`auction_insights.py`)

Column meanings are Google's, from "Use auction insights to compare performance"
(https://support.google.com/google-ads/answer/2579754, fetched 2026-11-01). Key points taken from that page:
the report covers Search, Shopping and Performance Max; Search campaigns have six statistics (impression share,
overlap rate, outranking share, position above rate, top of page rate, absolute top of page rate); and **the report
shows no insights when impression share is under 10%**, which is why values appear as `< 10%`. Overlap rate is how
often another advertiser's ad got an impression when ours did; position above rate is how often theirs was higher
when both showed; outranking share is how often ours ranked higher than theirs or showed when theirs didn't.

- Input 1: the Ads UI download (CSV, or the UTF-16 tab-separated file the UI often produces). Two title lines, then
  the header row `Display URL domain, Impr. share, Overlap rate, Position above rate, Top of page rate, Abs. Top of
  page rate, Outranking share` plus optional `Campaign / Ad group / Keyword` columns. The date range is read from
  the title line, or passed as `--range 2026-10-07..2026-10-31`.
- Input 2: the pasted code blocks the founder's Chrome session posts in #fable-desk (thread 1793536775.541489):
  headings like `CAMPAIGN acme_search | Oct 7-31, 2026 | 13 rows` or `TAMPA_CORE (16 rows)`, then whitespace
  separated rows. `You` is our own row. Keyword headings start with `KEYWORD`.
- `< 10%` is stored as NULL with `_raw = '<10%'` (the truth is somewhere in [0, 10%)); `--`/`-` is NULL with
  `_raw = '--'` (not applicable). Every metric has a `<metric>_raw` column, so nothing is silently coerced to 0.
- Keyed by (tenant, date range, level, scope, domain); re-importing replaces. Fixtures (Google's documented column
  names, synthetic values) are in `fixtures/auction_insights_*`; the pasted fixture is an excerpt of the real thread.
- "Above us" = overlap x position above rate. It is an approximation of the share of our impressions in which the
  competitor also showed above us, not a Google statistic.

```
python3 -m adsops.intel.auction_insights import --config tenants/acme.intel.json --file export.csv --level campaign
pbpaste | python3 -m adsops.intel.auction_insights import --config tenants/acme.intel.json --file -
python3 -m adsops.intel.auction_insights show   --config tenants/acme.intel.json --level campaign
```

### 2. Transparency Center (`transparency.py`)

Two public JSON RPC endpoints on `adstransparency.google.com/anji/_/rpc/SearchService/` (`SearchSuggestions`,
`SearchCreatives`; region US = 2840). The undocumented format was recorded live on 2026-11-01 and saved as fixtures
(`fixtures/transparency_creatives_competitor_example.json`); the module header documents the fields. A domain query to
`SearchCreatives` returns the creatives directly (no suggestion step needed), so a domain costs **one** request.
Text-ad copy sits inside the preview URL (gzip + protobuf in the `overlay`/`assets` parameter) and is decoded;
responsive ads carry only asset ids, so they show "not decodable". Creative counts are for the first page
(`page_size` 40, US-wide), not spend or impressions. Pagination is not implemented: Acme's competitors all fit
in one page (the largest, Bahia, has 16). The code records `total_reported` when Google's total exceeds the page.

Limits: max 40 live requests per run (config cannot exceed 40), 3-8 s jitter, responses cached 7 days, any
429 / redirect to `/sorry` / captcha page / non-JSON body stops the run, is written to `tr_runs`, and blocks live
runs for 6 hours. 11 competitor domains = 11 requests the first week, 0 on cache hits.

```
python3 -m adsops.intel.transparency run    --config tenants/acme.intel.json            # cache only, no network
python3 -m adsops.intel.transparency run    --config tenants/acme.intel.json --live     # <= 40 requests
python3 -m adsops.intel.transparency status --config tenants/acme.intel.json
```

### 3. Own stats (`ownstats.py`)

Reads our account through the existing adsops machinery (`adsops.config.load_config` for
`memory/adsops/config.json`, `tools.Context` -> `auth.from_config` -> `GoogleAdsClient.search`). It only ever calls
GAQL `search`; there is no mutate path. No credentials were added. GAQL field names were checked against the live
account on 2026-11-01 through the read-only google-ads MCP. Per weekly snapshot (7 days ending yesterday):
- ad group: impressions, `search_impression_share`, `search_top_impression_share`,
  `search_absolute_top_impression_share`, `search_rank_lost_impression_share`;
- keyword (`keyword_view`): the same shares plus quality score, `creative_quality_score` (ad relevance),
  `post_click_quality_score` (landing page experience), `search_predicted_ctr` (quality info has no date
  dimension, so it is a second query joined on ad group + criterion id);
- campaign: `search_budget_lost_impression_share`, impression share, rank lost, cost.
Google reports shares under 10% as 0.0999; the report renders that as `<10%`. Quality score 0 (not enough data) is
stored as NULL. `ownstats snapshot --dry-run` prints the four queries and calls nothing.

### 4. Report

`report.py` now orders the weekly report: Who beats us (Auction Insights) > What they say (Transparency) > Our own
trend > Competitor join (one row per configured competitor across all sources) > DataForSEO sections (each shows
"Paid source on hold" when there is no data) > Calibration > Assumptions.


Nothing here is registered with cron or launchd. A suggested set, each needing an observed unattended fire with a
timestamped log line before it counts as deployed:
1. `ownstats snapshot` weekly (e.g. Monday 06:30) via launchd or the adsops-credentialed environment. It needs the
   adsops config's credentials and `ADSOPS_GOOGLE_DEVELOPER_TOKEN` in the environment, exactly like the existing
   adsops guard; cron's PATH lacks `op` and `~/.local/bin`, so use absolute paths.
2. `transparency run --live` weekly (e.g. Monday 07:00), after the snapshot. Logs the JSON result line. Exit code 3
   means it was blocked (captcha/429/network); that should alert, not retry.
3. `report --write` weekly (e.g. Monday 08:00), after both.
4. Auction Insights stays manual: whoever has the Ads UI exports or pastes the table. The report prints the age of
   the latest range and flags it when older than 14 days.

## DataForSEO: the endpoints and fields we rely on (PAID, ON HOLD)

Docs fetched 2026-11-01. Auth is HTTP Basic (`login:password` from https://app.dataforseo.com/api-access).

| Use | Endpoint | Cost | Limits | Source |
|---|---|---|---|---|
| Queue a SERP (default) | `POST https://api.dataforseo.com/v3/serp/google/organic/task_post` | **$0.0006 per SERP** (Standard queue); you're charged when the task is set | ≤100 tasks per POST (error 40006 over that); ≤10,000 calls/min | https://docs.dataforseo.com/v3/serp/google/organic/task_post/ |
| List finished tasks | `GET .../serp/google/organic/tasks_ready` | free | ≤20 calls/min; returns ≤1000 tasks from the last 3 days | https://docs.dataforseo.com/v3/serp/google/organic/tasks_ready/ |
| Fetch a result | `GET .../serp/google/organic/task_get/advanced/{id}` | free for 30 days after posting | | https://docs.dataforseo.com/v3/serp/google/organic/task_get/advanced/ |
| Live SERP (`--live` flag) | `POST .../serp/google/organic/live/advanced` | **$0.002 per call** | 1 task per call | https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/ |
| Location list | `GET .../serp/google/locations/us` | free | | https://docs.dataforseo.com/v3/serp/google/locations/ |

Pricing source: https://dataforseo.com/pricing/serp/google-organic-serp-api. It says "$0.0006 in the Standard queue,
$0.0012 in the Priority queue, and $0.002 in Live mode", charged per SERP of up to 10 results. Depth is rounded up to
the next ten, and "the location, language, and device you request do not affect the price". The roadmap puts the
minimum deposit at $50, but the pricing page doesn't state one, so confirm it at signup.

**Request fields we send** (`dataforseo.build_task`): `keyword`, `location_code` (or `location_name`),
`language_code` (`en`), `device` (`mobile` or `desktop`), `depth` (10, one billed SERP), `tag` (≤255 chars), and
`priority: 2` only in `priority` mode. We send no options that cost extra (`calculate_rectangles`,
`load_async_ai_overview`, `people_also_ask_click_depth`, `max_crawl_pages`) and no search operators, which bill 5×.

**Response fields we read:**
- Top level: `status_code` (20000 = OK) and `cost` (billed USD, booked to the ledger).
- `tasks[]`: `id`, `status_code` (20100 = created on post; 20000 = done on get; 40601, 40602 and 40400 are treated
  as "not ready yet").
- `tasks[].result[]`: `keyword`, `location_code`, `datetime` (when the SERP was really fetched; stored as the
  observation time), `check_url`, `items_count`, `items[]`.
- `items[]` where `type == "paid"`: `rank_group` (ad position, stored as `position`), `rank_absolute`, `domain`,
  `breadcrumb` (display URL), `title` (headline), `description`, `url` (landing URL).
- `items[]` where `type == "organic"`: only `rank_absolute`, used to infer top of page.

**Top of page is inferred.** DataForSEO has no top or bottom field (`position` means left or right). We count a paid
item as top of page when its `rank_absolute` comes before the first organic result.

**Location codes** are Google Ads geo criteria IDs from Google's `geotargets-2026-10-06.csv`: Tampa 1015214, Wesley
Chapel 9053100, Riverview (Hillsborough County) 1015186, Brandon 1014964, New Port Richey 1015132, and Land O' Lakes
1015089. `run` refuses to post until `resolve-locations` (free) has confirmed each code against DataForSEO's own
location list.

## Cost: Acme at daily cadence

94 keywords × 6 towns × 2 devices = **1,128 SERPs per full pass**.

| Plan | SERPs/month | Cost/month |
|---|---|---|
| Full pass every day (Standard) | 34,291 | **$20.57** |
| **v0 as configured: $10 hard cap, daily rotation** | 16,659 (548/day, about 49% of combos per day, every combo every 3 days) | **$10.00** |
| Full pass weekly (Standard) | 4,907 | $2.94 |
| Full pass every day in Live mode | 34,291 | $68.58 |

The cap is hard. Before every POST (each chunk of ≤100 tasks, and each live call), the collector reads the ledger
and posts only as many tasks as the money left this month allows. Each ledger row counts the larger of our estimate
and DataForSEO's billed `cost`. A transport error books the estimate, so the cap can't be undercounted. Rotation
means a cap below full coverage still covers every keyword, town and device over a cycle, instead of resampling the
same first combos every day.

## Store (SQLite, `~/.adsops/intel/<tenant>.sqlite`)

- `observations`: one row per ad seen, with `ts, keyword, location, device, position, rank_absolute, top_of_page,
  domain, display_url, headline, description, landing_url` (plus `task_id` and `tenant`).
- `samples`: one row per SERP fetched. This is the denominator for appearance share: a SERP with no ads still counts.
- `pending`: queued tasks that haven't been collected yet.
- `ledger`: append-only spend records (`ts, month, kind, n_tasks, est_usd, actual_usd`).
- `meta`: the rotation cursor and the location-verification fingerprint.

## Analysis (`analysis.py`)

For each domain:
- appearance share overall, by keyword and by location;
- average ad rank and top-of-page share;
- first and last seen;
- distinct ad copies (headline and description) with when each was first and last seen;
- landing URLs, with query strings stripped.

**Spend band**, in USD per month:
`Σ_k volume_k × share_k × CTR(rank_k, top/bottom) × CPC_k`, computed once with every input at its low end and once
with every input at its high end. It is never a single number.

- **Volume and CPC** come from a pluggable `VolumeSource`. v0 uses `CsvVolumeSource` reading
  `tenants/<tenant>.volume.csv` (`keyword, monthly_volume_low, monthly_volume_high, cpc_low_usd, cpc_high_usd,
  source`). The shipped Acme CSV is a **PLACEHOLDER**: every row says so, uses volume 10-50 and CPC $2.50-$8.00,
  and every band computed from it is flagged "PLACEHOLDER inputs" in the report. Keyword Planner ranges replace it
  in v1.
- **CTR by rank** is a `[low, high]` table, and every value in it is an assumption:

  | Rank | Low | High |
  |---|---|---|
  | 1, top | 5% | 10% |
  | 2, top | 3% | 6% |
  | 3, top | 2% | 4% |
  | 4+, top | 1.5% | 3% |
  | Bottom | 0.5% | 1.5% |

  - The shape comes from AccuraCast's study of about 2M AdWords clicks: rank 1 at 7.11-7.94%, a steep drop to
    rank 2, and a slow decline after that. Source:
    https://www.accuracast.com/articles/advertising/adwords-clickthrough/. That data predates Google's 2016 layout
    change.
  - The level comes from LocaliQ's property-management average: CTR 6.66%, CPC $3.94. Those figures are quoted by
    secondary sources and haven't been checked against the primary report:
    https://localiq.com/blog/real-estate-search-advertising-benchmarks/.
  - The bottom-of-page row is a guess.
  - Override any of it with a `ctr_table` JSON in the config. Our own account's CTR by position is the first thing
    to calibrate it against.

## Calibration (`calibrate.py`)

- **Blind self-test.** `known_budget_usd_month: <your real budget>` must fall inside the band for `our_domains` (acmepm.example)
  once real samples and real volumes exist. The verdict is one of `inside`, `below`, `above` or `no-data`.
  `desired_state.acme_search.json`; re-check it live). Samples outside those hours will under-count us, so the
  blind test is only fair when collection fires inside the client's ad schedule.
- **SpyFu benchmark.** `tenants/<tenant>.spyfu.csv` with columns `domain,est_spend`, typed by hand. It's compared
  row by row and never ingested.

## Commands

```
cd projects/adsops-mcp
python3 -m adsops.intel --selftest                       # all modules, mocked: ALL PASS
python3 -m adsops.intel.keywords seed --config tenants/acme.intel.json \
    --structure ./reports/ \
    --town Tampa --town "Wesley Chapel" --town Riverview --town Brandon --town "New Port Richey" --town "Land O' Lakes" --write
python3 -m adsops.intel.collector --config tenants/acme.intel.json estimate | plan | status   # no network
python3 -m adsops.intel.collector --config tenants/acme.intel.json resolve-locations --write   # free API call
python3 -m adsops.intel.collector --config tenants/acme.intel.json run [--live]              # SPENDS, capped
python3 -m adsops.intel.collector --config tenants/acme.intel.json collect                    # free
python3 -m adsops.intel.report --config tenants/acme.intel.json --write   # -> ./reports/
```

Every network command checks its gates in this order:
1. Credentials exist. If they don't, it prints `no credentials yet: nothing done` and exits 0. That's the state today:
   op-sa answers, but none of the 53 items in the Ops vault is titled "DataForSEO API".
2. `run` only: `collector_enabled` is true.
3. `run` only: the locations have been verified.
4. The hard cap.

Slack posting doesn't exist in v0: `report --slack` is refused, and `slack.enabled: true` fails config validation.

## Going live (not done; each step needs its own receipt)

1. **Founder:** open a DataForSEO account, deposit funds (about $50), and save the API login and password in the
   OpenClaw Ops vault as an item titled **"DataForSEO API"**, with username = login and password = API password.
2. The first `collector ... run` or `collect` finds the item by title and writes its ID into
   `credentials.op_item_id`. After that it's read by ID only.
3. `resolve-locations --write` (free) confirms all 6 location codes.
4. Replace `tenants/acme.volume.csv` with real ranges. That means Keyword Planner, which needs the Basic API
   token, or the Ads UI Keyword Planner export typed in by hand.
5. Set `collector_enabled: true` (founder go: this is the spend switch). Do one manual `run` and `collect`, and check
   the ledger against the DataForSEO dashboard (MEASURE-THE-MEASUREMENT).
6. Schedule `run` and `collect` (collect at least 15 minutes after run), plus a weekly `report --write`, at an hour
   deployment law, it isn't deployed until an unattended fire leaves a timestamped log line.
