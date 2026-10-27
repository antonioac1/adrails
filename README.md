# AdRails (adrails.dev)

**Guardrails for AI agents running your ads: an open-source, self-hosted Google Ads MCP server.**

Website and docs: https://adrails.dev · MCP Registry: `dev.adrails/adrails` · plain-text summary for agents: https://adrails.dev/llms.txt

AdRails is a local MCP server for Google Ads. An agent may read campaigns, status and search
terms as often as it likes; every write has to land in a reviewed desired-state file on disk,
get diffed against live state, and clear caps that live in a policy file no AdRails tool writes.
The Python package and module are still named `adsops` — that is the name you install and run.

Three things it does differently:

- **Your keys.** It runs on your machine against your own Google Cloud project and Google Ads
  credentials. Nothing is hosted: the only endpoint the package talks to is the Google Ads API,
  and credentials are read from the sources you configure (a service-account JSON file, a static
  token, or a secret-manager lookup).
- **Your caps.** Caps live in a policy file that no AdRails tool writes; with `require_plan_id`
  on, every commit must quote a fresh, single-use `plan_id` issued for that exact plan; and a
  change that adds spend exposure is refused while the scheduled guard's heartbeat is stale.
- **Pay per call in sats.** Optional hosted add-ons, Lightning-first, metered per call in sats —
  coming soon. Nothing in this release contacts any hosted service.

## Testing

```bash
python3 -m adsops --selftest           # 430 unit checks, everything mocked
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
# from GitHub (PyPI release coming soon)
pipx install git+https://github.com/antonioac1/adrails     # console script: adsops-mcp
# or run without installing:
uvx --from git+https://github.com/antonioac1/adrails adsops-mcp --config /absolute/path/config.json
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
pip install git+https://github.com/antonioac1/adrails@v0.2.2

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
| `adsops_status` | Live read for one campaign: status, end date, daily budget, spend today and lifetime, negative count, whether caps come from the policy file, and the guard heartbeat's age. |
| `adsops_terms` | Clicked search terms for the last N days (1–90, default 14): term, matched keyword, clicks, cost. |
| `adsops_plan` | Pure diff of the desired-state file against live state; writes nothing. Returns the operations, notes, `ops_count`, the exposure increases it found, and a single-use **`plan_id`** with its expiry. |
| `adsops_apply` | Validate-only unless `commit=true`. A commit needs `commit_policy.allow_commit`, a clean git-tracked state file, a fresh guard heartbeat when the change adds exposure, and the **`plan_id`** from `adsops_plan` when the policy requires it. |
| `adsops_set` | Proposes a budget / status / end-date / negatives change with a short `reason`; checks it against policy caps and live state, writes the state file and commits it to git. `dry_run=true` returns a **`plan_id`** instead of writing; `apply=true` needs that `plan_id` when the policy requires it. |
| `adsops_guard` | Reads spend and pauses the campaign if the daily or lifetime threshold is crossed. Pause only: it never enables, raises a budget or edits keywords. Validate-only unless `commit=true`. |
| `adsops_usage` | Metering summary for the tenant: calls, errors, units, ops and median latency per tool. |

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

## Works with Google's official read-only `google-ads-mcp`

Run both servers side by side in the same MCP client. Google's server answers ad-hoc GAQL
exploration and never writes; AdRails is the only write path. The agent reads with the official
server, proposes a change to a desired-state or policy file, and applies it through
`adsops_plan` / `adsops_apply` — with caps, a validate pass, an account lock, and an audit line
in between. Both speak JSON-RPC over stdio, so one `.mcp.json` entry each is the whole setup.

## License

Apache-2.0.
