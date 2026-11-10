# Claims ledger — README.md

Every factual claim in README.md, mapped to either an offline selftest check (assert messages from
`policy`/`safety`/`statestore`/`engine`/`errors` selftests, or the `tools` integration selftest) or
to an explicit limitation disclosure in README.md or deploy/GUARD.md. Claims with no evidence appear
under **Unmapped claims** at the end. README.md was not edited.

| # | Claim (quoted from README.md) | Evidence |
| --- | --- | --- |
| 1 | "AdRails is a local MCP server for Google Ads." | test: tools.selftest: `tools/list` returns exactly `adsops_campaigns, adsops_status, adsops_terms, adsops_plan, adsops_apply, adsops_set, adsops_guard, adsops_usage, adsops_negatives_suggest, adsops_competitors_report, adsops_auction_insights_import, adsops_lp_diagnose`, all served by the in-process JSON-RPC handler |
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
| 20 | "The core is stdlib-only." | test: tools.selftest: the whole suite (config, server, all twelve tools) runs offline against stdlib-only fakes inside a temp dir |
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
| 79 | "`adsops_negatives_suggest`: Read-only." | test: playbook_tools.selftest: every GAQL string issued during the call starts with `SELECT`; no `mutate` exists on the fake client |
| 80 | "Skips terms and phrases your existing negatives already cover" (PHRASE contiguous, BROAD any order, EXACT identical, no close variants) | test: negatives.selftest: Acme fixture marks `tampa locators`, `airbnb property management tampa` as covered; re-running with the six new phrases added suggests nothing; `blocks()` cases for `apartment`/`apartments`, BROAD and EXACT; test: playbook_tools.selftest: coverage reads both the desired-state file and the live negatives |
| 81 | "never suggests words on your protect list" | test: negatives.selftest: a candidate equal to or inside a protected phrase (`jobs`, `management` inside `property management`) is refused and reported, while `acme tx` is allowed when only `acme` is protected |
| 82 | "returns a ready-to-paste `negatives_add` patch plus a basis note" | test: negatives.selftest / playbook_tools.selftest: `negatives_add_patch` holds `{text, match_type: PHRASE}` entries and `basis_note` has a `_negatives_basis_<date>` key |
| 83 | Acme 2026-11-01 playbook is reproduced: the six phrases `condominium, first service, castle group, bradenton, palmetto, polk` and nothing else | test: negatives.selftest against `adsops/fixtures/acme_negatives_2026_11_01.json` (synthetic terms rebuilt from the basis note; baseline = the real negative list before that batch) |
| 84 | "an optional local-model hook you supply" is never called unless configured, may only propose a phrase that literally appears in an unclassified term, and its suggestions are marked `needs_review` | test: negatives.selftest: stub hook; dead command returns nothing; a string `llm_command` is rejected |
| 85 | "`adsops_lp_diagnose`: Read-only ... plain HTTP check ... Returns plain-English findings, worst first." | test: lpdiag.selftest: 404 page ranks first as high severity; slow, heavy, no-title, no-H1, keyword-mismatch, missing-town and below-average component findings; healthy page earns none; one fetch per distinct URL; test: playbook_tools.selftest: runs through the server with the fetch mocked |
| 86 | "No browser and no JavaScript" | test: lpdiag.selftest: `<script>` text is ignored by the parser and the check uses `urllib` only; non-http(s), localhost and private-address URLs are refused by `http_fetch` |
| 87 | "`adsops_competitors_report`: ... makes no network call" | test: playbook_tools.selftest: the report is built from the local database after an import, with no client or HTTP object in the call path |
| 88 | "`adsops_auction_insights_import`: writes only that local SQLite file" and reads a pasted table | test: playbook_tools.selftest: pasted fixture stores rows, range returned; text with no rows and an unreadable `range` are errors |
| 89 | "Needs `intel_config`" | test: playbook_tools.selftest: without it the intel tools return an error naming `intel_config` |
| 90 | `adsops_plan` covers "the URL fields below" (`campaign_final_url_suffix`, `ad_groups.<name>.url_custom_parameters`, `.final_url_suffix`, `.ad_final_urls`) | test: urls.selftest: state equal to live gives 0 ops; suffix, custom-parameter and per-ad final-URL diffs give `campaigns`/`adGroups`/`ads` ops; engine.selftest: URL diff yields `adGroups` + `ads` ops and the campaign suffix shares the campaign op |
| 91 | "Final URLs for every enabled ad in the group" | test: urls.selftest: both enabled ads of a two-ad group get the op; google_ads.selftest: `live_urls` reads only `ad_group_ad.status = 'ENABLED'` ads and only non-removed groups |
| 92 | "Each must be `https://` on a host in the campaign's `url_domains` allowlist (or a subdomain of one), with no port or credentials" | test: urls.selftest: http, other hosts, look-alike hosts (`example.com.evil.com`, `notexample.com`), userinfo, ports, `javascript:`, spaces and scheme-relative URLs are refused; tools.selftest: an off-allowlist URL makes `adsops_plan` an error |
| 93 | "Any `url_domains` written in the state file is ignored. With no allowlist configured, a state file that names final URLs is refused even when nothing would change." | test: tools.selftest: the state file carries `url_domains: ["evil.com"]` and plan is refused until the config allowlist is set; urls.selftest: empty allowlist refused at 0 diff |
| 94 | "A name that is not in the campaign, or is shared by two live groups, is refused" | test: urls.selftest: unknown and duplicate-name ad groups raise RailViolation; google_ads.selftest: duplicate names are flagged by `live_urls` |
| 95 | "A URL change adds no spend exposure, so a stale guard heartbeat doesn't block it. Each change still appears as an op and a `url: ...` note in the plan, and in the apply line of the audit log." | test: tools.selftest: `exposure_increases == []`, commit succeeds with a 2-hour-old heartbeat, and the last audit line has `ops == {"adGroups": 1, "ads": 1}` with a `final_urls ... /owners/pasco/` note |
| 96 | Same lifecycle: "commit the state file, `adsops_plan`, `adsops_apply` (validate-only), then `adsops_apply commit=true` with the `plan_id`, and `adsops_status` to read the fields back" | test: tools.selftest: validate-only issues only `validateOnly` mutates on `adGroups`/`ads`; commit without `plan_id` is refused; with it, validate then commit on both services; `adsops_status` returns the new custom parameters, final URLs and suffix; re-plan is 0 ops |
| 97 | "Anything else, such as a bid field next to a URL, is refused" | test: engine.selftest: `check_op_fields` refuses a bid mask, an extra ad field, an unmanaged service, a non-negative criterion and a criterion remove; urls.selftest + tools.selftest: `cpc_bid_micros` in `ad_groups` is refused |
| 98 | Token expiry: google-auth's naive-UTC `creds.expiry` is read as UTC, never more than 3600 s ahead, and a 401 `ACCESS_TOKEN_EXPIRED` refreshes and retries once | test: auth.selftest: under `TZ=America/New_York` the naive `.timestamp()` is hours off (control) while `_expiry_epoch` is within 5 s; a 5-hour expiry is capped at now+3600; `invalidate()` forces a refresh; google_ads.selftest: first 401 retried with the fresh token, a second 401 propagates, other 401s are not retried |
| 99 | Live (2026-11-01, read-only): Acme `acme_search` URL fields imported from live; `adsops_plan` returns `ops_count: 0` | artifact: live run against the Google Ads API with mutate blocked, see the commit message of this change; not an offline test |

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

## 0.4.0 claims (added 2026-11-05, the maintainers) — each maps to a selftest in `python3 -m adsops --selftest`

| # | Claim | Evidence |
| --- | --- | --- |
| E1 | 40 MCP tools | test: tools.selftest asserts `len(names) == 40` and the core 12 in order |
| E2 | RBAC is deny-by-default once enabled; a refused call has zero side effects | test: rbac.selftest; enterprise_tools.selftest (viewer `adsops_apply` refused, call count unchanged) |
| E3 | Approval tokens are HMAC-signed, expire, and forged / expired / reused / cross-plan tokens are refused; the secret is never written | test: approvals.selftest; enterprise_tools.selftest (commit refused without plan_id and without approval, allowed after); the maintainers probe 2026-11-05 (file mode 0600, secret absent from file) |
| E4 | Every mutating call, including refused ones, is appended to a hash-chained audit log; edits and deletions are detected | test: auditchain.selftest; enterprise_tools.selftest (`entries >= 4` after refusals) |
| E5 | Rollback never reverts guards, commit policy, customer or campaign IDs, and never removes negatives; commit refs are injection-safe | test: rollback.selftest; the maintainers probe (`--output=` and `HEAD~51` refused) |
| E6 | Drift detection reads Google change history (max 30 days) and flags out-of-band edits | test: change_history.selftest, enterprise_tools.selftest; live read-only GAQL verified 2026-11-05 (finite date range required) |
| E7 | Aggregate cap assumes 2x delivery; proposals that lower exposure always pass | test: fleet.selftest; the maintainers probe |
| E8 | Rules can only pause, add negatives, alert or step budgets within caps; enabling is rejected | test: rules.selftest; the maintainers probe (enable action rejected; +500% clamped to +20%) |
| E9 | Budget reallocation never raises the total | test: budget_realloc.selftest |
| E10 | Conversion audit / diagnostics queries run against the real API | live read-only run on Acme 123-456-7890, 2026-11-05 (7/9 queries valid at first; 2 fixed and re-verified) |
| E11 | Offline conversions are hashed locally and validate-only | test: offline_conv.selftest |
| E12 | AEO fetcher refuses private / loopback / link-local / reserved addresses on every redirect hop | test: aeo_tools.selftest (private resolution and redirect-to-private refused) |
| E13 | AEO audits (crawler, page, schema, llms.txt, visibility, IndexNow, AI traffic, ads-to-answers) | test: aeo.* selftests + independent DeepSeek black-box suites in tests/blackbox/ written from the specs only |
| E14 | `init` writes a safe policy and never overwrites; `doctor` never echoes secrets | test: onboard.selftest (30 checks) |
| E15 | ChatGPT Ads adapter: in development, not released | disclosure: the site and README say so; no code claims |

## OpenAI (ChatGPT) Ads adapter (README "ChatGPT Ads (beta)" + docs/OPENAI-ADS.md)

Evidence is the check text printed by `python3 tests/qa_openai_ads.py`. Every check runs against an
in-process fake of the documented Ads API, with no network. Nothing below has been run against a live account.

| # | Claim | Evidence |
| --- | --- | --- |
| O1 | "One API key per ad account ... from 1Password, a file or an env var" | qa: `1Password failure falls back to the file source`, `1Password ref read`, `key file read`, `config loads an openai_ads account` |
| O2 | "Any number of `openai_ads` accounts" | config: accounts are a list; `config refuses ...` checks validate each entry. Only one account is exercised end to end (partial evidence) |
| O3 | "always created paused" | qa: `every create is paused`, `API saw status=paused on every create`, `partial failure leaves things paused` |
| O4 | "Each create carries an `Idempotency-Key`, so a retry cannot duplicate it" | qa: `every create carries an Idempotency-Key`, `retried create reuses the same Idempotency-Key`, `retries never duplicated the campaign`; creates without API idempotency support: `... is not retried on 5xx` |
| O5 | "max daily and lifetime budget, max bid, allowed domains, required UTM, no archiving unless allowed" | qa: `policy refusal: exceeds policy max`, `below policy min`, `lifetime budget`, `max_bid`, `no bid ceiling`, `allowlist`, `https`, `utm_campaign`, `utm_source`, `allow_archive`, `archive only when policy allows it` |
| O6 | "max cumulative spend per campaign key, and optionally for the whole account" | qa: `lifetime spend is summed over the managed campaign and its siblings`, `campaign rotation (rename + drop id) is refused`, `account cap: guard pauses every active campaign in the account` |
| O7 | "Activating a campaign needs a human approval block ... single-use and bound to the campaign name" | qa: `activation refused without approval; nothing activated`, `approval refused: approvers/expired/future/above approval/max_total_spend/window`, `re-activation needs a NEW approval`, `a boolean is not a number in the approval block` |
| O8 | "a fresh guard heartbeat" | qa: `stale guard heartbeat blocks activation`, `disabled openai account never writes its heartbeat`, `a failing openai guard writes no heartbeat (fail closed)` |
| O9 | "room under the cap, assuming 2x daily overdelivery" | qa: `pause_at = cap - 2 x daily`, `2x-overdelivery room check blocks at $440`, `activation room uses the same pause point as the guard`, `spend is re-read right before the first activation` |
| O10 | "`adsops_openai_guard` only pauses" | qa: `150 random guard runs: only /pause writes`, `execute refuses ops outside its allow-list (guard path = pause only)` |
| O11 | "uses live budgets and bids" | qa: `guard pauses an outside budget raise`, `guard pauses an uncapped (maximize) bid set outside AdRails` |
| O12 | "pauses an orphaned one" | qa: `guard pauses an active orphan campaign tagged for the key` |
| O13 | "If spend can't be read, it fails closed" | qa: `empty insights -> guard errors (fails closed), never reads $0`, `delivery without a spend field fails closed`, `missing created_at fails closed`, `unknown spend -> no heartbeat -> activation blocked` |
| O14 | "image upload, the brand icon (warns ... pause delivery)" | qa: `512px image refused for ads`, `favicon upload sends purpose=account_favicon`, `brand update refused without acknowledge_delivery_pause`, `brand update with acknowledgement` |
| O15 | "pixel plus a `lead_created` conversion event, and a `validate_only` test event" | qa: `pixel + lead_created event setting created and linked`, `conversions setup is idempotent by name`, `test event is lead_created with validate_only=true` |
| O16 | API key never leaks | qa: `the API key never appears in outputs, audit or meter`, `4xx is not retried and surfaces as an error`, `1Password stderr containing the key is redacted`, `group-readable key file refused` |
| O17 | "Live verification": read paths, live spend reads, guard and validation ran against a real OpenAI Ads account on 2026-11-06 (read-only); creates, uploads, activation and conversion/audience commits have NOT yet run live | evidence: docs/LIVE-PROOF-2026-11-06.md; the live run found the full-hour insights bug, fixed with a test that fails without the fix (tests/qa_openai_suite.py) |
| O18 | Google campaigns untouched by the OpenAI guard schedule | qa: `guard-all --only-platform openai_ads skips google campaigns`; the existing Google suites (`qa_invariants`, `qa_protocol`, `qa_inputs`, `qa_secrets`, `--selftest`) pass unchanged |

| O19 | Growth suite: ad check, campaign drafts the engine accepts, A/B report, conversions (validate-only default), audiences (plan-only default), hashed locally | qa_openai_suite.py (24 checks): drafts pass the real oai_engine/oai_policy validators, no raw PII on the wire/in outputs/audit chain, read_only accounts refuse commits; blackbox suites for oai_* modules |

## 0.11.0 technical SEO site audit (README "Technical SEO site audit (0.11)" and "New in 0.11.0")

Evidence is a `python3 -m adsops --selftest` check (module and quoted check message), the black-box suite
`tests/blackbox/test_seo_site_audit.py` (written by a separate agent from the spec only), or the acceptance runs of 2026-11-10
(baytownpm.com live, a pre-fix snapshot of baytownpm.com replayed on loopback, adrails.dev live), kept with the PR notes.

| # | Claim | Evidence |
| --- | --- | --- |
| S1 | "Read-only: GET requests only, nothing is ever written to the site" | test: seo.fetcher.selftest `only GET/HEAD`; seo.render.selftest `the browser may only GET/HEAD: page-script POSTs are aborted`; no other request path exists in adsops/seo |
| S2 | "public addresses only, checked once per hop and the connection pinned to that address (no DNS rebinding), default ports only" | test: seo.fetcher.selftest `refused ...` (private, loopback, link-local/metadata, IPv6 loopback, IPv4-mapped, mixed public+private DNS answer, :8080) and `connects to the pinned address`; seo_tools.selftest `real audit refuses loopback targets` |
| S3 | "robots.txt honoured, also on every redirect hop" | test: seo.crawl.selftest `disallowed URL never fetched`, `a redirect into a Disallowed path is not followed`; seo_tools.selftest `robots.txt Disallow honoured: /private/ never requested` (real loopback HTTP) |
| S4 | "1 request/second, 200 pages by default, 2,000 max; same host and its www twin only" | test: seo.crawl.selftest `every request start is >= rate_limit_s apart`, `max_pages stops the crawl`, `off-scope URLs never fetched`; seo_tools.selftest `tool refuses a rate faster than 0.5 s`, `tool refuses max_pages > 2000` |
| S5 | "3 MB and a deadline per response, a wall-clock limit per crawl" | test: seo.fetcher.selftest `body capped`, `gzip bomb stops at the cap`, `a trickling server is cut off at 3 x timeout`; seo.crawl.selftest `the crawl stops at its wall-clock deadline`, `hostile sitemap bodies are scanned in linear time` |
| S6 | "redirect chains recorded" | test: seo.crawl.selftest `chain records every hop`, `redirect loop detected and stopped`; seo_tools.selftest `two-hop redirect chain recorded` |
| S7 | The listed checks (indexability, sitemap cross-reference, titles/H1/meta, near-duplicates, canonicals, doctype/lang, hreflang, depth, PageRank, starved pages, sibling skew, anchors, nofollow, utm links, schema coverage, HSTS/headers) | test: seo.checks_content / checks_site / checks_links selftests (one fixture per code); seo_tools.selftest `fixture defect found: <code>` for 17 codes over a real loopback site |
| S8 | "internal links carrying utm_ or click-id parameters" | test: seo_tools.selftest `6 utm-tagged internal links to /owners/, which is noindex`; acceptance: baytownpm.com 79 links on 42 pages (prototype found the same pattern) |
| S9 | "ad landing-page guard over the final URLs in your Google Ads and ChatGPT Ads state files" | test: seo.adguard.selftest (Google `ad_final_urls`, OpenAI `target_url`, noindex, equity sink, organic duplicate, non-200); seo_tools.selftest `a noindex ad page linked from organic pages is ONE P4 row (no generic noindex / equity-sink duplicates)`, `off-site ad URL ignored, on-site one audited` |
| S10 | "prioritised issue table (priority, hours, issue, recommendation, example URL, pages affected)" as JSON, Markdown, CSV | test: seo.issues.selftest (ordering, polish last, markdown escaping, CSV formula neutralising); seo_tools.selftest `every row has the table columns`, `markdown + csv written` |
| S11 | "Runs are stored and diffed ... regressions: new noindex ..." | test: seo.runs.selftest (round trip, 0600 files, traversal refused, each regression kind); seo_tools.selftest `diff: new noindex flagged as a regression`, `diff: duplicate pair reported fixed` |
| S12 | `render=true`: JS render parity in a sandboxed Chromium, child process with a time limit, WebSockets refused | test: seo.render.selftest (route handler, parity, `a hung render child is stopped and reported`); acceptance: render ran on 6 pages of baytownpm.com and adrails.dev. WebSocket refusal is configured via Playwright's route_web_socket and NOT exercised by a test (partial evidence) |
| S13 | `lighthouse=true`: LCP per template, the LCP element, preload vs LCP image | test: seo.perf.selftest (synthetic Lighthouse 13 report); acceptance: the prototype's real Lighthouse 13.6 report of the pre-fix home page gives `preloads hero-antonio.webp but its LCP element is ... video#broll (poster hero-broll-poster.webp)`; live runs measured 6 templates per site |
| S14 | `gsc=true` / `seo_gsc_exclusions`: URL Inspection + clicks, "not connected" degrades cleanly | test: metrics.gsc.selftest `inspect request`; seo.gsc_join.selftest; seo_tools.selftest `seo_gsc_exclusions merges Google's coverage state with the crawl's explanation` (fake client). Live: see the PR notes for whether the founder-credential run connected |
| S15 | "Runs are stored in seo-audits/<host>/ beside your audit log, newest 30 per site, mode 0600" | test: seo_tools.selftest `stored run is private (0600)`; seo.runs.selftest pruning to keep and report pruning |
| S16 | "91 tools" (0.11.0; superseded by C15: 99 with an openai_ads account, 83 without) | test (0.11.0): tools.selftest `len(names) == 75` and the four seo_* names; the 16 adsops_openai_* tools register only with an openai_ads account (as in 0.10's 87) |

## 0.12.0 guarded content pipeline (README "Guarded content pipeline (0.12)" and "New in 0.12.0")

Evidence is a `python3 -m adsops --selftest` check (module and check message), the black-box suite
`tests/blackbox/test_seo_content.py` (loopback model API over real HTTP, injected site fetch), or the live acceptance runs of
2026-11-10 (Order D log). Nothing here is claimed from code reading alone.

| # | Claim | Evidence |
|---|---|---|
| C1 | "Without an approved FACTS file the pipeline refuses to draft"; `seo_content_facts` never approves | test: content.facts.selftest (proposed / missing approved_by / empty refused; propose never sets approved_by); blackbox `test_kill_switch_and_unapproved_facts` (no model call made) |
| C2 | Topics from Search Console 8-40, questions, thin/starved audit pages; extend preferred; brand/competitor/doorway refused | test: content.topics.selftest (band, impressions, safe_query, doorway place/zip swap, lessons weights); acceptance: live plan on baytownpm.com (297 GSC rows, own-brand queries refused) |
| C3 | Three writer kinds, fallback chain recorded, truncated output is an error | test: content.writers.selftest (`chain falls to backup`, `truncated output is an error`, command backend); blackbox `test_truncated_writer_falls_back_to_backup_and_is_recorded`, `test_all_writers_truncated_is_held` |
| C4 | Keys by env name only, never logged, not passed to a command backend; prompt never in argv | test: content.writers.selftest (`key only in the header`, `error names the env, never a value`, `API keys not passed to command`, `prompt via 0600 file, not stdin`, `prompt in argv refused`); blackbox `test_end_to_end_approved_bundle` (no key value anywhere on disk) |
| C5 | Reviewer of a writer's family refused before any call | test: content.writers.selftest (`same-family reviewer refused at config`, `reviewer = backup writer family refused`); blackbox `test_same_family_reviewer_is_refused_before_any_call` |
| C6 | Answering model verified; silent fallback, unverifiable model, tool use refused; no verdict = hold | test: content.writers.selftest, content.review.selftest; blackbox `test_silent_fallback_reviewer_is_held`, `test_reviewer_without_verdict_is_held` |
| C7 | Every number traced to FACTS or a verbatim quote fetched in the run; statistics in words refused | test: content.numbers.selftest, content.checks.selftest; blackbox `test_invented_statistic_is_held_and_never_reviewed` |
| C8 | Reviewer edits re-run every check | test: content.review.selftest (`edit that fails checks = hold`); blackbox `test_reviewer_edit_adding_a_number_is_held`, `test_reviewer_edit_is_rechecked_and_kept_when_clean` |
| C9 | Fails closed on any fetch error | test: content.sources.selftest; blackbox `test_fetch_error_mid_run_fails_closed`, `test_source_fetch_error_at_review_holds` |
| C10 | Prompt injection in fetched pages is caught by the checks | blackbox `test_prompt_injection_in_live_page_is_caught_by_checks` |
| C11 | Per-run call/token/cost/time caps | test: content.writers.selftest (`cost cap exceeded after the call`, `call cap`); blackbox `test_cost_cap_exceeded_is_held` |
| C12 | Bundle: page.md, IMPLEMENT.md, review.json, hashes; tampering detected | test: content.bundle.selftest; content.publish.selftest (`tamper refused`) |
| C13 | Publish: human approval by default, audit chain, never over an existing page for new pages, no symlinks, git never pushes, auto only with caps + window, HOLD on a failed post-publish check, kill switch | test: content.publish.selftest; blackbox `test_publish_human_then_auto_hold`, `test_cli_refuses_with_kill_switch` |
| C14 | Results at 14/28/56 days; LESSONS.md visible, capped, operator deletions stick | test: content.learn.selftest; blackbox `test_results_and_lessons_loop` |
| C15 | "99 tools" (with an openai_ads account configured; 83 without) | test: tools.selftest `len(names) == 83` and the eight seo_content_* names |
