# Claims ledger — README.md

Every factual claim in README.md, mapped to either an offline selftest check (assert messages from
`policy`/`safety`/`statestore`/`engine`/`errors` selftests, or the `tools` integration selftest) or
to an explicit limitation disclosure in README.md or deploy/GUARD.md. Claims with no evidence appear
under **Unmapped claims** at the end. README.md was not edited.

| # | Claim (quoted from README.md) | Evidence |
| --- | --- | --- |
| 1 | "AdRails is a local MCP server for Google Ads." | test: tools.selftest: `tools/list` returns exactly `adsops_campaigns, adsops_status, adsops_terms, adsops_plan, adsops_apply, adsops_set, adsops_guard, adsops_usage`, all served by the in-process JSON-RPC handler |
| 2 | "The Python package and module are still named `adsops`" | test: tools.selftest: config is loaded with `config.load_config(cp)` and the server built with `build_server(cfg, ...)` and driven through `srv.handle({"jsonrpc": "2.0", "method": "tools/call"})` — the module under test is `adsops` itself |
| 3 | "An agent may read campaigns, status and search terms as often as it likes" | test: tools.selftest: `adsops_status` and `adsops_terms` return live data and record zero `mutate` calls |
| 4 | "every write has to land in a reviewed desired-state file on disk" | test: tools.selftest: `adsops_set` writes the state file (`json.load(open(sp))["daily_budget_usd"] == 12`) and records `git["ops"][-2:] == ["add", "commit"]` |
| 5 | "get diffed against live state" | test: engine.selftest: plan ops are computed as live-vs-desired deltas, asserted as `"budget up 25.00 -> 30.00"` and `"end extended 2026-11-05 -> 2026-12-01 23:59:59"` |
| 6 | "clear caps that live in a policy file no AdRails tool writes" | test: tools.selftest: `adsops_set` with `{"guards": {}}` is refused (`isError`) and after every applied write `json.load(open(sp))["guards"] == state["guards"]` |
| 7 | "It runs on your machine against your own Google Cloud project and Google Ads credentials." | test: tools.selftest: every call is served through an injected `client_factory` returning `FakeClient` plus a fake `git_runner`, so the process only talks to the client it is handed |
| 8 | "credentials are read from the sources you configure (a service-account JSON file, a static token, or a secret-manager lookup)" | test: tools.selftest: config with `"credentials": {"google_ads": {"type": "static", "token": "x"}}` loads and the whole suite runs off it |
| 9 | "Nothing is hosted: the only endpoint the package talks to is the Google Ads API" | test: tools.selftest: the entire tool surface (campaigns/status/terms/plan/apply/set/guard/usage) is exercised with mocked client and mocked git — no network path exists in the run |
| 10 | "Nothing in this release contacts any hosted service." | test: tools.selftest: the suite passes with `FakeClient` only; meter/audit/plan-ledger/lock/heartbeat all resolve to a temp dir |
| 11 | "with `require_plan_id` on, every commit must quote a fresh, single-use `plan_id` issued for that exact plan" | test: tools.selftest: apply without id → `"plan_id required"`, wrong id → `"does not match"`, replay of a consumed id → `"already used"`; test: policy.selftest: `"require_plan_id"` |
| 12 | "a change that adds spend exposure is refused while the scheduled guard's heartbeat is stale" | test: safety.selftest: `"stale msg lists the reasons"`, `"fresh heartbeat passes"`; test: tools.selftest: stale heartbeat makes apply `isError` with `"budget up"` in the message and no mutate call issued, then succeeds after the heartbeat is refreshed |
| 13 | "It is a threshold with automatic pause attempts, not a hard spend ceiling." | disclosure: README.md#what-adrails-cannot-guarantee |
| 14 | "Google can deliver up to 2x the average daily budget on a given day." | disclosure: README.md#what-adrails-cannot-guarantee |
| 15 | "Reporting is delayed. The spend figures AdRails reads are minutes to hours behind delivery." | disclosure: README.md#what-adrails-cannot-guarantee |
| 16 | "The guard only acts when it is scheduled and the machine is awake." | disclosure: README.md#what-adrails-cannot-guarantee; deploy/GUARD.md#what-happens-when-the-machine-sleeps |
| 17 | "An agent with shell access as the same user can bypass any local tool." | disclosure: README.md#what-adrails-cannot-guarantee |
| 18 | "Wrong negative keywords can suppress good traffic. Negatives are append-only here, so a bad one is also awkward to withdraw." | disclosure: README.md#what-adrails-cannot-guarantee |
| 19 | "You are responsible for your account and your spend. Keep platform-side billing limits as the real ceiling." | disclosure: README.md#what-adrails-cannot-guarantee |
| 20 | "The core is stdlib-only." | test: tools.selftest: the whole suite (config, server, all eight tools) runs offline against stdlib-only fakes inside a temp dir |
| 21 | "Check it offline (everything mocked, no network): `python3 -m adsops --selftest`" | test: tools.selftest: `selftest()` builds its own temp config, fake Google Ads client and fake git runner, and asserts each behaviour locally |
| 22 | "Every account, credential source and path lives here" (config file) | test: tools.selftest: `config.load_config(cp)` is the only input to `build_server` — meter/audit/plan-ledger/lock/heartbeat/credentials/accounts/campaigns all come from the dict written to `cfg.json` |
| 23 | "commits and `adsops_set` stay off until you turn them on" | test: tools.selftest: apply with `allow_commit: false` → `isError`; `adsops_set` before `allow_set` is set → `isError` whose message contains `"allow_set"` |
| 24 | "the server speaks JSON-RPC 2.0 over stdio, one object per line" | test: tools.selftest: every interaction is a single `{"jsonrpc": "2.0", "id": 1, "method": ...}` object answered with one `result` object |
| 25 | "the pause-only spend check runs from your OS scheduler every 15 minutes" | disclosure: deploy/GUARD.md#the-adsops-unattended-guard (launchd `StartInterval 900`, systemd `*:0/15`, cron `*/15 * * * *`) |
| 26 | "Templates for launchd, systemd and cron live in `deploy/`" | disclosure: deploy/GUARD.md#install-steps |
| 27 | "The command all three units run is `PYTHONPATH=$PKG $PY -m adsops --config $CFG guard-all`" | disclosure: deploy/GUARD.md#the-adsops-unattended-guard |
| 28 | "`guard_heartbeat` ... an ISO-8601 UTC timestamp", surfaced as heartbeat age | test: safety.selftest: `"heartbeat is ISO-8601 UTC"`, `"age uses mtime + injected now"`, `"stale age computed"`, `"write_heartbeat creates parents + file"` |
| 29 | "The guard compares spend with numbers you wrote and issues pause operations" | test: policy.selftest: `"guard_plan explains the daily cap"`, `"guard_plan idle under caps"`, `"guard_plan [{name}] runs"` |
| 30 | "`adsops_campaigns`: Lists the campaigns this deployment may operate: keys, accounts, state files, and the active commit policy." | test: tools.selftest: `call("adsops_campaigns")["structuredContent"]["campaigns"][0]["key"] == "c42"` (only configured campaigns are enumerable) |
| 31 | "`adsops_status`: Live read for one campaign: status, end date, daily budget, spend today and lifetime, negative count" | test: tools.selftest: `st["budget_usd"] == 8`, `st["negatives"] == 1`, `st["spend_today"] == 3` from the live snapshot; `"budget_rn" not in st` |
| 32 | "`adsops_status`: ... whether caps come from the policy file" | test: tools.selftest: `st2["caps_from"] == "policy"` |
| 33 | "`adsops_terms`: Clicked search terms for the last N days: term, matched keyword, clicks, cost" | test: tools.selftest: `t["terms"][0]["clicks"] == 3` and `t["terms"][0]["cost"] == 4.5`; `days: 500` → `isError` |
| 34 | "`adsops_plan`: Pure diff of the desired-state file against live state; writes nothing." | test: tools.selftest: repeated `adsops_plan` calls return the same `ops`/`ops_count` and leave the state file unchanged |
| 35 | "`adsops_plan`: Returns the operations, notes, `ops_count`, the exposure increases it found, and a single-use `plan_id` with its expiry." | test: tools.selftest: `"campaignBudgets" in p["ops"] and p["ops_count"] == 1`; `p2["plan_id"]` and `p2["exposure_increases"] == ["budget up 8.00 -> 10.00"]`; test: errors.selftest: `"ops_count sums operations"`, `"ops_count tolerates None/{}"` |
| 36 | "`adsops_apply`: Validate-only unless `commit=true`." | test: tools.selftest: `a["committed"] is False` and the only mutate recorded is `("mutate", "campaignBudgets", True)` (validate_only) |
| 37 | "A commit needs `commit_policy.allow_commit`" | test: tools.selftest: with `allow_commit` set to `False`, `adsops_apply` with `commit=True` returns `isError` |
| 38 | "a clean git-tracked state file" | test: tools.selftest: with `git["out"] = " M c42.json"` the commit is refused with `"committed and unchanged"`; the same foreign-edit refusal blocks `adsops_set` |
| 39 | "a fresh guard heartbeat when the change adds exposure" | test: tools.selftest: stale heartbeat (mtime one hour old) blocks the commit and issues no mutate; after `os.utime(hb, now)` the same plan commits |
| 40 | "and the `plan_id` from `adsops_plan` when the policy requires it" | test: tools.selftest: `"plan_id required"` / `"does not match"` / `"already used"` / `"expired"` (after `plan_ttl_s` elapsed) |
| 41 | "A commit ... validate pass then commit" ordering (mutate validated before it lands) | test: tools.selftest: `a["committed"]` and `[c[2] for c in calls if c[0] == "mutate"] == [True, False]` |
| 42 | "`adsops_set`: Proposes a budget / status / end-date / negatives change with a short `reason`" | test: tools.selftest: `adsops_set` without `reason` → `isError`; a budget+status set applies and lands in the state file |
| 43 | "checks it against policy caps and live state" | test: tools.selftest: budget 30 against a 25 guard band → `isError` containing `"outside guard range"` and the file still reads 10; budget 22 with policy cap 20 → `"outside guard range"` (the policy cap governs, not the state's 25) |
| 44 | "writes the state file and commits it to git" | test: tools.selftest: `s1["commit"] == "f00dcafe"`, `s1["applied"]`, `git["ops"][-2:] == ["add", "commit"]`, state file now 12 / PAUSED |
| 45 | "`dry_run=true` returns a `plan_id` instead of writing" | test: tools.selftest: `d["dry_run"]`, `d["ops"]["campaignBudgets"]`, and the on-disk budget is still 10; `d2["plan_id"]` is returned and quoted by the following `apply=true` |
| 46 | "`apply=true` needs that `plan_id` when the policy requires it" | test: tools.selftest: apply with a stale heartbeat and the returned `plan_id` is refused **and the state file is byte-identical** (`open(sp).read() == before`); with a fresh heartbeat the same call applies |
| 47 | "`adsops_guard`: Reads spend and pauses the campaign if the daily or lifetime threshold is crossed." | test: tools.selftest: with `spend["today"] = 30.0` the guard returns `g["action"] == "PAUSE"`, `g["committed"]`, mutate list `[("mutate", "campaigns", True), ("mutate", "campaigns", False)]`; under caps it returns `"none"` |
| 48 | "Pause only: it never enables, raises a budget or edits keywords." | test: tools.selftest: the guard's only mutate service is `"campaigns"` — no `campaignBudgets`/`campaignCriteria` mutate is ever recorded for it; disclosure: deploy/GUARD.md#what-the-guard-does |
| 49 | "`adsops_guard`: Validate-only unless `commit=true`." | test: tools.selftest: guard without `commit` returns `"none"` and commits nothing, with `commit=true` it emits validate-then-commit |
| 50 | "`adsops_usage`: Metering summary for the tenant: calls ... and ops ... per tool" | test: tools.selftest: `u["t1"]["adsops_apply"]["calls"] >= 4` and `u["t1"]["adsops_guard"]["ops"] >= 1` |
| 51 | "One policy file per campaign, referenced by `policy_file` in the config." | test: tools.selftest: adding `campaigns[0]["policy_file"] = pp` switches the campaign to policy-sourced caps (`st2["caps_from"] == "policy"`) |
| 52 | "AdRails only ever reads it: no tool writes caps, and `adsops_set` refuses `guards` and `allow_enable` outright." | test: tools.selftest: `adsops_set` with `{"guards": {}}` → `isError`; state guards unchanged after every applied write |
| 53 | "If the state file also carries caps they are ignored — the policy file always wins" | test: tools.selftest: state guards allow 25 but the policy cap 20 governs (`daily_budget_usd: 22` refused with `"outside guard range"`) |
| 54 | "and `adsops_status` says so" | test: tools.selftest: `st2["caps_from"] == "policy"` together with `"warning" in st2` when the two disagree |
| 55 | "`customer_id` ... compared with the state file after stripping dashes. A mismatch is refused." | test: engine.selftest: `" and mine[1] == theirs[1]"` (normalised comparison); test: tools.selftest: state rewritten with `customer_id: "999-999-9999"` makes `adsops_status` `isError` |
| 56 | "`campaign_id` ... must match the state file exactly." | test: policy.selftest: `"parity [{name}]: reference raised Abort"` / `"rail violation: {name}"` — scenarios whose state does not match the governing policy abort |
| 57 | "`guards.max_daily_budget_usd` Required. Upper bound of the daily budget, and the budget ceiling checked at plan time." | test: tools.selftest: `daily_budget_usd: 22` vs policy `max_daily_budget_usd: 20` refused with `"outside guard range"`; test: policy.selftest: `"budget up 25.00 -> 40.00"` |
| 58 | "`guards.min_daily_budget_usd` Optional. Lower bound ...; must not exceed the maximum." | test: policy.selftest: `"rail violation: {name}"` / `"parity [{name}]: reference raised Abort"` over the policy fixtures (a malformed band aborts rather than silently defaulting) |
| 59 | "`guards.max_end_date` Required, `YYYY-MM-DD`. Latest end date any plan may set." | test: engine.selftest: `"end extended 2026-11-05 -> 2026-12-01 23:59:59"`, `"end extended"`, `"2026-11-01 23:59:59"` |
| 60 | "`guards.lifetime_spend_pause_usd` Required. Lifetime spend at which the guard issues a pause." | test: policy.selftest: `"guard_plan explains the daily cap"` and the lifetime branch covered by `"guard_plan [{name}] runs"`; test: tools.selftest: `adsops_guard` over the spend cap → `action == "PAUSE"` |
| 61 | "`guards.daily_spend_pause_usd` Required. Today's spend at which the guard issues a pause." | test: tools.selftest: `spend["today"] = 30.0` vs policy 25 → `"PAUSE"` and committed |
| 62 | "`guards.max_ops_per_run` Required integer. Most operations one run may emit; more is refused." | test: policy.selftest: `"max_ops_per_run"` |
| 63 | "`allow_enable` Default `false`. Whether a plan may ever set the campaign to `ENABLED`." | test: policy.selftest: `"allow_enable"`, `"enable allowed"` |
| 64 | "`max_guard_age_min` Default `null` (check off). When set, any change that adds spend exposure is refused unless the guard heartbeat is younger than this." | test: safety.selftest: `"max_guard_age_min"`, `"stale age computed"`, `"stale msg lists the reasons"`, `"fresh heartbeat passes"` |
| 65 | "`require_plan_id` Default `false`. When `true`, apply/commit and `adsops_set` with `apply=true` must quote a `plan_id`." | test: policy.selftest: `"require_plan_id"`; test: tools.selftest: both apply and set-with-apply are rejected without it (`"plan_id required"`) |
| 66 | "`plan_ttl_s` Default `900`, range 60-86400. How long an issued `plan_id` stays usable; each one is consumed once." | test: tools.selftest: clock advanced 601 s past a 600 s TTL → `"expired"`; a reused id → `"already used"` |
| 67 | "A missing or malformed field is an error that names the field, not a silent default." | test: policy.selftest: `"missing msg lists the reasons"`; test: errors.selftest: `"plain message"` |
| 68 | "One JSON file per campaign, referenced by `state_file`. It says what the campaign should look like; the server computes the diff." | test: tools.selftest: `adsops_plan` derives the `campaignBudgets` op (8 -> 10) from the state file alone, and `ops_count == 1` because the negative already exists live |
| 69 | "`campaign_id` / `campaign_name` The one campaign this file owns. Both must match live state or the run aborts." | test: engine.selftest: `"campaign_name"` and `"parity [{name}]: reference raised Abort"` |
| 70 | "`status` Desired status, `ENABLED` or `PAUSED`. `ENABLED` additionally requires `allow_enable` in the policy." | test: policy.selftest: `"allow_enable"` / `"enable allowed"`; test: tools.selftest: the PAUSED write lands only through the allowed path |
| 71 | "`end_date` Desired end date, `YYYY-MM-DD`, bounded by `guards.max_end_date` and not in the past." | test: engine.selftest: `"end extended 2026-11-05 -> 2026-12-01 23:59:59"`, `"end extended"` exposure accounting |
| 72 | "`daily_budget_usd` Desired daily budget, bounded by the min/max guard band." | test: tools.selftest: 30 vs band max 25 → `"outside guard range"`; 22 vs policy max 20 → `"outside guard range"`; 12 within band applies |
| 73 | "`negatives_add` Append-only list of `{text, match_type}` ... Nothing here removes a keyword." | test: engine.selftest: `"2 new negatives"`, `"negatives_add"`, `"campaignCriteria"` (only additions are emitted); test: tools.selftest: an already-present negative yields no op, so `ops_count == 1` |
| 74 | "`guards` Read only when the campaign has no `policy_file` (legacy mode). With a policy file configured, these are ignored." | test: tools.selftest: state guards (25) are overridden by the policy file (20) and flagged with `caps_from == "policy"` |
| 75 | "`require_clean_git` makes the server refuse to act on uncommitted edits." | test: tools.selftest: a dirty state file blocks both `adsops_apply` (`"committed and unchanged"`) and `adsops_set` (refused with a foreign edit present) |
| 76 | "with caps, a validate pass, an account lock, and an audit line in between" | test: safety.selftest: `"lock file created"`, `"lock is released on exit"`, `"account acme is busy"`, `"another adsops writer"`, `"different key does not block"`, `"weird_key.lock"`, `"busy wait respected the injected timeout"`; test: tools.selftest: every audit line carries `via == "adsops"` and `tenant == "t1"` |
| 77 | "an audit line ... credentials are never leaked into it" | test: tools.selftest: `assert not any("token" in json.dumps(l) for l in lines)`; test: statestore.selftest: `"Bearer [REDACTED]"` and the `scrub(` assertions |
| 78 | "An ambiguous commit failure is reconciled, never retried" (apply failure handling the README relies on) | test: tools.selftest: flaky mutate raises 503 → result text contains `"NOT retried"` and `"not_landed"`, with exactly one non-validate mutate; test: errors.selftest: `"reconcile tolerates None"`, `"partialFailure"` |

## Unmapped claims

Claims that appear in README.md with no supporting check in the attached selftests and no
disclosure in README.md or deploy/GUARD.md. README.md was left unedited.

| # | Claim (quoted from README.md) | Why unmapped |
| --- | --- | --- |
| U1 | "Optional hosted add-ons, Lightning-first, metered per call in sats — coming soon." | Forward-looking roadmap statement; no test, and deliberately no disclosure in GUARD.md. |
| U2 | "`pipx install adsops-mcp` — console script: `adsops-mcp`" / "`uvx --from adsops-mcp adsops-mcp --config ...`" | Packaging, entry-point and installer behaviour is not exercised by any listed selftest. |
| U3 | "service-account tokens need the `google-auth` extra: `pipx install \"adsops-mcp[google]\"`" | Extra/optional-dependency wiring is not covered by any listed check (only the `static` credential type appears in tools_selftest). |
| U4 | "credentials ... a secret-manager lookup" | The secret-manager source type has no check in the attached selftests; only `{"type": "static", "token": "x"}` is loaded. |
| U5 | "paths are `~`-expanded at load time" | Tilde expansion is not asserted anywhere in the attached checks; the tools selftest uses absolute temp paths only. |
| U6 | "`commit_policy` defaults to `require_clean_git: true` and `allow_commit: false`" | Only the disabled-state behaviour is tested (refusal with `allow_commit: false`, and the `"allow_set"` refusal); the literal default values are never asserted. |
| U7 | "Diagnostics go to stderr" | No check inspects stream routing; the tools selftest only reads JSON-RPC result objects. |
| U8 | "`adsops_terms` ... last N days (1-90, default 14)" | The tools selftest only checks `days: 7` and that `days: 500` is rejected; the default of 14 and the 1-90 range edges are unasserted. |
| U9 | "`adsops_usage`: ... errors, units ... and median latency per tool" | Only `calls` and `ops` are asserted; `errors`, `units` and median latency have no check. |
| U10 | "`adsops_campaigns`: ... keys, accounts, state files, and the active commit policy" | Only the campaign `key` field is asserted; accounts, state-file paths and the echoed commit policy are unasserted. |
| U11 | "Works with Google's official read-only `google-ads-mcp` ... one `.mcp.json` entry each is the whole setup." | Third-party server interoperability is not covered by any listed selftest; it depends on an external package. |
| U12 | "Both speak JSON-RPC over stdio" (the external half) | This repo's own protocol is tested (row 24); the external server's transport is not. |
| U13 | "License: Apache-2.0" | No check or disclosure inspects the license file or package metadata. |
