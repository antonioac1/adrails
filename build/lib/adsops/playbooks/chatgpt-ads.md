# ChatGPT Ads playbook: paid placements inside ChatGPT, run under caps

For the owner's Claude. Read all of it, then run it step by step with the AdRails tools named in each step.

ChatGPT Ads (OpenAI Ads) are new. They are contextual placements inside ChatGPT, not search ads. There are no keyword targets and no negative keywords. You choose where the ad may appear (countries, regions, markets, postal codes), write hints about the situations it fits, and write the creative. AdRails runs them under the same rails as Google Ads: desired state in git, caps in a policy file no tool writes, plan, then commit.

Ground rules before you start:

- This platform is young and our record of it is short. Say "early evidence" when a claim comes from a few weeks of work, and "untested" where it is untested.
- Part of the AdRails adapter has run against a live account read-only. Creates, image upload, activation, conversion commits and audience commits had not been exercised live at the time of writing. Say which half, every time.
- Never write an approval block, and never touch billing. The owner writes the approval and sets the spend limit in the Ads Manager interface, which is the real ceiling. Draft, then wait.
- Facts in the ads must be true and sourced. A wrong price or a wrong service area is repeated by the platform and is hard to correct.

---

## Fast path: first 7 days (a business that has never advertised on ChatGPT)

Goal: one capped, paused campaign that a human can turn on, with attribution that works before money is spent.

| Day | Do | Tool | Time | Done when |
|---|---|---|---|---|
| 1 | Confirm the ad account, its currency and its time zone. | `adsops_openai_account` | 10 min | Right account id, USD, expected time zone |
| 1 | Freeze the offer in one line: landing page, price statement, one service, one trade. | Owner decides, Claude writes | 20 min | One sentence the owner approved |
| 1 | Write the policy file by hand: caps, allowed domain, required UTMs, activation off. | Claude drafts, owner owns the file | 30 min | File exists and no tool can write it |
| 2 | Draft the campaign from a brief: paused, conservative caps, tagged URLs. | `adsops_openai_draft_campaign` | 30 min | State and policy files returned and readable |
| 2 | Find the target ids for the areas you sell in. | `adsops_openai_geo_lookup` | 20 min | Ids in the state file, names read back |
| 3 | Write 2 to 4 ads, with one copy pair for an image test. | `adsops_openai_ad_check` | 45 min | No errors, warnings read one by one |
| 3 | Upload the ad image and the brand icon. | `adsops_openai_upload_image` | 20 min | A `file_id` for each, sizes accepted |
| 4 | Set the brand icon, URL and brand name. | `adsops_openai_brand_update` | 15 min | Preview correct, delivery pause acknowledged |
| 4 | Check the landing page echoes the promise, with tags intact. | `adsops_lp_diagnose` | 20 min | Page loads, no findings left |
| 5 | Make sure the pixel and the lead event exist, then send one test event. | `adsops_openai_conversions_setup`, `adsops_openai_test_event` | 30 min | Event setting exists, test accepted and not counted |
| 6 | Plan, validate-only apply, then schedule the guard every 15 minutes. | `adsops_openai_plan`, `adsops_openai_apply`, Owner for cron | 40 min | No unexpected exposure, heartbeat file ages |
| 7 | Activate ad, then ad group, then campaign, on a written approval. | Owner writes approval, `adsops_openai_apply` | 30 min | Active, guard run clean, spend tomorrow non-zero |

Do not skip the guard on day 6. A live campaign without a fresh heartbeat is a campaign nobody is watching.

---

## 1. What is proven and what is not

| Capability | Status | What it means for you |
|---|---|---|
| Account read, key ownership, currency | Live verified | Trust it |
| Status, spend today and lifetime, cap and pause math | Live verified | Trust it, then reconcile clicks with your own logs |
| Insights reads | Live verified after one fix | The end of a range must be a full hour in the account time zone, and it rounds up on purpose |
| Geo lookup and validation refusals | Live verified | Read canonical market names, validate before every commit |
| Creates, image upload, activation | Not yet live | Read the first run closely, do not batch it |
| Conversions commit, audience commit | Not yet live | One test event first, then one real lead |

An agent that says "it works" when half of it has only run against a mock is misleading the owner. Say which half.

---

## 2. Account, key, and the caps

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 2.1 | Create one API key per ad account in the platform's own settings. | Owner | 15 min | Key in a password manager, not in a repo |
| 2.2 | Hold the account read-only until the plan is boring. | Claude edits config | 10 min | No live commits possible |
| 2.3 | Run the account check. | `adsops_openai_account` | 5 min | Id matches the configured account |
| 2.4 | Put the Conversions API key in its own file, owner-only permissions. | `adsops_openai_conversions_setup` | 10 min | File exists, key never printed |

A key is scoped to one ad account, so a second account needs a second key. Read-only mode still allows reads, plans, validate-only runs and guard pauses, and it blocks commits. An account read that fails behind a Cloudflare block is usually the client's user agent header, not the key, so check that before debugging credentials.

The policy file holds every cap and no AdRails tool writes it. The owner edits it. It sets the daily and lifetime budget ceilings, the total spend ceiling, the bid ceiling, a required end date, allowed domains, required UTM values, the approvers list and the maximum guard heartbeat age. Bids must be fixed bids at or under the ceiling, because the Maximize strategies have no bid ceiling and are refused on purpose. Currency must be USD and the key must belong to the configured account, both checked on every plan. If the platform offers a spend credit with a deadline, the deadline does not override the cap: set the cap first, then decide how much of it to spend before the deadline.

Done when: the owner can read the file and say what the worst case month costs.

---

## 3. Draft the campaign paused and capped

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 3.1 | Assemble the brief: account, landing page, offer, daily and lifetime budget, cap, the situations people ask about, 2 to 4 draft ads. | Claude writes | 45 min | One page, no placeholder text |
| 3.2 | Turn the brief into a state file and a policy file, everything paused. | `adsops_openai_draft_campaign` | 20 min | Both documents returned, saved only if the owner asks |
| 3.3 | Read the draft to the owner in plain English: what it spends, where it shows, what it says. | Claude reports | 15 min | Owner says yes or edits |

New resources are always created paused, and activation is separate and last: ad, then ad group, then campaign. Values starting with `TODO` are refused, which catches an agent that meant to come back later. The bidding type cannot be changed later and a daily budget cannot switch back to lifetime, so get it right in the draft. Every campaign AdRails creates is tagged with the campaign key: one live campaign per key, spend summed across a key's tagged campaigns, and renaming does not buy a second allowance. A resource matched by name and not by id gets no changes and no children until its id is recorded, so record the ids that apply returns.

Done when: `adsops_openai_plan` returns ops that mostly say create, with nothing that raises exposure on a live object.

---

## 4. Creative: limits, checks, images, one honest test

`adsops_openai_ad_check` is a pure check. It reads no account and writes nothing. Run it on every ad.

| Limit | Value |
|---|---|
| Title / body | 3 to 50 characters / up to 100 characters |
| Target URL | https, up to 2048 characters |
| Images | 1, or 4 to 6 for a chat card |
| Domain and UTMs | must match the policy exactly |

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 4.1 | Check the ad set against the limits and the policy. | `adsops_openai_ad_check` | 10 min | No errors |
| 4.2 | Fix warnings one at a time: caps, superlatives, phone numbers, near-duplicate headlines. | Claude edits | 20 min | Only warnings you can defend remain |
| 4.3 | Check every claim against the site: price, area, licence, any disclosure the trade requires. | Claude and owner | 30 min | Each claim has a page it matches |
| 4.4 | Make two images of the same idea, one person or product, one place or result, and keep the copy identical. | Owner or Claude | 45 min | Two images, same size, both over the minimum |
| 4.5 | Upload each image, then set the brand icon and website URL. | `adsops_openai_upload_image`, `adsops_openai_brand_update` | 30 min | A `file_id` per image, preview correct |
| 4.6 | After enough traffic, compare the ads. | `adsops_openai_ab_report` | 10 min | Rates with intervals, a leader, or an honest "too early" |

Creating a chat card needs an image `file_id`, which is a hard error rather than a warning, so an ad without one cannot be created at all. An account can be rejected for a missing brand icon, and a brand change can pause all delivery until review: do brand work once, early, and keep the icon readable at small sizes. The ad report gives per-ad rates with 95 percent intervals, a leader, and comparisons corrected for multiple looks, and it recommends a pause only for an ad that is significantly worse. Never call a winner from a handful of clicks, and never compare two ads that differ in both image and copy, because that is two changes and no lesson.

Done when: the report names a leader you can defend, or says the sample is too small and sets a date to look again.

---

## 5. Targets: places and context hints

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 5.1 | Look up the places you sell in. Countries, regions and markets only, no counties. | `adsops_openai_geo_lookup` | 20 min | Ids recorded, canonical names read |
| 5.2 | Check what a market actually contains. A metro name often bundles cities the owner does not want. | Claude reads the name | 10 min | Written note: included or excluded, and why |
| 5.3 | Write 5 to 15 context hints in the voice of the person asking, not the seller. | Claude writes, owner edits | 30 min | Hints read like a customer sentence |
| 5.4 | Split local and out-of-area into separate campaigns when the offer differs. | `adsops_openai_draft_campaign` | 30 min | One campaign per idea, one audience each |

Read the canonical name before trusting an id, because a market can bundle a town the owner refuses to serve. Postal-code targeting is possible and precise, but it needs a real list that maps to the service area, so never invent one. There is no negative keyword list, so exclusion is done with geography and hint wording: write hints that cannot be read by the wrong person. Target the person, not the property. Someone who lives elsewhere can own a building in your town, and that buyer needs a campaign and copy of their own. Early evidence from one account.

Done when: every geo id has a written reason and no hint makes a claim you cannot keep.

---

## 6. Plan, validate, commit, activate

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 6.1 | Diff the state against live. | `adsops_openai_plan` | 10 min | Ops, exposure list and spend-gate blockers read |
| 6.2 | Apply validate-only. | `adsops_openai_apply` | 10 min | Local schema checks pass, no write |
| 6.3 | Commit with the `plan_id`. | Owner approves, `adsops_openai_apply` (commit=true) | 15 min | Created paused, ids recorded and committed |
| 6.4 | Activate in order: ad, ad group, campaign. | Owner writes the approval, `adsops_openai_apply` | 20 min | Active, guard run clean |

A commit that leaves the campaign active and adds exposure needs all of this: activation allowed in the policy with named approvers, an approval block written by the owner (campaign name exactly, a named approver, dates inside the maximum window, caps at or above the plan), a fresh guard heartbeat, spend below the pause points read again right before the first activation call, and clean committed state and policy files.

Honest limit: the approval block is text in a file the agent can edit. It records intent and stops accidents. It cannot stop an agent with shell access as your user. The spend limit in the Ads Manager interface is the only real ceiling. Say that to the owner once, out loud.

Done when: live status shows the campaign active, the ad approved, and the guard sees it.

---

## 7. Tag every click and ask every lead

ChatGPT clicks often arrive with no referrer at all. Some UTMs survive, many do not. This is the biggest attribution trap on this platform.

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 7.1 | Put the policy's required UTM values on every target URL, and let the platform add its own click id. | `adsops_openai_ad_check` | 10 min | Every URL passes the UTM check |
| 7.2 | Record the click id on the landing page and in the intake form. | Claude edits, owner approves | 30 min | A test click shows the id in the lead record |
| 7.3 | Add one lead form question: "If an AI assistant sent you, which one, and what did you ask it?" | Claude edits form | 20 min | Field live, answers stored |
| 7.4 | Log every lead with its source, and reconcile clicks monthly. | `adsops_leads` | 5 min per lead, 15 min monthly | Pipeline by source works, two click counts explained |

The click id parameter is reserved, so adding it to a URL template yourself is wasted effort and can break the template. A monitor built on incomplete field requests once reported zero clicks while real paid clicks landed: ask for impressions, clicks and spend explicitly, and date rows by start time in the account time zone rather than by a label that may be absent. A tagged external visit should clear older tags and never inherit them, last non-direct click wins, and the cross-channel journey is where this breaks. When the referrer is missing, the form question is the only attribution worth quoting.

Done when: one real lead from this campaign carries its click id, its UTM and its stated source.

---

## 8. Conversions, audiences, hashed data

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 8.1 | Make sure a web pixel and a lead event setting exist. | `adsops_openai_conversions_setup` | 20 min | Both exist, reused by name if present |
| 8.2 | Send one test event. | `adsops_openai_test_event` | 10 min | Accepted, never counted |
| 8.3 | Send real leads from the lead book. | `adsops_openai_send_conversions` | 20 min | Validate-only read, then committed once |
| 8.4 | Build an exclusion audience of current clients. | `adsops_openai_audience` | 30 min | Hashed file uploaded, counts match the list |

Emails, phones and names are normalised and hashed locally with SHA-256, so raw personal data never leaves the machine. Events older than seven days are skipped, so send leads promptly or accept the gap, and event ids are deterministic so a re-send dedupes instead of double counting. Validate-only first, always, because the first real send is what teaches you the field names. Audiences are for exclusion and look-alike seeds, not for chasing people who already bought. Until conversions exist, bids and hints are guesses, so say that instead of dressing a guess as a strategy.

Done when: one real lead appears as a conversion and the count matches the lead book.

---

## 9. The guard pauses, it does not cap

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 9.1 | Run the guard validate-only to see its decision. | `adsops_openai_guard` | 5 min | The action and the reason both read |
| 9.2 | Schedule it every 15 minutes for this platform only. | Owner (cron, launchd or systemd), Claude writes the entry | 20 min | Heartbeat file updates |
| 9.3 | Confirm the heartbeat age in the status output. | `adsops_openai_status` | 5 min | Age is minutes, not hours |

It pauses. It never activates, never raises a budget and never archives. It does not depend on the state file: it finds the key's campaigns by tag, so it still pauses when the state file is broken, then fails so the heartbeat goes stale. It fails closed when spend is unknown, because an empty report, a missing spend field or a missing timestamp is an error and never zero. Past the account cap it pauses every active campaign in the account, including ones made by hand in the interface.

Honest limits, tell the owner plainly:

- It is not a hard ceiling. It pauses after a threshold, delivery can overshoot between runs, and reports normalise to whole hours so they lag. Assume some overspend is possible.
- It runs only when scheduled and the machine is awake. The 2x per day overspend assumption is a conservative policy choice, not a statement from the platform's documentation.
- The real ceiling is the spend limit set in the platform's interface. Set it.

Done when: the owner has seen the pause point in plain numbers and knows what happens when it is reached.

---

## 10. Read the numbers without fooling yourself

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 10.1 | Daily impressions, clicks and spend at ad level. | `adsops_openai_insights` | 10 min | Rows dated in the account time zone |
| 10.2 | Which ad is ahead, with intervals. | `adsops_openai_ab_report` | 10 min | A leader or an honest "too early" |
| 10.3 | Status, spend and serving issues. | `adsops_openai_status` | 5 min | Review state and any serving issue read |
| 10.4 | Cost per lead across every channel on one ruler, after checking conversion tracking. | `channel_cac`, `adsops_conv_audit` | 30 min monthly | One table, small numbers labelled, dead tags fixed |
| 10.5 | Check the landing page echoes the ad and the place. | `adsops_lp_diagnose` | 10 min | Findings fixed, worst first |

Two leads in a week is two leads, and it does not prove a channel works, so write the number and the sample size together. A reported conversion for a direction request or a page view is not a lead: keep form fills, calls over a minute and click to call as primary. Previews and creative review sit behind a human check in the platform's interface, so never claim an agent verified a preview. Phase the goal: first prove the flow from click to lead to contact to sale, then push cost per click down once leads exist. Early evidence, a judgment call.

Done when: the owner gets one short paragraph with spend, clicks, leads, cost per lead and the sample size.

---

## 11. Cadence

### Week 1
Run the fast path. Nothing activates without the guard running and a written approval.

### Days 8 to 30
- Daily, 10 minutes: `adsops_openai_status` and `adsops_openai_guard`. Spend and serving issues.
- Weekly, 30 minutes: `adsops_openai_insights`, `adsops_openai_ab_report`, then one creative or hint change at most. One change per week or you learn nothing.
- Weekly: reconcile clicks with first-party landings. A zero that is really a reporting gap is the expensive mistake on this list.

### Days 31 to 90
- Keep the daily guard and the weekly read. Do not rewrite ads after one noisy week, change when two weeks agree, and add a second campaign only when the first is inside its cap and producing leads.
- Decide by phase. If the flow is proven, work cost per click with bids and hints. If it is not, fix the landing page and the offer instead of tuning bids.

### The weekly routine in one list (about 15 minutes of agent work)
1. `adsops_openai_status` 2. `adsops_openai_insights` 3. `adsops_openai_ab_report` 4. `adsops_openai_guard` 5. `channel_cac` monthly.

---

## 12. The scoreboard: five numbers

1. Spend today and month to date, against the cap and the pause point.
2. Clicks and click to landing reconciliation. Two counts that agree, or a written reason they do not.
3. Leads from this campaign with the click id and the stated source, not just the counter.
4. Cost per lead next to every other channel on one ruler, with the sample size.
5. Guard heartbeat age, and activations this month. Every activation traces to one written approval.

---

## 13. Myths and mistakes

- "No keywords means no targeting." Wrong. Places and context hints decide who sees the ad.
- "Spend the credit before the deadline." Only inside the cap. A credit is not a reason to break the ceiling.
- "The guard is a hard cap." It pauses. Delivery can overshoot and reports lag by an hour or more.
- "The dashboard says zero clicks, so it did not run." Check the requested fields and reconcile with your own logs first.
- "ChatGPT clicks show up in analytics." Often there is no referrer. Tag everything and ask the lead.
- "Two ads, one winner." Only with intervals and enough clicks, otherwise you are reading luck, and more ads only split a small budget.
- "Half of this was proven live." Reads, status, spend, cap math and geo lookup were. Creates, image upload, activation and conversion commits were not, at the time of writing.
- "An agent can approve its own spend." The approval block is text in a file. Set the platform spend limit.

---

## 14. Rules for Claude while running this

- Read first. Every OpenAI tool is read-only except the ones with commit=true. Validate before committing, every time.
- Never edit the policy caps and never write an approval block. Draft it, hand it to the owner.
- Never activate without a fresh guard heartbeat and a spend reading taken right before the call.
- If a fact is missing, ask. Do not fill in a price, an area or a licence.
- Label every claim "live verified", "early evidence" or "untested". Report in plain English: what changed, what is still owed, and the one next step.
