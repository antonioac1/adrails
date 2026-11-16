# Microsoft Ads (Bing Ads): the read-only monitor

AdRails reads Microsoft Advertising accounts so an agent can watch delivery and stats in detail. It cannot change or spend
anything there. There is no write tool, and the client underneath has no create, update or delete method. You can hand
these tools to a monitoring agent (a daily check, a reporting bot, a second model) without giving it the ability to touch
your campaigns.

## 1. What you get (6 tools, plus Microsoft in the metrics history)

| Tool | Answers |
|---|---|
| `adsops_msads_accounts` | Who am I signed in as, and which ad accounts can I see (number, id, customer id, status, currency)? |
| `adsops_msads_campaigns` | Each campaign's status, daily budget, bid strategy and max CPC cap, time zone and tracking suffix |
| `adsops_msads_structure` | One campaign's full build: ad groups (status, network), every keyword (match type, status, editorial approval), every ad (status, approval, final URLs, headlines) and the campaign's negative keywords. Answers "is this set up right, and can it serve?" |
| `adsops_msads_performance` | Impressions, clicks, spend and conversions per day at campaign, ad group or keyword level, with totals, CTR, average CPC and cost per conversion |
| `adsops_msads_search_terms` | What people actually typed, summed and sorted by spend, with the matched keyword, plus a list of terms that spent with no conversion |
| `adsops_msads_health` | Everything that stops an Active campaign from serving or wastes its money, as critical / warning / info findings (see section 5) |

`adsops_metrics_pull` also stores Microsoft campaign and search-term days in the local metrics history (source
`microsoft_ads`). They use the same metric names as Google Ads (`cost`, `clicks`, `impressions`, `conversions`), so
`adsops_metrics` and `adsops_impact` compare the two platforms side by side.

## 2. What you need

1. **A developer token.** In Microsoft Advertising, go to Tools, then Developer settings / Developer Portal
   (https://developers.ads.microsoft.com), and request one. It is a 16-character string. Production tokens can read any
   account the signed-in user can see.
2. **One sign-in, by a user who can see the account.** Which kind depends on how you log in to ads.microsoft.com:
   - **"Sign in with Google"** → `provider: "google"`. Create a Google Cloud OAuth client of type **Web application**
     with an authorized redirect URI of `http://localhost:8765/oauth/callback` (any free localhost port works, as long as
     the config matches). Then open the OAuth consent screen and set **Publishing status: In production**. No Google
     review is needed for the `openid email profile` scopes. **In "Testing", Google expires the refresh token after 7 days.**
   - **A Microsoft account or work account** → `provider: "microsoft"`. Register an app in Microsoft Entra
     (portal.azure.com → App registrations → New registration). Supported accounts: "any organizational directory and
     personal Microsoft accounts". Redirect: Public client/mobile & desktop,
     `https://login.microsoftonline.com/common/oauth2/nativeclient`. Then open Authentication and set **Allow public client
     flows = Yes**. No client secret is needed.

## 3. Config

Add a top-level `microsoft_ads` block. It needs no campaigns or desired-state files, because it never writes. A config
with only this block (and `"accounts": []`) is valid.

```json
"microsoft_ads": {
  "developer_token": {"kind": "env", "name": "MSADS_DEVELOPER_TOKEN"},
  "auth": {
    "provider": "google",
    "client_id": "1234-abc.apps.googleusercontent.com",
    "client_secret": {"kind": "env", "name": "MSADS_GOOGLE_CLIENT_SECRET"},
    "redirect_uri": "http://localhost:8765/oauth/callback",
    "refresh_token_file": "~/.adsops/msads-refresh-token"
  },
  "accounts": [{"number": "G123ABCD", "account_id": 187000000, "customer_id": 255000000}]
}
```

- **Secret sources** (`developer_token`, `client_secret`): `{"kind": "file", "path"}`, `{"kind": "env", "name"}` or
  `{"kind": "1password", "ref", "token_file"}` (a 1Password service-account token file, for unattended runs).
- **`provider: "microsoft"`** drops `client_secret` and `redirect_uri`.
- **`accounts`** is optional:
  - Omit it to read every account the sign-in can see.
  - A list of numbers (`["G123ABCD"]`) is an allow-list.
  - Pinned objects (`number`, `account_id`, `customer_id`, which `adsops_msads_accounts` prints) skip account discovery
    entirely. Daily monitoring then only needs the campaign and reporting endpoints.

## 4. Sign in once

```
adrails msads-auth --config ~/.adsops/config.json
```

- **google:** prints a link. Open it in a browser signed in to the Google account you use for Microsoft Ads and click
  Allow. A one-shot listener on `127.0.0.1` (never a public interface) catches the redirect.
- **microsoft:** prints a short code for microsoft.com/devicelogin, which works from any device, including a phone.

The refresh token is saved to `refresh_token_file` with mode 0600. It never appears in a tool result, an error or a log.

- **Microsoft refresh tokens rotate on every use.** AdRails writes the new one back, and any use within 90 days keeps it alive.
- **Google refresh tokens do not rotate.** They last until revoked, once the Google OAuth app is published.

If the token is ever rejected, run `msads-auth` again.

**Guard the token file like a password.** The tools are read-only, but the token is not. Microsoft only offers the
`msads.manage` scope, and a Google sign-in token is accepted by the Microsoft Ads API the same way, so anyone who copies
that file plus your developer token could make changes with their own software. Keep it on a machine you control, with
its 0600 mode.

## 5. The health check (built for a daily monitoring agent)

`adsops_msads_health` reads the account and every campaign, then checks the structure of Active **Search** campaigns, the
last 2 days of stats and today's spend. Other campaign types (Performance Max, Shopping, Audience) are listed as
`not_structure_checked`, because their ad groups and ads work differently and checking them the same way would raise
false alarms. Findings:

| Severity | Finding |
|---|---|
| critical | The account is not Active (for example paused for billing), so nothing in it can serve |
| critical | A campaign is Suspended (usually billing or policy) or paused because its budget ran out |
| critical | The campaign is Active but has no Active ad group, so nothing can serve |
| critical | An Active ad group has no serving keyword, or no serving ad (all paused or disapproved) |
| warning | Keywords or ads disapproved by editorial review (with examples) |
| warning | Negative keywords that block your own keywords |
| warning | An Active ad group has no keywords at all, so it cannot serve (dynamic search ad groups are exempt) |
| warning | Active but 0 impressions in the window (new campaigns can take hours; check bids, budget, approvals and payment) |
| info | Impressions unknown in the window (the report returned non-numeric values), so delivery is not confirmed |
| info | Today's spend reached the daily budget (Microsoft can deliver up to 2x on a single day, averaged over the month) |
| info | No final URL suffix, so leads will not carry utm or campaign ids into your CRM |
| info | Ad group on Bing-owned sites only (syndicated search partners off) |

Campaigns you paused yourself are not alarms. `ok` is false only when there is a critical finding.

A simple agent recipe: once a day, call `adsops_msads_health`. If `ok` is false, alert a human with the findings. Otherwise
post `adsops_msads_performance days=1` and the top 10 from `adsops_msads_search_terms days=7`.

## 6. Notes and limits

- **API:** the Microsoft Advertising v13 JSON REST API (Customer Management, Campaign Management, Reporting), stdlib only,
  no SDK. Ids are returned as strings, exactly as the API sends them.
- **Report empty days:** Microsoft returns no report file when there is no traffic in the window. AdRails reports that as
  zero rows, not an error. The metrics history writes explicit zero days for known campaigns.
- **Unknown values:** a report cell Microsoft fills with a placeholder (`--`, `N/A`, empty) is unknown, not zero. Totals
  over it are `null` and listed under `incomplete`, ratios that need it are `null`, and the metrics history leaves that day
  and metric empty instead of writing a zero.
- **Report dates:** reports use the account's own time zone (its campaigns' `TimeZone`), so days line up with the Microsoft
  Ads UI and with Google Ads' account-local days. "Today" in a tool's default window is the account's date in that time
  zone (this machine's local date when the zone is unknown). With `day_to` and no `day_from`, `days` counts back from `day_to`.
- **Network errors:** these are retried with backoff (429 and 5xx too). A DNS failure is named as such, with the host it
  could not resolve.
- **Not covered yet:**
  - Audience and Shopping campaign reports beyond the campaign list
  - Ad extensions (assets)
  - Account-level properties such as msclkid auto-tagging
  - Writing of any kind (by design)
- **Live proof:** [LIVE-PROOF-MSADS-2026-11-16.md](LIVE-PROOF-MSADS-2026-11-16.md).
