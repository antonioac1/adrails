# Technical SEO site audit playbook: find what blocks Google, fix it in the right order, prove it held

For the owner's Claude (or Cursor, or any MCP client). Read all of it, then run it step by step with the AdRails tools named in each step.

A technical audit answers one question: can Google discover, crawl, render and index the pages that earn money, and does each page send one clear signal? AdRails crawls the site the way Googlebot does, groups pages by URL template, and returns one prioritised table: priority (1 blocks discovery or indexing, 4 is polish), estimated hours, the issue, the specific fix, an example URL, how many pages are affected, and a status.

Ground rules:

- The audit is read-only. It sends GET requests to the site and never writes anything to it. You (or the owner's developer) make the fixes.
- Be polite. Keep the default of one request per second and at most 200 pages unless the owner asks for more. Never crawl a site you were not asked to audit.
- Fix by template, not page by page. Every row says which template (for example `/property-management/*/`) the pages belong to; one template change usually fixes all of them.
- Search Console has the last word on what Google indexed. The crawl says what Google can see; `seo_gsc_exclusions` says what Google did with it.
- Priorities are ours, not Google's. Treat hours as estimates for a developer who knows the site.

---

## Fast path: one site, first day

| Step | Do | Tool | Time | Done when |
|---|---|---|---|---|
| 1 | Run the audit with the defaults. | `seo_audit_run` (url) | 3-10 min | A run id, the summary and the top rows |
| 2 | Read every priority 1 row and the example URL of each. Open the example URL yourself and confirm the problem is real before you tell the owner. | `seo_audit_issues` (priority=1) | 15 min | Each P1 confirmed or marked as a false alarm with the reason |
| 3 | If Google credentials are connected, ask Google what it indexed and why it excluded the rest. | `seo_gsc_exclusions` | 5 min | Excluded pages listed next to what the crawl says about each |
| 4 | Turn the P1 and P2 rows into a fix list grouped by template. One line per template change. | Claude writes | 20 min | A short list the developer can work through |
| 5 | After the fixes ship, run the audit again and diff. | `seo_audit_run`, then `seo_audit_diff` | 10 min | Fixed rows listed as fixed, zero regressions |

---

## 1. Run the audit

| Step | What | Tool | Done when |
|---|---|---|---|
| 1.1 | Start from the home page URL the owner uses (https, with or without www as the site prefers). | `seo_audit_run` url=https://example.com/ | The run finishes and `notes` says what did not run |
| 1.2 | If the site uses JavaScript to build navigation or content, add `render=true` (needs Playwright installed). It loads one page per template in a sandboxed browser and compares raw HTML with the rendered page. | `seo_audit_run` render=true | `js_only_links` and `js_content_gap` either fire or appear in `passed` |
| 1.3 | For speed, add `lighthouse=true`. It measures one page per template with a local `lighthouse` CLI, or the PageSpeed Insights API with the owner's key (`seo.pagespeed_key` in the config or `PAGESPEED_API_KEY`). | `seo_audit_run` lighthouse=true | Each measured page has an LCP time and its LCP element |
| 1.4 | For Google's own view, add `gsc=true` (uses the Search Console read-only scope of the AdRails Google credential). Inspection is capped (`max_inspect`, default 50) because Google allows about 2,000 per property per day. | `seo_audit_run` gsc=true | Rows `gsc_not_indexed` / `gsc_canonical_mismatch` fire or pass |

If the run says `cap_reached`, the site has more pages than `max_pages`. Raise it (up to 2,000) only if the owner agrees; the sitemap-orphan check does not run on a capped crawl, on purpose.

## 2. Read the table in the right order

| Priority | Means | Typical rows | What to do |
|---|---|---|---|
| 1 | Blocks discovery or indexing | robots.txt blocks Googlebot, http does not redirect, 5xx pages, broken internal links, sitemap URLs Google cannot index, canonicals to dead URLs, ad landing pages that do not answer 200 | Fix this week |
| 2 | Wastes crawl or splits signals | starved hub and content pages (legal/utility pages are never counted), a template that hard-codes the same sibling link, near-duplicates that are both self-canonical, internal links with utm_ tags, missing doctype | Fix this month |
| 3 | Weakens pages, or needs a decision | thin content, schema gaps per template, slow LCP opportunities, deep pages, noindex pages that indexable pages link to (often deliberate) | Plan |
| 4 | Polish | long titles and descriptions, missing alt text, security headers, HSTS | When convenient |

Each row has `detail` (the specifics: counts and an example) and `data` (pairs, targets, per-template numbers). Use `seo_audit_issues` with `format=markdown` to paste the table into a ticket, or `format=csv` for a spreadsheet.

## 3. The checks AdRails is built for (the ads angle)

| Row | Why it matters for ads | Fix |
|---|---|---|
| `internal_utm_links` | A utm-tagged link between your own pages starts a new session and overwrites the real source, so Google Ads and ChatGPT Ads conversions get credited to "site / organic" or whatever the tag says. | Remove utm_ tags from internal links. Tag only links on other sites and in ads. If an owner deliberately uses them for funnel A/B reports, record that decision and accept the row. |
| `ad_lp_noindex` | Fine for the ads: Google Ads does not need the landing page indexed. It only needs to be deliberate. The same row says how many organic pages link to it (one row, P4). | Confirm with the owner, keep the page out of the sitemap; organic pages can link to an indexable equivalent instead. |
| `ad_lp_equity_sink` | An ad page that canonicalises to another URL but still collects organic links. | Link organic pages to the canonical page instead. |
| `ad_lp_duplicates_organic` | An indexable ad page that is a near-copy of an organic page competes with it for the same queries. | Make the ad page noindex, or canonical to the organic page, or rewrite it. |
| `ad_lp_non_200` | An ad that lands on a redirect or an error wastes the click and can get the ad disapproved. | Point the final URL at the page that answers 200. |

The ad landing pages come from `ad_urls` and, unless `ad_states=false`, from the final URLs in this deployment's Google Ads and ChatGPT Ads desired-state files.

## 4. Search Console: excluded pages and why

| Step | What | Tool | Done when |
|---|---|---|---|
| 4.1 | Inspect the latest run's pages, sitemap pages first. | `seo_gsc_exclusions` | `excluded` lists Google's coverage state beside `crawl_says` |
| 4.2 | Where Google says "Crawled - currently not indexed" on a page the crawl calls indexable, the problem is quality or duplication, not access. Look at thin content and near-duplicate rows for that URL. | Claude reads | One sentence per excluded page |
| 4.3 | Where Google picked a different canonical, align the signals (canonical tag, internal links, sitemap) or merge the pages. | `gsc_canonical_mismatch` row | Owner agrees which URL wins |

If the tool says "not connected", the credential lacks the Search Console scope or the service account is not a user on the property. The crawl findings stand on their own.

## 5. After the fixes: prove they held

| Step | What | Tool | Done when |
|---|---|---|---|
| 5.1 | Re-run with the same settings. | `seo_audit_run` | New run id |
| 5.2 | Diff against the previous run. Read `regressions` first: new noindex, pages that stopped being indexable, broken canonicals, lost internal links, LCP jumps, pages now erroring or gone. | `seo_audit_diff` | Zero regressions, fixed rows listed |
| 5.3 | Tell the owner in plain words what got fixed and what is still open. | Claude writes | Three lines, no jargon |

Runs are stored on this machine (beside the AdRails audit log, `seo-audits/<host>/`), the newest 30 per site. Nothing is uploaded.

## Limits (say these to the owner when they matter)

- A crawl sees what a crawler sees. Without `render=true` it does not run JavaScript; with it, it renders a few pages per template, not all.
- The crawl reaches only pages linked from the start page or listed in the sitemap, within `max_pages`.
- Near-duplicate detection compares visible text (5-word shingles, 80% overlap) across up to 600 indexable pages.
- Lighthouse numbers are one lab run per template on this machine, not field data.
- "Starved" means three or fewer internal links from other pages. On a very small site most pages will qualify; read it with the site's size in mind.
