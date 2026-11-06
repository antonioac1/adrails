# OpenAI (ChatGPT) Ads in AdRails

Status: **beta. Built and tested offline against a mock of the documented API. It has not yet run
against a live OpenAI Ads account.** The first live run is read-only (see "First run with a real key").

AdRails can now run ChatGPT Ads under the same rails as Google Ads:

1. You commit the campaign's desired state to git.
2. Caps live in a policy file that no tool writes.
3. `plan`, then validate, then commit with a single-use `plan_id`.
4. A scheduled guard can only pause.

The tools are named `adsops_openai_*`.

API docs: https://developers.openai.com/ads (OpenAPI spec: https://developers.openai.com/ads/openapi.json).

## 1. Create the API key (one per ad account)

1. Open Ads Manager → **Settings → API Keys** (https://ads.openai.com/settings) for the ad account.
2. Create a key. OpenAI scopes each key to **one** ad account. Accounts you add later each get their own key.
3. Store it in 1Password, for example at `op://<vault>/OpenAI Ads - <Account>/credential` (a plain-text
   value). As a fallback, store it in a file with `chmod 600`. AdRails refuses a key file that is group- or
   world-readable.
4. Add billing in Ads Manager. AdRails never touches billing.

## 2. Config

Each `openai_ads` account carries its own credentials:

```json
{
  "key": "acme_openai",
  "platform": "openai_ads",
  "ad_account_id": "adacct_...",
  "mode": "disabled",
  "credentials": {"type": "api_key", "sources": [
    {"kind": "1password", "ref": "op://Vault/OpenAI Ads - Acme/credential", "token_file": "~/.secrets/op-token"},
    {"kind": "file", "path": "~/.secrets/openai-ads-acme.key"}
  ]},
  "capi_key_file": "~/.secrets/openai-capi-acme.key",
  "bulk_validate": false,
  "guard_heartbeat": "~/.adsops/openai-acme.heartbeat",
  "campaigns": [{"key": "acme_chatgpt", "state_file": "~/ads/acme_chatgpt.openai.json",
                 "policy_file": "~/ads/acme_chatgpt.openai.policy.json"}]
}
```

| Field | Meaning |
| --- | --- |
| `mode` | `disabled`: no key yet. Nothing runs, and the guard reports "disabled". `read_only`: reads, plans and validate-only runs, plus guard pauses. `live`: commits allowed (still subject to `commit_policy.allow_commit`). |
| `credentials.sources` | Tried in order. Supported kinds: `1password` (`op read` with a service-account token file), `file`, and `env`. |
| `capi_key_file` | Where the Conversions API key lives. It is needed only for `adsops_openai_test_event`. |
| `bulk_validate` | Set `true` only if OpenAI has enabled the Bulk API (limited preview) on the account. |
| `guard_heartbeat` | One per OpenAI account. It is written only after every campaign in that account passed a guard run, and it is separate from the Google heartbeat. |

You can configure any number of `openai_ads` accounts.

## 3. Policy file (immutable caps)

```json
{
  "platform": "openai_ads", "ad_account_id": "adacct_...", "campaign_key": "acme_chatgpt", "currency": "USD",
  "caps": {"max_daily_budget_usd": 40, "min_daily_budget_usd": 5, "max_lifetime_budget_usd": 500,
           "max_cumulative_spend_usd": 500, "max_account_spend_usd": 500, "account_spend_since": "2026-11-01",
           "max_bid_usd": 5, "max_end_date": "2026-11-30", "max_ops_per_run": 25, "overdelivery_factor": 2.0},
  "guard": {"pause_headroom_usd": 0, "daily_anomaly_multiple": 1.5, "daily_spend_pause_usd": 72},
  "allowed_domains": ["example.com"],
  "required_utm": {"utm_source": "chatgpt", "utm_medium": "paid", "utm_campaign": "{campaign_key}"},
  "allow_activate": false, "allow_archive": false, "approvers": ["Your Name"],
  "approval_max_age_days": 14, "require_plan_id": true, "plan_ttl_s": 900, "max_guard_age_min": 30
}
```

Checked on every plan:

- **Daily budget:** stays between min and max. **Lifetime budget:** at most the lifetime max.
- **End date:** must be set and must not be later than `max_end_date`.
- **Bids:** every ad group uses `fixed_bid` at or under `max_bid_usd`. The Maximize strategies have no bid ceiling, so they are refused.
- **Destination URL:** every `target_url` is https on an allowed domain (no port, no credentials) and carries exactly the required UTM values.
- **Archive:** refused unless `allow_archive` (archiving is irreversible).
- **Ops per run:** at most `max_ops_per_run`.
- **Account checks:** the account currency must be USD, and the API key must belong to the configured `ad_account_id`.

### Cap math (2x overdelivery)

```text
worst_day      = daily_budget x overdelivery_factor        (0 for a lifetime budget)
pause_at       = max_cumulative_spend - max(pause_headroom, worst_day)
account_pause  = max_account_spend    - max(pause_headroom, worst_day)
daily_pause_at = min(daily_budget x daily_anomaly_multiple, guard.daily_spend_pause_usd)
```

- **The guard pauses** when the key's lifetime spend reaches `pause_at`, or today's spend reaches `daily_pause_at`.
- **Activation is refused** past those same points, so the guard and activation can never ping-pong.
- **Over the account cap**, the guard pauses every active campaign in the account, including ones made by hand in Ads Manager.

**Overspend factors:** the task brief said "up to 2x per day, 7x over 7 days". That is Google's rule, and I could not find it in OpenAI's Ads docs (see "Unverified"). The factor is a policy setting, defaulting to 2.

## 4. Desired-state file

One file per campaign key. See `adsops/oai_engine.py` for the full schema. In short:

- **campaign:** `name`, `status`, `bidding_type`, `budget.daily_usd|lifetime_usd`, `end_date` (inclusive, in the
  account time zone), and `targeting.countries|location_ids|excluded_location_ids|platforms`.
- **ad_groups[]:** `key`, `name`, `status`, `bidding.strategy|max_bid_usd`, `context_hints`, and its ads.
- **ads[]:** `key`, `name`, `status`, `title` (3-50 characters), `body` (up to 100), `target_url`, and `file_id`.
- **approval:** the spend gate (below).

Rules:

- **New resources are always created `paused`.** Activation is a separate step and comes last: ad, then ad group, then campaign.
- **Every campaign AdRails creates is tagged `adrails:<key>`** in its description. Only one live campaign per key is
  allowed. A rename does not create a second $500 allowance, and spend is summed over all of a key's tagged campaigns.
- **Name matching only converges.** A resource matched by name (no `id` in the file) gets no changes and no children
  until its `id` is recorded. Record the ids that `adsops_openai_apply` returns.
- **`bidding_type` cannot change** (OpenAI won't allow it). A daily budget cannot switch back to lifetime.
- **Placeholder values** that start with `TODO` are refused.
- **Location targeting** covers countries, regions and "markets" (metro areas). Use `adsops_openai_geo_lookup` to
  find ids. OpenAI does not offer counties.

## 5. The spend gate (activation)

A commit that would leave the campaign **active** and add exposure needs all of these. Exposure means an activation, a budget, bid, targeting or schedule change, or a new active ad.

- The policy has `allow_activate: true`. It needs `approvers` and `max_guard_age_min`.
- The committed state's `approval` block has:
  - `activate: true`
  - `campaign_name` equal to the campaign's name
  - `approved_by` set to a name in the policy's `approvers`
  - `approved_on` no later than today, and `expires_on` no earlier than today, at most `approval_max_age_days` apart
  - `max_daily_budget_usd` (or `max_lifetime_budget_usd`), `max_bid_usd` and `max_total_spend_usd`, each at or above the plan's values and the total at or under the cap
- **The approval is single-use.** After the guard pauses the campaign, re-activating needs a new approval.
- The OpenAI guard heartbeat is fresh.
- Spend is below `pause_at`, `daily_pause_at` and the account pause point. Spend is read again right before the first activation call.
- The state and policy files are committed and clean. This is checked before and after they are read, inside the account lock.

**Honest limit:** the approval block is text in a file the agent can edit and commit. It records intent and
blocks accidents and MCP-only agents. It cannot stop an agent that has shell access as your user (see
THREAT-MODEL.md). For real protection, also set a spend limit in Ads Manager.

## 6. Tools

| Tool | Writes? | What it does |
| --- | --- | --- |
| `adsops_openai_account` | no | Checks the key (`GET /ad_account`) and shows status and brand/integrity review state. |
| `adsops_openai_status` | no | Shows the live campaign, ad groups and ads (review status, serving issues), spend today and lifetime, the cap, the pause points and the heartbeat age. |
| `adsops_openai_insights` | no | Daily impressions, clicks and spend for 1-90 days, at campaign, ad group or ad level. |
| `adsops_openai_geo_lookup` | no | Looks up location ids. |
| `adsops_openai_plan` | no | Diffs the state against live. Returns ops, notes, exposure, spend-gate blockers and a `plan_id`. |
| `adsops_openai_apply` | commit=true | Validate-only by default: local schema checks, plus Bulk `validate_only` when enabled. A commit runs the rails above. |
| `adsops_openai_guard` | pause only | Pause-only spend guard. It never activates, raises or archives. |
| `adsops_openai_upload_image` | commit=true | Checks a PNG, JPEG or WebP (ad image ≥640x640, favicon ≥256x256), then uploads it and returns a `file_id`. |
| `adsops_openai_brand_update` | commit=true | Sets the advertiser icon, URL or brand name. Needs `acknowledge_delivery_pause=true`, because a brand change can pause all delivery until review. |
| `adsops_openai_conversions_setup` | commit=true | Makes sure a web pixel and a `lead_created` event setting exist (reused by name). It can create a Conversions API key and write it to `capi_key_file` (mode 0600). The key is never returned. |
| `adsops_openai_test_event` | no (validate_only) | Sends one `lead_created` event with `validate_only: true`. It is never saved or counted. |

The Google tools (`adsops_status`, `adsops_plan`, ...) refuse OpenAI campaign keys, and the OpenAI tools refuse Google keys.

## 7. Schedule the guard

```bash
PYTHONPATH=$PKG $PY -m adsops --config $CFG guard-all --only-platform openai_ads
```

Run this every 15 minutes with launchd, systemd or cron, using the templates in `deploy/` with the extra argument. `--only-platform openai_ads` leaves the Google campaigns alone.

The guard:

- does not depend on the desired-state file. It finds the key's campaigns by tag and still pauses when the file is broken, then fails, so the heartbeat goes stale.
- **fails closed when spend is unknown.** An empty insights report, a missing spend field or a missing `created_at` is an error, never $0. No heartbeat is written, so activation stays blocked.

## 8. What this cannot guarantee

- **The guard is not a hard ceiling.** It pauses after a threshold. Delivery can overshoot (factor configurable), and insights normalise to whole hours, so reports lag.
- **The guard runs only when scheduled and the machine is awake.**
- **AdRails sees only spend it can read.** The account cap counts everything, but only if you set it.
- **Keep billing limits in Ads Manager as the real ceiling.** Account spend-limit windows exist only on some accounts.
- **New ads and creative edits go to OpenAI review.** A brand update can pause the whole account until review.

## 9. Unverified against a live account (check on first run)

- **The exact JSON field names in insights rows.** The docs show `spend` in major units for `fields[]=campaign.spend`. If a field is missing, AdRails fails closed.
- **Whether `includes[]=zero_impression_items` works on the campaign insights endpoint.** If it errors, the guard errors (fails closed), and that would need a code tweak.
- **The query style for array parameters.** The docs use `fields[]=...` and the OpenAPI spec names them `fields`. AdRails follows the docs.
- **Overspend:** "2x per day / 7x per week" is not stated in OpenAI's docs. The 2x factor is a conservative assumption.
- **Bulk API:** limited preview. Its create schema differs from the per-resource API, so AdRails validates only updates and status changes through it. Creates are checked locally against the published limits.
- **Image minimums:** the docs disagree on the favicon minimum (128 vs 256 px). AdRails uses 256.

## 10. First run with a real key

1. Store the key and set `mode: "read_only"`.
2. Run `adsops_openai_account`. It should show the right `adacct_` id, USD and America/New_York.
3. Run `adsops_openai_geo_lookup` and put the market id in the state file.
4. Run `adsops_openai_plan` and `adsops_openai_apply` (validate-only). Read the ops.
5. Schedule the guard and confirm the heartbeat appears.
6. Set `mode: "live"`, then commit the paused creates with the `plan_id`. Record the returned ids and commit them.
7. Activate only via an approval block written by the account owner.
