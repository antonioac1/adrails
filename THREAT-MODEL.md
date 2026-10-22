# AdRails (adsops) Threat Model

## Scope and evidence

AdRails is an open-source, self-hosted MCP server operating customers’ Google Ads accounts with their own credentials. The project hosts neither credentials nor workloads. Customers control installation, OS isolation, scheduling, and account permissions.

This review covers the supplied tools.py and engine.py. Other modules and deployment behavior are specified by contracts, not verified implementations. “Contract” below explicitly marks that distinction. No runtime security tests were executed.

Threat actors include malicious advertising-data authors, prompt-injected or compromised agents, local users/processes, compromised dependencies, and accidental concurrent operators. A trusted administrator is outside the enforcement boundary. Severity assumes real advertising spend and production credentials.

## 1. Assets and trust boundaries

| Asset/component | Boundary and security requirement |
|---|---|
| Agent | Reads untrusted search terms/campaign names; proposes actions. Neither its reasoning nor its supplied reason constitutes authorization. |
| MCP client | Launches/connects to adsops over stdio. Client access conveys tool authority; no independent human-approval identity is shown. Client/model providers may receive returned advertising data. |
| adsops process | Converts requests into privileged API mutations. Protect process identity, configuration, credentials, imported code, and environment from the agent. |
| State file | Desired state is agent-writable through adsops_set. Treat all fields as untrusted, even when git-tracked. |
| Policy file | Contains caps and enable permission; adsops tools never write it. This is a logical boundary, not filesystem isolation. Policy is optional in current tools. |
| Git | Records desired-state history; cleanliness proves tracked/unchanged status, not human review. adsops_set itself commits agent changes. Protect hooks/config and repository integrity. |
| Google Ads API | External authorization and mutation boundary. Protect tokens/customer binding; reporting may lag and multi-service changes are not atomic. Other clients can change the account. |
| Scheduled guard | Independent pause-only worker; depends on scheduler, host uptime, credentials, API availability, and spend freshness. Its heartbeat is a separate trust signal. |
| Plan ledger | Local authorization/replay state: issued identifiers, expiry, consumption. Must resist tampering, truncation, races, and crash loss. |
| Locks and logs | Locks coordinate cooperating local writers; logs support accountability and contain sensitive metadata. Protect paths, permissions, integrity, retention, and disk capacity. |

Money, traffic availability, account configuration, credentials, search-query privacy, and trustworthy mutation history are primary assets.

## 2. STRIDE threat register

S = spoofing; T = tampering; R = repudiation; I = information disclosure; D = denial of service/wallet; E = elevation of privilege.

### 1. Prompt injection through advertising data — S/T/E — High

**Current:** Config-selected campaigns, engine caps, validateOnly, and conditional plan IDs restrict tool actions. Terms and campaign names are returned as data, without an instruction/data isolation mechanism shown.

**Residual:** An injected agent can obtain its own plan ID and invoke permitted mutations or leak queries through other tools. A plan ID proves neither human review nor benign intent.

**Fix:** Label external strings untrusted, constrain client tool authority, and require independently authenticated approval for consequential mutations. Do not rely on prompt filtering.

### 2. Shell access bypasses rails — T/E — Critical

**Current:** Policy overrides state guards when configured; tools do not write policy. Customer identity is checked. Commit defaults off.

**Residual:** An agent sharing credentials or OS identity can edit policy/config/ledger/heartbeat, run modified code, or call Google directly. Clean git does not prevent this.

**Fix:** Run adsops under a separate principal with credentials unavailable to the agent; administrator-owned read-only policy/config/code and protected storage. Restrict account permissions and direct credential access. Agent root/admin access defeats local enforcement.

### 3. Plan replay and TOCTOU — S/T — High

**Current:** Digest binds account/campaign keys, operations, policy-file hash, and non-spend live fingerprint; nonce distinguishes issues. Conditional verification and contracted expiring, single-use ledger consumption occur before execute.

**Residual:** require_plan_id defaults false. Policy is loaded separately from hashing, allowing old effective caps to bind a newer file hash. adsops_set reuses pre-lock live/state/operations; apply loads state/policy before locking. Spend is excluded; daily spend does not block enabling. Git cleanliness is checked before locking. External changes remain possible after reads.

**Fix:** Require plans for spending mutations; lock before reading/checking, hash the exact parsed policy bytes, replan against fresh live data, recheck spend, and atomically consume durable IDs. Bind normalized customer ID and execution settings, not just config aliases.

### 4. Stale or forged guard permits overspend — S/D — High

**Current:** Contracted freshness check blocks exposure increases when configured; guard only pauses. Templates specify 15-minute checks.

**Residual:** max_guard_age_min defaults off. One global mtime proves neither campaign coverage nor successful pause. Future timestamps, forged touches, sleep, API failures, and reporting lag undermine freshness. Existing spend continues when increases are refused. guard-all heartbeat success semantics are not supplied.

**Fix:** Require protected per-account/campaign successful-check records with bounded clock skew, coverage and pause outcomes; fail closed on exposure increases and alert on missed checks. Use conservative thresholds and independent supervision; never advertise a hard ceiling.

### 5. Concurrent writers and lost updates — T/D — High

**Current:** Mutation paths take contracted flock-based account-key locks.

**Residual:** set computes updates before locking; guard reads before locking. Aliases for one customer, different lock directories, remote hosts, and direct API writers evade serialization. Issuance and shared JSONL ledger access are not shown transactionally protected.

**Fix:** Lock on normalized customer identity before all local reads/checks/writes; use transactional ledger/state coordination and a defined single-host writer boundary. Re-read live under lock; explicitly handle external drift.

### 6. Ambiguous or partial mutation failure — T/R — High

**Current:** All services validate first; commit proceeds sequentially. _commit catches ToolError, fetches live, and reports contracted count-based reconciliation without retry. IDs are consumed before validation/execution, so even a validation failure burns the ID.

**Residual:** Budgets can land before later services fail. Equal operation counts do not prove identical operations. Partial-failure responses merely set a boolean; validation partial errors may be ignored. Guard bypasses _commit reconciliation. Other exceptions, crashes, or audit failures can obscure success.

**Fix:** Persist per-operation intent/outcome, inspect validation and commit partial errors, reconcile exact resource fields/negative identities, and mark unresolved outcomes unknown. Apply this to every mutation path; prohibit automatic retries until resolved.

### 7. Secret leakage and weak accountability — I/R — High

**Current:** Contracts require HTTP/protocol error scrubbing; meter excludes arguments/results. Execute returns reduced result counts. Audit and git record actions.

**Residual:** _commit writes str(e) directly to audit before protocol scrubbing; audit reasons/summaries and git commit messages are unsanitized. Local logs are mutable; an agent-written reason is not actor identity. Returned queries can reach external model services.

**Fix:** Use structured allowlisted audit fields, scrub before every persistent/output sink, omit raw provider bodies, protect/rotate logs, and record authenticated actor plus plan/attempt IDs. Treat client data disclosure separately from secret redaction.

### 8. Malicious state or filesystem content — T/D/E — High

**Current:** Required keys, customer/campaign identity checks, budget bounds, shared-budget refusal, negative match types, and per-run operation limits exist. Policy replaces embedded caps when present.

**Residual:** State validation is shallow; the protocol contract checks required fields/top-level types, not enums or nested items. Non-finite numbers, wrong types, oversized/nested JSON, negative arrays, and unvalidated date strings can fail or bypass intended semantics. NaN budget comparisons can suppress planning. Policy is optional. Symlink/path defenses and git-hook behavior are not shown.

**Fix:** Require policy; strictly validate finite values, identifiers, real dates, enums and nested items; bound input/file/response sizes. Use trusted paths, reject symlinks where appropriate, and disable untrusted git hooks/config during commits.

### 9. Supply-chain compromise — T/E — Critical

**Current:** Contract limits runtime dependencies to stdlib plus optional lazy Google authentication imports.

**Residual:** No supplied evidence of pinned transitive dependencies, verified hashes, signed releases, or provenance. A compromised package, release, Python environment, or op executable inherits credential authority.

**Fix:** Publish hash-locked dependencies and SBOM, sign release artifacts with verifiable provenance, protect release CI, and document verification plus patch updates. Pins alone do not prove safety.

### 10. Incremental denial of wallet — D/T — High

**Current:** Absolute per-campaign budget cap, maximum end date, per-run operation limit, lifetime enable check, and scheduled spend-pause thresholds.

**Residual:** Small increases cannot exceed the absolute budget cap when policy is enforced, but repeated changes can reach it, extend duration, and aggregate spend across campaigns. No rolling aggregate exposure/change budget exists. Re-enabling after a daily pause is not blocked by daily spend; guard delays allow continued delivery.

**Fix:** Add account-wide rolling exposure/change limits, cumulative increase approvals, cooldowns, and a daily-cap pause latch reset by account billing day or authorized review.

### 11. Negatives sabotage traffic — T/D — High

**Current:** Negative match types, normalization/deduplication, per-run operation cap, and conditional plan checks.

**Residual:** Repeated batches or broad negatives can eliminate valuable traffic. Negatives never count as exposure increases, so stale guards do not block them; spend rails do not protect revenue. The pause-only guard deliberately bypasses allow_commit, git and plan gates; writable legacy thresholds can therefore authorize disruptive pauses even with ordinary commits disabled.

**Fix:** Require immutable policy for guard decisions and document its independent pause authority. Separate negative-management permission; protect core terms, bound cumulative additions, require independent approval for broad/high-impact changes, and provide reviewed rollback with traffic monitoring.

## 3. Public risk-page wording

- AdRails cannot guarantee a hard spend ceiling. Thresholds trigger pause attempts; reporting delays, scheduling gaps, API outages, and Google delivery rules can cause overspend, including daily delivery up to twice an average daily budget where applicable.
- AdRails cannot constrain an agent or user that can access the same credentials, alter its policy/runtime, or administer its host.
- Plans and clean git history do not prove human approval or protect against prompt injection.
- AdRails cannot guarantee atomic multi-service changes, exactly-once Google execution, or immediate knowledge of an ambiguous outcome.
- Local locks cannot control other hosts, Google Ads UI users, or direct API clients.
- AdRails cannot guarantee traffic quality, revenue, or protection from harmful but permitted negatives and budget changes.
- Self-hosting does not prevent disclosure to your MCP client/model provider. You remain responsible for credentials, host security, scheduling, backups, and release verification.

## 4. Ranked Phase 0 fixes and acceptance tests

1. **Establish a real privilege boundary; require policy.** Test from the agent OS identity: policy/config/code/ledger/heartbeat writes and credential reads fail, direct credential-backed API access fails, while authorized MCP operations work.
2. **Make planning/commit serialization transactional.** With deterministic interleavings, race two set calls and replay one ID: no lost update, at most one execution. Swap policy between parse/hash and change config/live state: reject mismatches. Crash/restart after consumption: replay stays refused.
3. **Close spend/guard gaps.** Fake time/API tests cover missing, stale, future, wrong-campaign and failed-pause heartbeats; all increases fail closed. Daily-cap re-enable fails. Many sub-cap increments across campaigns hit an aggregate limit.
4. **Reconcile exact outcomes everywhere.** Inject timeout-after-success, second-service failure, validation/commit partial errors, and guard failure. Assert correct per-operation landed/not-landed/unknown outcomes, no false success, and zero blind retries.
5. **Harden untrusted inputs and output sinks.** Fuzz JSON/types/dates/non-finite values and oversized negatives; reject safely before writes. Inject token canaries into provider errors/reasons: none reach logs, git, stderr or MCP output. Injection strings and broad-negative requests cannot bypass independent authorization.

These are acceptance criteria, not claims of passing tests.
