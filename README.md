# AdRails (adrails.dev)

**Guardrails for AI agents running your ads: an open-source, self-hosted Google Ads MCP server.**

Website and docs: https://adrails.dev · MCP Registry: `dev.adrails/adrails` · plain-text summary for agents: https://adrails.dev/llms.txt

AdRails is a local MCP server for Google Ads. An agent may read campaigns, status and search
terms as often as it likes; every write has to land in a reviewed desired-state file on disk,
get diffed against live state, and clear caps that live in a policy file no AdRails tool writes.
The Python package and module are still named `adsops` — that is the name you install and run.

Three things it does differently:

- **Your keys.** It runs on your machine against your own Google Cloud project and Google Ads
  credentials, read from the sources you configure (a service-account JSON file, a static token, or
  a secret-manager lookup), and they never leave your machine. It talks to the ad platforms you
  connect, to PyPI once a day for an update check (off with `ADRAILS_NO_UPDATE_CHECK=1`), and to
  adrails.dev only when your agent calls `adsops_paid`.
- **Your caps.** Caps live in a policy file that no AdRails tool writes; with `require_plan_id`
  on, every commit must quote a fresh, single-use `plan_id` issued for that exact plan; and a
  change that adds spend exposure is refused while the scheduled guard's heartbeat is stale.
- **Free, with optional paid extras.** Everything runs free. Three hosted extras are paid per job in
  sats (on-chain) or USDC (x402), with no account: an AI visibility panel, a second opinion before a
  change goes live, and a Bitcoin timestamp of your audit log. Your agent shows you the price first;
  the tool never pays.































> **New in 0.12.0:** a guarded content pipeline. `seo_content_plan` picks topics from Search Console evidence and your last site audit, a writer model of your choice drafts (OpenRouter, any OpenAI-compatible API, or a local CLI login for subscriptions), code checks every number and claim against your approved FACTS file and verbatim source quotes, and a model from a **different family** must review it (same family, a silent model swap or no verdict = hold). Out comes an approved Markdown bundle with an `IMPLEMENT.md` for your own coding agent; publishing is a separate step behind human approval. Results are measured at 14/28/56 days into a LESSONS.md you can edit. 99 tools.

> **New in 0.11.0:** a technical SEO site audit. `seo_audit_run` crawls a site the way Googlebot does and returns one prioritised issue table (priority, hours, issue, recommendation, example URL, pages affected): indexability, sitemap vs crawl, canonicals and near-duplicates, internal PageRank and starved pages, template hard-coded links, internal links carrying utm_ tags, your ad landing pages, schema coverage per template, and optionally JS render parity, Lighthouse per template and Search Console URL Inspection. Runs are stored and diffed. Read-only towards every site.

> **New in 0.10.0:** the measurement loop. `adsops_metrics_pull` keeps a daily local history of Google Ads, Google Search Console, AI-visibility panels and leads; `adsops_search_console` finds the cheapest organic wins and the search terms you pay for while already ranking top-3; `adsops_impact` answers "did that change work?" against a counterfactual with a 90% range and a placebo test. All local, all read-only towards Google. 87 tools.

## Common questions

**What is the safest way to let an AI agent manage Google Ads?**
Give the agent a write path that cannot exceed limits it doesn't control. With AdRails, every change is a git-committed desired-state file, then `adsops_plan` (a dry-run diff), then Google's validate-only check, then a commit with a single-use plan ID. Caps live in a policy file no AdRails tool can write, and an hourly guard can only pause. Checklist: [Google Ads MCP safety: 7 controls](https://adrails.dev/tools/google-ads-mcp-safety/).

**Is there an open-source Google Ads MCP server that runs locally?**
Yes. AdRails is Apache-2.0, runs on your machine over stdio with your own Google Cloud project and keys, and installs with `pip install adrails`. The server itself is never hosted; only the optional paid extras run on adrails.dev.

**How do I connect Claude Code, Cursor or Grok to Google Ads?**
`pip install adrails`, then `adrails init --dir DIR --customer-id ID --campaign-id N --sa KEY.json` and `adrails doctor`, then `claude mcp add adrails -- adrails --config DIR/config.json` (Cursor and Grok use the same `mcpServers` entry). See [install guides](https://adrails.dev/docs/).

**Can I require human approval before an AI agent changes my ad budget?**
Yes. Set `approvals.mode` to `exposure` (budget increases, enabling, end-date extensions) or `all`. The agent calls `adsops_approval_request`, a human approves with an HMAC-signed token that expires, and a commit without an approved `plan_id` is refused.

**How do I audit every change an AI agent makes to my ad accounts?**
Every mutating call, including refused ones, is appended to a SHA-256 hash-chained log with secrets redacted. `adsops_audit_verify` detects edited, deleted or reordered entries. `adsops_rollback` rebuilds any earlier committed state, and `adsops_drift` flags edits made outside AdRails.

**How do I check whether Google Ads conversion tracking actually works?**
`adsops_conv_audit` scores the account 0-100: no primary goal, auto-tagging off, primary actions with zero conversions, double counting, lead actions counting every click, primary actions whose goal is not biddable, spend without conversions, and smart bidding without enough data.

**How can an agent check if ChatGPT, Claude and Perplexity can crawl and cite my site?**
`aeo_site_audit` checks robots.txt rules for 18 AI and search crawlers (RFC 9309), llms.txt, the sitemap, noindex and canonical tags, and whether the body text is readable without JavaScript. Free browser version: [AI crawler checker](https://adrails.dev/tools/ai-crawler-checker/).

**How do I validate or build an llms.txt file?**
`aeo_llms_txt` validates a live `/llms.txt` against the llmstxt.org format, or builds one from a page list.

**How do I track whether AI assistants recommend my business?**
Your agent asks ChatGPT, Claude or Perplexity your category questions and passes the answers to `aeo_visibility_record`. It scores mentions, rank against competitors, citations and sentiment, and reports the change since the last run. AdRails makes no LLM calls itself.

**How do I track response rates and cost per lead for postcards?**
`mail_codes_assign` gives every piece a phone-readable tracking code and a UTM-tagged URL. `mail_scans` counts real scans from your own server logs (bots and your own test scans removed). `mail_response` reports rates with 95% intervals, and `channel_cac` puts direct mail cost per lead next to Google Ads. Free browser tools: [postcard QR codes](https://adrails.dev/tools/postcard-qr-codes/), [response calculator](https://adrails.dev/tools/direct-mail-response-calculator/), [holdout calculator](https://adrails.dev/tools/direct-mail-holdout-calculator/).

**Does AdRails support ChatGPT Ads (the OpenAI Ads API)?**
Not yet. The adapter is in development; it will use the same plan, validate and commit path, with OpenAI's Bulk API `validate_only` and `partial_failure` forced off.

## Testing

```bash
python3 -m adsops --selftest           # 4,389 checks, everything mocked (site audit + content pipeline run against loopback fixtures)
python3 tests/qa_invariants.py         # 200 random sequences against a simulated Google Ads account
python3 tests/qa_inputs.py             # hostile policy/state/config inputs (NaN, Infinity, bad dates, bad UTF-8, ...)
python3 tests/qa_secrets.py            # canary tokens injected everywhere; none may reach output or logs
```
The QA suites were written adversarially by a separate agent with no write access to `adsops/`, and every finding was fixed
or documented as intended behaviour.

## What AdRails cannot guarantee

Read this before pointing it at a live account.

- **It is a threshold with automatic pause attempts, not a hard spend ceiling.** The guard
  compares spend with numbers you wrote and issues pause operations. That is containment, not
  billing control.
- **Google can deliver up to 2x the average daily budget on a given day.** A daily budget is an
  average, not a maximum, and a pause is an API call that can be rejected or land late.
- **Reporting is delayed.** The spend figures AdRails reads are minutes to hours behind delivery.
- **The guard only acts when it is scheduled and the machine is awake.** A chat-session MCP
  process is not a monitor: while the machine sleeps no checks run, spend keeps moving, and the
  heartbeat goes stale.
- **An agent with shell access as the same user can bypass any local tool.** AdRails constrains
  the tool calls it serves, not the machine underneath. Sandbox the agent if that matters to you.
- **Wrong negative keywords can suppress good traffic.** Negatives are append-only here, so a
  bad one is also awkward to withdraw.
- **You are responsible for your account and your spend.** Keep platform-side billing limits as
  the real ceiling.

## Quickstart

**1. Install.**

```bash
# from PyPI (https://pypi.org/project/adrails/)
pipx install adrails     # console script: adsops-mcp
# or run without installing:
uvx --from adrails adsops-mcp --config /absolute/path/config.json
```

No other dependencies. Service-account tokens are signed with your system `openssl`
when `google-auth` isn't installed (install `google-auth` if you prefer it).

The core is stdlib-only. Check it offline (everything mocked, no network):

```bash
python3 -m adsops --selftest      # or: deploy/smoke_docker.sh
```

**2. Write a config.** Every account, credential source and path lives here; paths are
`~`-expanded at load time.

```json
{
  "tenant": "your-tenant",
  "meter_log": "~/.adsops/meter.jsonl",
  "audit_log": "~/.adsops/audit.jsonl",
  "plan_ledger": "~/.adsops/plans.jsonl",
  "lock_dir": "~/.adsops/locks",
  "guard_heartbeat": "~/.adsops/guard.heartbeat",
  "credentials": {"google_ads": {"type": "service_account",
      "sources": [{"kind": "file", "path": "~/.adsops/sa.json"}]}},
  "google_ads": {"api_version": "v24", "end_field": "end_date_time", "login_customer_id": null},
  "commit_policy": {"require_clean_git": true, "allow_commit": true, "allow_set": true},
  "accounts": [{"key": "main", "platform": "google_ads", "customer_id": "000-000-0000",
      "campaigns": [{"key": "search_core", "state_file": "~/ads/search_core.json",
                     "policy_file": "~/ads/search_core.policy.json"}]}]
}
```

`commit_policy` defaults to `require_clean_git: true` and `allow_commit: false`, so commits and
`adsops_set` stay off until you turn them on.

**3. Write the policy file** for the campaign (next section), and commit both files to git so
`git diff` is the change review.

**4. Register the server** in your MCP client. Diagnostics go to stderr; the server speaks
JSON-RPC 2.0 over stdio, one object per line.

```json
{
  "mcpServers": {
    "adrails": {
      "command": "python3",
      "args": ["-m", "adsops", "--config", "/absolute/path/config.json"]
    }
  }
}
```

**5. Schedule the guard.** A chat session cannot watch spend, so the pause-only spend check runs
from your OS scheduler every 15 minutes. Templates for launchd, systemd and cron live in
`deploy/`; render the placeholders (`__PYTHON__`, `__CONFIG__`, `__PKG__`, `__LOGDIR__`) and read
`deploy/GUARD.md` before installing:

```bash
PY=$(command -v python3); CFG=/absolute/path/config.json
PKG=/absolute/dir/containing/adsops; LOGDIR="$HOME/.adsops/logs"
mkdir -p "$LOGDIR"
sed -e "s|__PYTHON__|$PY|g" -e "s|__CONFIG__|$CFG|g" \
    -e "s|__PKG__|$PKG|g" -e "s|__LOGDIR__|$LOGDIR|g" \
    deploy/launchd/com.adsops.guard.plist.template \
    > ~/Library/LaunchAgents/com.adsops.guard.plist
plutil -lint ~/Library/LaunchAgents/com.adsops.guard.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.adsops.guard.plist
```

The command all three units run is `PYTHONPATH=$PKG $PY -m adsops --config $CFG guard-all`.

## Use it from Grok, Claude Code, Cursor or any MCP client

AdRails is a local stdio server, so every client registers the same command. There is no hosted URL, by design.

```bash
pip install adrails

# Claude Code
claude mcp add adrails -- python3 -m adsops --config /absolute/path/config.json
# Grok CLI (it also reads ~/.claude.json, .cursor/mcp.json and .mcp.json)
grok mcp add adrails -- python3 -m adsops --config /absolute/path/config.json
```

Cursor or a project `.mcp.json`:

```json
{"mcpServers":{"adrails":{"command":"python3","args":["-m","adsops","--config","/absolute/path/config.json"]}}}
```

Claude Desktop: install the `.mcpb` bundle from the [latest release](https://github.com/antonioac1/adrails/releases/latest).
More: https://adrails.dev/docs/mcp-clients/

## Tools

| Tool | What it does |
| --- | --- |
| `adsops_campaigns` | Lists the campaigns this deployment may operate: keys, accounts, state files, and the active commit policy. |
| `adsops_status` | Live read for one campaign: status, end date, daily budget, spend today and lifetime, negative count, whether caps come from the policy file, and the guard heartbeat's age. When the state file manages URL fields it also returns `urls`: the campaign final URL suffix and, per ad group, its custom parameters, final URL suffix and the final URLs of each enabled ad. |
| `adsops_terms` | Clicked search terms for the last N days (1–90, default 14): term, matched keyword, clicks, cost. |
| `adsops_plan` | Pure diff of the desired-state file against live state; writes nothing. Covers budget, status, end date, negatives and the URL fields below. Returns the operations, notes (one `url: ...` line per URL change), `ops_count`, the exposure increases it found, and a single-use **`plan_id`** with its expiry. |
| `adsops_apply` | Validate-only unless `commit=true`. A commit needs `commit_policy.allow_commit`, a clean git-tracked state file, a fresh guard heartbeat when the change adds exposure, and the **`plan_id`** from `adsops_plan` when the policy requires it. |
| `adsops_set` | Proposes a budget / status / end-date / negatives change with a short `reason`; checks it against policy caps and live state, writes the state file and commits it to git. `dry_run=true` returns a **`plan_id`** instead of writing; `apply=true` needs that `plan_id` when the policy requires it. |
| `adsops_guard` | Reads spend and pauses the campaign if the daily or lifetime threshold is crossed. Pause only: it never enables, raises a budget or edits keywords. Validate-only unless `commit=true`. |
| `adsops_usage` | Metering summary for the tenant: calls, errors, units, ops and median latency per tool. |
| `adsops_negatives_suggest` | Read-only. Classifies the last N days (1–90, default 30) of search terms (renter, job, HOA/condo, vacation, DIY/software, out-of-area, competitor brand) with deterministic rules and an optional local-model hook you supply, and proposes PHRASE negatives with cost and click evidence. Skips terms and phrases your existing negatives already cover, never suggests words on your protect list, and returns a ready-to-paste `negatives_add` patch plus a basis note. It writes nothing: the patch goes through the desired-state file like any other change. |
| `adsops_lp_diagnose` | Read-only. Per ad group, Google's quality-score components (expected CTR, ad relevance, landing page experience) plus a plain HTTP check of each final URL: status, response time, bytes, and whether the title and H1 echo the ad group's keywords and town. Returns plain-English findings, worst first. No browser and no JavaScript, so a headline built by script shows as missing. |
| `adsops_competitors_report` | Read-only. The AdRails Intel weekly report from the free sources (Auction Insights you imported, cached Transparency Center data, your own stats snapshots). Needs `intel_config` in the config; makes no network call. |
| `adsops_auction_insights_import` | Imports an Auction Insights CSV/TSV export or a pasted table into the local intel database (replaces rows for the same date range). Writes only that local SQLite file. Needs `intel_config`. |


### New in 0.4.0: enterprise controls, automation and an AEO toolkit (28 tools)

| Tool | What it does |
| --- | --- |
| `adsops_conv_audit` | Read-only. Audit conversion tracking for the campaign's account: primary goals, dead tags, double counting, auto-tagging, lead counting, smart bidding without enough conversions. Returns a 0-100 score, grade, and ranked findings with fixes. Run this before trusting any CPA or optimising toward conversions. |
| `adsops_diagnostics` | Read-only PPC diagnostics bundle for one campaign: wasted spend (zero-conversion search terms), impression share lost to budget vs rank, cost-weighted quality score with weak components, and day-over-baseline anomalies (z-score). Returns a ranked headline list plus details. |
| `adsops_rsa_validate` | Pure check, no account access. Validate a Responsive Search Ad draft against Google's limits and common disapproval causes (lengths incl. double-width chars, pins, duplicates, caps, punctuation, phone numbers, unsubstantiated superlatives, banned phrases) before any write. Returns errors, warnings and fixes. |
| `adsops_rules_check` | Read-only. Evaluate the automation rules in the campaign's POLICY file (founder-owned, agents can't edit it) against live metrics. Returns PROPOSALS and a desired-state patch; nothing is applied. Rules can only pause, add negatives, alert, or move budget inside the policy caps. |
| `adsops_monitor_check` | Read-only. Run monitors (CPA spike, pacing over/under, month forecast, zero impressions, disapprovals, budget-limited) over live snapshots of every configured campaign (or one). New alerts are de-duplicated for 24 h; with deliver=true they are posted to the webhook named in config alerts.webhook_env. |
| `adsops_brief` | Read-only. A short markdown brief across every configured campaign: spend today, conversions, blended CPA and the top issues from the monitors. Built for a daily Slack/email post. |
| `adsops_fleet` | Read-only. Fleet view across every configured account and campaign: spend, budget, conversions and CPA by account and platform, plus the AGGREGATE cap check (config fleet_caps), which assumes platforms may deliver up to 2x the daily budget. |
| `adsops_realloc_suggest` | Read-only. Recommend moving daily budget from weak campaigns to budget-limited strong ones inside each policy's caps. The total never rises. Returns moves plus per-campaign desired-state patches to review. |
| `adsops_drift` | Read-only. Who changed the account outside AdRails? Reads Google's change history for the last N days (max 30) and flags out-of-band edits (UI, scripts, auto-applied recommendations), highlighting budget, status and bidding changes. |
| `adsops_keyword_ideas` | Read-only. Keyword Planner ideas (searches, competition, top-of-page bid range) for seed keywords and/or a URL, clustered by theme with an intent label. Needs a Basic-access developer token. |
| `adsops_offline_conv_plan` | Build (never send) an offline / enhanced conversion upload from CRM rows. Emails, phones and names are normalised and SHA-256 hashed LOCALLY, so raw PII never leaves this machine. Returns a redacted preview and counts; validate_only stays true. |
| `adsops_history` | Read-only. Git history of the campaign's desired-state file: every change agents or lanes made, newest first. |
| `adsops_rollback` | Undo: build the desired state as of an earlier commit (guards, account and campaign ids are never reverted, and negatives are never removed). With commit=true it writes and git-commits the state file; then run adsops_plan and adsops_apply as usual. Nothing reaches the ad account from this tool. |
| `adsops_approval_request` | Ask a human to approve a plan before it can be committed (when config approvals.mode is on). Returns approve/reject tokens to send to the approver; the tokens are HMAC-signed and expire. |
| `adsops_approval_decide` | Record a human decision using a signed approve/reject token. Forged, expired or reused tokens are refused. |
| `adsops_approvals` | List approval requests (newest first), optionally by status. |
| `adsops_audit_verify` | Verify the tamper-evident audit chain of every mutating call (hash-linked entries). Returns ok, entry count, the first bad entry if any, and a head digest to publish so later truncation is detectable. |
| `adsops_audit_tail` | The last N audit-chain entries (redacted), newest first. |
| `adsops_sbom` | Supply-chain manifest of this AdRails install: a CycloneDX 1.5 SBOM and a SHA-256 manifest of every package file, so you can verify the code you run is the code that was released. |
| `adsops_whoami` | This server's identity and controls: version, principal and roles (RBAC), approval mode, audit chain path, and whether a guard heartbeat is configured. |
| `aeo_site_audit` | Read-only. Can AI answer engines reach this site? Checks robots.txt rules for 18 AI and search crawlers (GPTBot, OAI-SearchBot, ChatGPT-User, ClaudeBot, Claude-User, PerplexityBot, Google-Extended, Bingbot, Applebot...), llms.txt, the sitemap, and the homepage's noindex/canonical/body-text signals. Returns a 0-100 score and ranked fixes. |
| `aeo_page_audit` | Read-only. Answer-readiness of one page: answer-first paragraph, question headings, FAQ, structured data, specifics, freshness, citations, scannability, plus a JSON-LD audit (required/recommended fields per type) and crawler directives. Returns scores, extractable answer blocks, and missing questions. |
| `aeo_llms_txt` | Read-only. Validate a site's /llms.txt (llmstxt.org format), or build one: pass pages [{url,title,description,section}] with title and summary and it returns a ready file that passes validation. |
| `aeo_schema_generate` | Pure. Generate valid JSON-LD for a page: kind = faq (qas [[q,a]...]), software (name,url,description,price), org (name,url,logo,same_as) or breadcrumbs (trail [[name,url]...]). Returns the object, its own audit, and a ready <script> tag. |
| `aeo_visibility_record` | Record an AI share-of-answer panel. YOU (the calling agent) ask each engine the prompts and pass the answers here [{engine,prompt,text,citations}]; AdRails scores mentions, rank vs competitors, citations and sentiment, stores the run, and returns the score plus the change since the last run. |
| `aeo_indexnow` | Tell Bing, Yandex, Seznam and Naver (IndexNow) that URLs changed. Checks the key file at https://host/<key>.txt first. submit=false (default) only validates. Optionally pass sitemap_since (ISO date) to send every sitemap URL modified since then. |
| `aeo_ai_traffic` | Read-only. Measure AI-driven traffic from a local web-server log (nginx combined or JSON lines): referrals from ChatGPT, Perplexity, Claude, Gemini and Copilot with landing pages, and AI crawler fetches split into verified / unverified / spoofed (by the providers' published IP ranges and scanner paths). |
| `aeo_content_gaps` | Read-only. The ads-to-answers bridge: turn the campaign's real paid search terms into the questions people ask, check which of your pages already answer them, and return content briefs for the biggest gaps (facts only from site_facts, never invented). |

**Hardening applied to every tool** (`adsops/enterprise_tools.py:harden`): role checks (`rbac`, off until enabled, then deny-by-default),
an approval gate on commits (`approvals.mode`: `off` | `exposure` | `all`; HMAC-signed, expiring tokens; secret from env
`ADRAILS_APPROVAL_SECRET`, 32+ chars), and a SHA-256 hash-chained audit log of every mutating call, refused ones included
(`audit_chain`, default `~/.adsops/audit-chain.jsonl`). Monitors deliver to the webhook URL in the env var named by `alerts.webhook_env`.
Nothing here adds spending authority: account changes still go committed state file → `adsops_plan` → `adsops_apply`.

New CLI: `adsops-mcp init --dir DIR --customer-id ID --campaign-id N --sa KEY.json` scaffolds a campaign with a safe policy
(caps = today's budget, commits off, enabling forbidden) and makes the first git commit; `adsops-mcp --config DIR/config.json doctor`
reports what's missing, with a fix for each failed check.

Optional per-campaign config for the new tools: `negatives` (`protect`, `out_of_area`, `competitor_brands`, `home_state`, `llm_command`) and `landing` (`towns`); top-level `intel_config` points at your AdRails Intel tenant file (see `adsops/intel/README.md`).

## Technical SEO site audit (0.11)

An agent says "audit this site" and gets the table a senior technical SEO would hand over. Read-only: GET requests only, nothing is
ever written to the site. Playbook for the agent: `adsops_playbook` name=`seo-audit`.

| Tool | What it does |
| --- | --- |
| `seo_audit_run` | Crawl one site (Googlebot user-agent by default, configurable; sitemap seeding; robots.txt honoured, also on every redirect hop; redirect chains recorded; same host and its www twin only; 1 request/second, 200 pages by default, 2,000 max), group pages by URL template, run every check, store the run and diff it against the previous one. Optional `render=true` (JS render parity in a sandboxed Chromium, needs `pip install adrails[render]`), `lighthouse=true` (one page per template with a local `lighthouse` CLI, or the PageSpeed API with your key), `gsc=true` (Search Console URL Inspection and clicks). |
| `seo_audit_issues` | The stored issue table as JSON, Markdown or CSV, filtered by priority or issue code, plus the schema coverage matrix (template x type). |
| `seo_audit_diff` | New and fixed issues between two runs, and regressions: new noindex, pages that stopped being indexable, broken canonicals, lost internal links, LCP jumps, pages now erroring or gone. |
| `seo_gsc_exclusions` | Search Console's view of a stored run: which pages Google excluded and why (coverage state, Google-selected canonical), next to what the crawl says about each page. |

What the checks cover: http to https and www consolidation; a true 404 test; robots.txt for Googlebot and Bingbot and its Sitemap line;
the sitemap against the crawl (non-indexable URLs in the sitemap, indexable pages missing from it, orphans); the share of indexable pages;
internal links to redirects, errors and http; titles, meta descriptions and H1s (missing, duplicate, too long, multiple);
near-duplicates (5-word shingles) and thin pages; canonical and noindex errors, including near-duplicate pairs that are both
self-canonical; hreflang return links and codes; missing doctype and html lang; click depth; internal PageRank, pages with three or fewer
internal links (hubs first) and templates that hard-code the same sibling link; empty and generic anchors; nofollow internal links;
**internal links carrying utm_ or click-id parameters** (they overwrite the real traffic source, so ad conversions get mis-credited);
an **ad landing-page guard** over the final URLs in your Google Ads and ChatGPT Ads state files (non-200, noindex, canonical elsewhere,
indexable copies of organic pages, organic link equity flowing into noindex ad pages); schema coverage per template; HSTS and security
headers. With `lighthouse=true`: LCP per template, the LCP element, and whether the preloaded image is the LCP image.

Safety: public addresses only, checked once per hop and the connection pinned to that address (no DNS rebinding), default ports only,
3 MB and a deadline per response, a wall-clock limit per crawl, no execution of fetched content (the optional browser runs in a child
process with a time limit, WebSockets and non-GET requests refused). Runs are stored in `seo-audits/<host>/` beside your audit log
(`seo_audit_dir` overrides), newest 30 per site, mode 0600. CLI: `adsops-mcp site-audit --url https://example.com/ [--max-pages N]
[--render] [--lighthouse] [--gsc] [--format md|json|csv]`.

Limits: without `render=true` no JavaScript runs; the crawl reaches only linked or sitemap-listed pages within `max_pages`;
near-duplicates are compared across up to 600 indexable pages; Lighthouse is one lab run per template, not field data.

## Guarded content pipeline (0.12)

The selling point is the guardrails, not the writing: **it refuses to hand over a page that states something it cannot back up.**
Same promise as the ads side: your keys, your server, your caps, rules in code, approvals, an audit log. Playbook for the agent:
`adsops_playbook` name=`seo-content`. Setup: a site profile and an approved FACTS file (examples in `examples/seo-server/`), and
`"seo_content": {"profiles": {"mysite": "/path/site-profile.json"}}` in the config.

| Tool | What it does |
| --- | --- |
| `seo_content_facts` | Proposes a FACTS file from the site's own pages for a human to approve. Never approved automatically; without an approved file the pipeline refuses to draft. |
| `seo_content_plan` | Topics from evidence: Search Console queries ranking 8-40, question queries, thin or starved pages from the last `seo_audit_run`. Prefers extending a ranking page over a new URL; refuses brand/competitor queries and doorway patterns (town or keyword swaps). |
| `seo_content_draft` | The writer chain drafts (fallback to the next configured writer on error or truncation, recorded), then the deterministic checks run, with one fix round. |
| `seo_content_review` | Mandatory adversarial review by a model of a different family. Sources re-fetched, quotes re-found, reviewer edits re-checked. Approve = an approved bundle. |
| `seo_content_bundle` | Lists, reads and verifies bundles: `page.md`, `IMPLEMENT.md`, `review.json`, hashes. |
| `seo_content_publish` | Request approval, dry-run or publish one bundle: static directory or a git commit on a new branch (never pushed). |
| `seo_content_results` | Impressions, clicks and position per day, 28 days before vs 14/28/56 days after each piece, and a scoreboard by content type and query class. |
| `seo_content_lessons` | Reads LESSONS.md: the evidence-backed lines the planner and writer use, plus the operator's own notes. |

**Writers and reviewers.** Three backend kinds: `openrouter` (reported cost counts toward the cap), `openai_compatible` (base URL +
key env name: z.ai GLM, DeepSeek, OpenAI, a local server) and `command` (a local CLI you configure, for subscription logins; the
prompt goes through a 0600 temp file or stdin, never argv, and API keys are removed from its environment). Keys are read from the
environment by name and never logged. Per run: call, token, dollar and time caps; truncated output is an error.

**Independence, enforced in code.** The tool refuses to start if any reviewer is the same model family as any configured writer.
At run time the model that actually answered is read from the response (or the CLI's JSON) and must be the model that was asked
for (a dated suffix is fine); a different model, a reported fallback, tool use, an unknown family or a writer-family answer is
refused and recorded. No parseable verdict is a hold.

**The checks** (before any reviewer, and again after every reviewer edit): every number, percentage, dollar amount and year traced
to FACTS or to a verbatim quote from a source fetched in that run (spelled-out statistics refused); banned phrases, excluded areas,
competitor names, superlatives, URLs in text, raw HTML; near-duplicates against every sitemap page (any fetch error = hold);
minimum substance; for page extensions, a retention floor and a maximum number of changed live sentences, each declared; JSON-LD,
author and internal links.

**Publishing** goes through the existing approvals queue and audit chain: human approval per bundle by default; `auto` only with
daily and weekly caps and a time window; a failed post-publish check writes a HOLD file that stops all publishing until a person
removes it; `state/content-STOP` stops everything. **Learning** is a visible file: lines with their evidence, capped, editable,
and deleted lines never return. No self-modifying code, no hidden weights.

One shot for cron or systemd (no ports): `adsops-mcp seo-run --profile site-profile.json --config config.json [--stage
run|plan|results|publish|facts]`. Limits: drafts are only as good as the FACTS file and the sources you allow; the checks trace
numbers and named things, they cannot judge every qualitative claim (that is the reviewer's job, and its findings are recorded);
results are before/after comparisons, not controlled experiments.

## The policy file

One policy file per campaign, referenced by `policy_file` in the config. AdRails only ever reads
it: no tool writes caps, and `adsops_set` refuses `guards` and `allow_enable` outright. If the
state file also carries caps they are ignored — the policy file always wins, and `adsops_status`
says so.

```json
{
  "customer_id": "000-000-0000",
  "campaign_id": 1000000000,
  "guards": {
    "max_daily_budget_usd": 40,
    "min_daily_budget_usd": 5,
    "max_end_date": "2026-12-31",
    "lifetime_spend_pause_usd": 900,
    "daily_spend_pause_usd": 60,
    "max_ops_per_run": 12
  },
  "allow_enable": false,
  "max_guard_age_min": 30,
  "require_plan_id": true,
  "plan_ttl_s": 900
}
```

| Field | Meaning |
| --- | --- |
| `customer_id` | Account the campaign lives in; compared with the state file after stripping dashes. A mismatch is refused. |
| `campaign_id` | The one campaign this policy governs; must match the state file exactly. |
| `guards.max_daily_budget_usd` | Required. Upper bound of the daily budget, and the budget ceiling checked at plan time. |
| `guards.min_daily_budget_usd` | Optional. Lower bound of the daily budget; must not exceed the maximum. |
| `guards.max_end_date` | Required, `YYYY-MM-DD`. Latest end date any plan may set. |
| `guards.lifetime_spend_pause_usd` | Required. Lifetime spend at which the guard issues a pause. |
| `guards.daily_spend_pause_usd` | Required. Today's spend at which the guard issues a pause. |
| `guards.max_ops_per_run` | Required integer. Most operations one run may emit; more is refused. |
| `allow_enable` | Default `false`. Whether a plan may ever set the campaign to `ENABLED`. |
| `max_guard_age_min` | Default `null` (check off). When set, any change that adds spend exposure is refused unless the guard heartbeat is younger than this. |
| `require_plan_id` | Default `false`. When `true`, apply/commit and `adsops_set` with `apply=true` must quote a `plan_id`. |
| `plan_ttl_s` | Default `900`, range 60–86400. How long an issued `plan_id` stays usable; each one is consumed once. |

A missing or malformed field is an error that names the field, not a silent default.

## Desired-state file

One JSON file per campaign, referenced by `state_file`. It says what the campaign should look
like; the server computes the diff.

```json
{
  "customer_id": "000-000-0000",
  "campaign_id": 1000000000,
  "campaign_name": "Search - Core",
  "status": "PAUSED",
  "end_date": "2026-11-30",
  "daily_budget_usd": 20,
  "negatives_add": [
    {"text": "free", "match_type": "BROAD"},
    {"text": "cheap", "match_type": "PHRASE"}
  ],
  "guards": {
    "max_end_date": "2026-12-31",
    "min_daily_budget_usd": 5,
    "max_daily_budget_usd": 40,
    "lifetime_spend_pause_usd": 900,
    "daily_spend_pause_usd": 60,
    "max_ops_per_run": 12
  }
}
```

| Field | Meaning |
| --- | --- |
| `customer_id` | Account the campaign lives in; dashes are stripped before use. |
| `campaign_id` / `campaign_name` | The one campaign this file owns. Both must match live state or the run aborts. |
| `status` | Desired status, `ENABLED` or `PAUSED`. `ENABLED` additionally requires `allow_enable` in the policy. |
| `end_date` | Desired end date, `YYYY-MM-DD`, bounded by `guards.max_end_date` and not in the past. |
| `daily_budget_usd` | Desired daily budget, bounded by the min/max guard band. |
| `negatives_add` | Append-only list of `{text, match_type}`; match type is `BROAD`, `PHRASE` or `EXACT`. Nothing here removes a keyword. |
| `guards` | Read only when the campaign has no `policy_file` (legacy mode). With a policy file configured, these are ignored. |

Keep these files in git. That is what makes "reviewed desired state" real: `git diff` is the
change review, and `require_clean_git` makes the server refuse to act on uncommitted edits.






















## ChatGPT Ads

AdRails also runs OpenAI (ChatGPT) Ads accounts through the same plan, validate and commit path. The tools
are named `adsops_openai_*`. Full guide: [docs/OPENAI-ADS.md](docs/OPENAI-ADS.md).

- **One API key per ad account.** It comes from 1Password, a file or an env var. Any number of `openai_ads` accounts can sit in one config.
- **New campaigns, ad groups and ads are always created paused.** Each create carries an `Idempotency-Key`, so a retry cannot duplicate it.
- **Caps live in an immutable policy file:**
  - max daily and lifetime budget
  - max cumulative spend per campaign key, and optionally for the whole account
  - max bid
  - allowed destination domains and required UTM parameters
  - no archiving unless allowed
- **Activating a campaign needs a human approval block** in the committed desired-state file. It is single-use and bound to the campaign name. Activation also needs a fresh guard heartbeat and room under the cap, assuming 2x daily overdelivery.
- **`adsops_openai_guard` only pauses.** It uses live budgets and bids, counts every campaign AdRails created for the key, and pauses an orphaned one. If spend can't be read, it fails closed instead of reading $0.
- **Helpers:** image upload, the brand icon (warns that a brand change can pause delivery until review), pixel plus a `lead_created` conversion event, and a `validate_only` test event.
- **Growth suite:** `adsops_openai_ad_check` (chat_card limits, allowed domains, required UTM, duplicates), `adsops_openai_draft_campaign`
  (a business brief becomes a paused, capped desired state the engine accepts), `adsops_openai_ab_report` (winner with intervals and
  Bonferroni-corrected significance), `adsops_openai_send_conversions` (CRM leads to the Conversions API, hashed locally, validate-only
  unless `commit=true`) and `adsops_openai_audience` (hashed customer lists to custom audiences, plan-only unless `commit=true`).

**Live status (2026-11-06):** the read paths, live spend reads, the guard and validation ran against a real OpenAI Ads account
in read-only mode ([live proof](docs/LIVE-PROOF-2026-11-06.md)); that run found and fixed a full-hour insights rule the mock had
missed. Creates, image upload, activation and conversion/audience commits are tested offline (`tests/qa_openai_ads.py`,
`tests/qa_openai_suite.py`) but have not yet run live. Section 9 of the guide lists what is still unverified.


## Local business and Google LSA

For a small service business (plumber, HVAC, roofer, cleaner, lawyer...) that wants leads from Google and AI assistants and runs
everything from its own Claude. **Setup is one prompt** in Claude Desktop's Code tab:

> Set me up with AdRails for my business. Read https://adrails.dev/start/ and follow the steps for Claude.

Claude runs `adsops-mcp setup --business "Name" --install-desktop` (workspace in `~/AdRails`, registered in Claude Desktop with a backup
of its config), interviews the owner, builds the plan, and later `adsops-mcp connect --config ~/AdRails/config.json --client-file
<Desktop-app OAuth client JSON>` (one Google sign-in: loopback on 127.0.0.1 + PKCE, refresh token saved chmod 600 beside the config, the
reachable Google Ads accounts added). No ad account is needed to start: the lead book, plan, AEO and reviews work without one.

| Tool | What it does |
|---|---|
| `adsops_local_profile` | questions to ask the owner; saved profile; max cost per lead the business can afford |
| `adsops_local_plan` | generic 90-day growth plan customised from the profile (4 stages, every task names its tool); progress |
| `adsops_leads`, `adsops_leads_due` | one lead book for every channel (dedupe by source id/phone/email), stages, follow-ups, pipeline by source, CSV, won jobs -> offline conversions; speed-to-lead list |
| `adsops_lsa_leads` | Local Services Ads leads + calls/messages, answered/missed, first-response time, junk flags; syncs into the lead book |
| `adsops_lsa_report` | spend, cost per charged lead vs max, credits, booking/answer rate, missed calls, leads by hour/weekday, advice |
| `adsops_lsa_dispute` | suggested lead ratings (junk -> dissatisfied + Google's reason, booked -> satisfied); `commit=true` + explicit decisions submits ProvideLeadFeedback |
| `adsops_lsa_readiness` | license/insurance/background/registration verification, expiry warnings, ranking signals |
| `adsops_local_schema`, `adsops_local_nap`, `adsops_local_area_page` | LocalBusiness JSON-LD, NAP consistency + citation checklist, service x area page briefs for AI answers |
| `adsops_reviews` | review link, request/reply DRAFTS (never sent), review velocity vs competitors |

MCP prompts `start`, `morning` and `weekly` chain these into the daily routine. **Live status:** every LSA query (leads, conversations, campaigns, verification) runs against the live Google Ads API v24
(verified 2026-11-06; `tests/qa_local_suite.py` pins each selected field to Google's live field list). Lead feedback submits through
ProvideLeadFeedback the first time an account has a lead to rate, and returns Google's credit decision. LSA budgets, bids, hours and pausing are not in the API (dashboard only). Lead-book, profile and plan files
live beside the config (chmod 600); the audit chain records only structural arguments for these tools, never customer names or notes.

**Playbooks.** `adsops_playbook` serves the strategy behind the tools, written for the agent to execute: `aeo` (a 7-day fast path
for a new business, the AI-visibility panel method, the pages engines quote, schema, entity consistency, the ads-to-AEO bridge,
cadence, myths), `local-leads` (speed-to-lead, LSA, reviews, follow-up cadence), `google-business-profile` (to 100% and kept
there), `google-ads` (tracking honesty, negatives, landing pages, caps, when to stop) and `chatgpt-ads` (drafts, creative,
attribution, the guard). Every task in the 90-day plan links the playbook section behind it. Every step names its
tool and a done-check; every claim is marked tested or early evidence. Files: `adsops/playbooks/*.md`.

**Results and Business Profile.** `adsops_results` compares the month before AdRails with now (leads, jobs won, cost per lead by
channel, first-response time, AI share of answer, reviews, plan progress); missing data is listed as a gap, never a zero.
`action=case_study` returns a shareable summary with trade and city only (name only with permission), no customer data, and a
quote that is refused if it holds a phone or email. `adsops_gbp` scores a Google Business Profile snapshot to 100 with ranked
fixes, checks the description against Google's rules and plans two posts a week. The `monthly` prompt runs all of it.

**Staying current.** The server tells the agent when a newer AdRails is out: the MCP `initialize` instructions carry a one-line
notice (from a cache, so startup never waits) and `adsops_update_check` returns the release notes and the upgrade command.
At most one request a day to `https://pypi.org/pypi/adrails/json`, no account or usage data sent; off with
`ADRAILS_NO_UPDATE_CHECK=1` or `"update_check": false`. With the owner's OK the agent runs `adsops-mcp upgrade`, which pre-fetches
the release and raises the version floor in Claude Desktop's config (backup first, other servers untouched).

**Optional paid extras.** Everything above is free and stays free. Three extras cost us money to run, so `adsops_paid` sells them per
job, with no account or subscription: an **AI visibility panel** (ChatGPT, Perplexity, Gemini and Claude answer up to 8 questions
from clean sessions with live web search; feed the answers to `aeo_visibility_record`), a **second opinion** (OpenAI GPT-5 reviews
a plan before `adsops_apply commit=true`: go, caution or stop) and a **Bitcoin timestamp** (anchors the audit-chain head via
OpenTimestamps; proofs are saved beside the chain and verify with `ots verify`). The tool only fetches a quote: a fresh Bitcoin
address and amount, and an x402 URL for USDC on Base. **It never pays.** The owner pays from their own wallet, or an x402 agent
wallet pays within a budget the owner set. Prices and terms: `https://adrails.dev/api/v1` and https://adrails.dev/pricing/.


## Works with Google's official `google-ads-mcp`

Run both servers side by side in the same MCP client. Google's server answers ad-hoc GAQL
exploration and never writes; AdRails is the only write path. The agent reads with the official
server, proposes a change to a desired-state or policy file, and applies it through
`adsops_plan` / `adsops_apply` — with caps, a validate pass, an account lock, and an audit line
in between. Both speak JSON-RPC over stdio, so one `.mcp.json` entry each is the whole setup.

## License

Apache-2.0.

<!-- mcp-name: dev.adrails/adrails -->
