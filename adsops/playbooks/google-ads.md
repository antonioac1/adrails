# Google Search Ads playbook: leads from a small budget, measured honestly

For the owner's Claude. Read all of it, then run it step by step with the AdRails tools named in each step.

Google Search Ads for a small local business answers one question: does a click cost less than the business can afford to pay for a lead? Everything below serves that question. The work has two halves. Make the numbers true (tracking and attribution), then make the clicks cheaper and better (terms, negatives, landing pages, copy). Doing the second half on top of false numbers wastes the owner's money and your credibility.

Ground rules before you start:

- Tag every claim you make to the owner as "tested" or "early evidence". Most of what is in this file came from reading a handful of small accounts, one of them in detail over a few weeks. Where the sample is one account or one week, this file says so.
- Read-only first. `adsops_status`, `adsops_terms`, `adsops_diagnostics`, `adsops_conv_audit`, `adsops_lp_diagnose`, `adsops_competitors_report` and `adsops_drift` change nothing. Run them before you touch anything.
- No agent can enable a campaign or raise exposure alone. Caps and guards live in a owner-owned policy file that `adsops_set` cannot write. Exposure increases are refused while the guard heartbeat is stale. That is deliberate. Do not look for a way around it, and never ask the owner to widen the guards to make a test easier.
- Never quote a cost per lead, a conversion count or a win count to the owner until section 2 is clean. A wrong conversion number is worse than no number.
- Ads and pages must be true and sourced. A price must say what it covers. A number from a third party needs a link and an "as of" date, or it reads "Unverified".
- AdRails never sends anything to the owner's customers. It drafts. The owner or staff sends.

---

## Fast path: first 7 days on an account nobody has cleaned

Goal: know what the account is really doing, stop the obvious waste, and have one honest baseline. Do not restructure anything in week one.

| Day | Do | Tool | Time | Done when |
|---|---|---|---|---|
| 1 | List campaigns and read live state: status, daily budget, spend today and lifetime, caps. | `adsops_campaigns`, `adsops_status` | 15 min | You can say the five numbers out loud without opening the Ads UI |
| 1 | Audit conversion tracking. Name every action that counts, every soft action, every dead tag. | `adsops_conv_audit` | 20 min | A written list: real lead actions vs soft actions vs dead |
| 1 | Pull clicked search terms for 30 days, with clicks and cost. | `adsops_terms` | 10 min | A list, sorted by cost, of what people actually typed |
| 2 | Diagnostics bundle: wasted spend, impression share lost to budget vs rank, quality score. | `adsops_diagnostics` | 15 min | One worst item named, with the evidence line |
| 2 | Landing page diagnosis per ad group, plus a plain HTTP check of each final URL. | `adsops_lp_diagnose` | 15 min | The worst ad group and the reason (relevance, speed, or mismatch) |
| 2 | Ask the owner three questions: did anyone call or text you directly, what does a signed client pay, which leads were junk. | Owner | 20 min | Answers written in one facts sheet |
| 3 | Propose negatives from the terms that cannot become customers. | `adsops_negatives_suggest` | 15 min | A list with cost and click evidence per phrase |
| 3 | Apply only the negatives the owner approved. | `adsops_set` (negatives_add, apply=true) | 20 min | Change committed with a reason, receipt posted |
| 4 | Check what changed outside AdRails, and the state file history. | `adsops_drift`, `adsops_history` | 15 min | You can name every out-of-band edit |
| 4 | Run monitors and produce one short brief. | `adsops_monitor_check`, `adsops_brief` | 10 min | Alerts listed, brief is under one screen |
| 5 | Import an Auction Insights export and read the weekly intel report. | `adsops_auction_insights_import`, `adsops_competitors_report` | 20 min | You know who you overlap and who wins the top slot |
| 5 | Put every recent lead in one book, with a source. | `adsops_leads` (import), `adsops_leads_due` | 30 min | No lead without a source, nobody waiting for a first contact |
| 6 | Draft the first landing page fix. Draft only. | Claude writes, `aeo_page_audit` to check it | 1 hour | Owner has a diff to approve, one change at a time |
| 7 | Write the baseline scoreboard with the date. Include the zero rows. | `adsops_status`, `adsops_terms` | 20 min | Five numbers stored, dated, and readable next week |

After day 7 the weekly loop below applies. A small local account moves slowly. Expect weeks, not days, and say so.

---

## 1. Measure first, and write the baseline down

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 1.1 | Read live state: status, end date, daily budget, spend today and lifetime, negative count, URL fields. | `adsops_status` | 5 min | Numbers copied into the scoreboard with a timestamp |
| 1.2 | Thirty day diagnostics: zero-conversion terms, impression share lost to budget vs rank, cost weighted quality score, day anomalies. | `adsops_diagnostics` | 15 min | One sentence per finding, worst first |
| 1.3 | Read the terms that got clicks, not the long tail that got none. | `adsops_terms` | 10 min | A top ten by cost, with the matched keyword beside each |
| 1.4 | Put the baseline in writing. | Claude writes | 10 min | Dated sheet the owner can read in a minute |

Why the baseline matters: without it, every later claim is a feeling. The zero rows are the most valuable rows, because they are the only honest proof that something changed.

Small sample discipline, and this matters more than any tool: 0 leads from 30 clicks is not proof the page fails. At a normal 4 percent lead rate, that happens about 29 times in 100. At 0 from 60 clicks it is about 9 in 100. Give the owner the odds before recommending a cut, and never let one thin week become a verdict.

## 2. Conversion tracking honesty (the biggest source of wrong decisions)

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 2.1 | Audit the account's conversion actions: primary goals, dead tags, double counting, auto-tagging, smart bidding without enough conversions. | `adsops_conv_audit` | 20 min | Score plus ranked findings, worst first |
| 2.2 | Find soft actions. Google-hosted "Local actions" (website visits, directions, clicks to call) record as conversions but are not leads. If they are primary, the account optimises toward taps. | Owner (Ads UI) | 20 min | Only real lead actions are primary for the bidding goal |
| 2.3 | Name the true conversion precisely: a form submit with the click id attached, a call over 60 seconds, or a CRM stage upload. Write the label, the id and the action type. | Claude writes, owner confirms | 20 min | You can describe every counting action without guessing |
| 2.4 | Hunt test rows. Test submissions sit in the same table as real ones. Any lead carrying a test string in its click id is a test. | `adsops_leads` (find) | 10 min | Test rows labelled or removed before you quote a count |
| 2.5 | Decide on enhanced conversions. Phone-only leads carry no hashed email, so the enhanced leg has nothing to match on. The click id still attributes them. | Owner decides, Claude notes | 10 min | Decision written down, including "do not send raw phone numbers" |
| 2.6 | Verify the offline upload really fires, at its scheduled slot, with a timestamp. A script that exists is not a script that ran. | `adsops_offline_conv_plan` (build, preview) | 15 min | You saw a fired upload, or you flagged it as unverified |

What a real defect looked like: the account reported conversions while the only true lead action reported none. Five Google-hosted actions were primary. Cost per "conversion" looked excellent and meant nothing. Demoting those actions changed the reported numbers without changing a single click, which is the point.

The click-to-lead gap is the second trap. A tag fires only on a successful submit. If the form never submits, the tag never fires, and a "zero conversions" row can be a true zero rather than lost attribution. Prove which one it is: check whether any real lead row carries a click id, and whether any ad landing hit shares an IP and user agent with a lead. Do that before you tell the owner attribution is broken.

Friction before the first field costs leads. In the account we measured most closely, a qualifying question sat in front of the form and zero paid sessions tapped it. That is early evidence from one account, so treat it as a hypothesis to test on the next one, not a law.

## 3. Search terms and negatives

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 3.1 | Pull raw clicked terms for 30 days, with clicks and cost. | `adsops_terms` | 5 min | List sorted by cost |
| 3.2 | Read intent per term: the business's customer, a renter or tenant, a job seeker, DIY and software, out of area, a competitor brand. | Claude reads | 15 min | Each top term has an intent label |
| 3.3 | Propose phrase negatives with cost and click evidence. The tool skips phrases already covered and never suggests protected core words. | `adsops_negatives_suggest` | 15 min | A reviewed list, with the basis note for the state file |
| 3.4 | Add the approved negatives. Negatives are add only, so this is one of the safest writes. | `adsops_set` (negatives_add, apply=true) | 20 min | Receipt posted, rerun shows the phrases excluded |
| 3.5 | Look for new themes by intent, not by volume. | `adsops_keyword_ideas` (seeds) | 10 min | Clusters labelled by intent, one candidate theme chosen |

Rules that came from real spend:

- A term that is wrong for the business is waste at any price. Renter, tenant and job-seeker intent under a broad or phrase keyword is common in property and service trades, and no headline fixes it.
- Do not widen match type to find volume. Add negatives and write ad groups for the intent you actually want.
- Message match is not optional. In one account the single largest spend line was a phrase keyword that the landing headline never echoed. Quality score and conversion rate both suffered for a reason that costs nothing but a sentence.
- Keep own-brand terms in their own place. They click, they convert, they teach you nothing about growth, and counting them as growth is dishonest.
- Check the negatives before you trust them: never negative a word that carries the core service.

## 4. Landing pages: diagnose before you rewrite

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 4.1 | Per ad group: expected CTR, ad relevance, landing page experience, plus status, response time and whether the page echoes the ad group's words and town. | `adsops_lp_diagnose` | 15 min | Worst ad group and its reason named |
| 4.2 | Read the pattern. Below average landing page experience on almost every keyword, with a fast page, means relevance and engagement, not speed. | Claude reads | 10 min | You stopped planning a speed project |
| 4.3 | Check the main page for answer-readiness: first paragraph, question headings, real numbers, missing questions. | `aeo_page_audit` | 15 min | A list of unanswered questions, which become the next headings |
| 4.4 | Write one change. Draft only, then wait for approval. | Claude writes | 1 hour | Owner has a diff, not a redesign |
| 4.5 | After approval and deploy, re-measure the same signals on the same ad group. | `adsops_lp_diagnose` | 10 min | One change, one read, before the next change |

The first screen decides. In the account we read in detail, two thirds of paid sessions left inside ten seconds and almost nobody touched a form field. That was not a bidding problem.

Three fixes that repeat across accounts (early evidence, small samples):

- Give before asking. A free tool, a real number, or a quote is a better first ask than a form. A visitor who receives something comes back.
- Proof above the fold: verified rating and review count, the licence, the years trading. If the price is far below the local norm, missing proof reads as "too cheap to be real".
- Lower the ask. One contact field, and a text or call option for people who will not fill a form on a phone. Only if the owner can actually answer the phone.

Do not publish pages that answer the same question twice. Merge them.

## 5. Budget, caps and the change rail

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 5.1 | Preview a change without writing it. | `adsops_set` (dry_run=true, reason="...") | 10 min | Diff shown, nothing written |
| 5.2 | Diff the committed desired state against live and get a plan id. | `adsops_plan` | 5 min | plan_id returned |
| 5.3 | Validate the identical payload against Google first. | `adsops_apply` (commit=false) | 5 min | Validate only passes with no errors |
| 5.4 | Commit the change, quoting the plan id. | `adsops_apply` (commit=true, plan_id) | 5 min | Live state read back with `adsops_status` |
| 5.5 | Pause if a cap is crossed. This tool can only pause. | `adsops_guard` | 5 min | Campaign paused or a clean no-op |
| 5.6 | See who changed the account outside AdRails. | `adsops_drift` | 10 min | Out-of-band edits listed with timestamps |
| 5.7 | Read the history, and undo to an earlier state if needed. Guards and negatives are never reverted. | `adsops_history`, `adsops_rollback` | 10 min | State file restored, then the normal plan and apply flow |

Rails that are code, not convention:

- Every real change needs a fresh plan id. It is single use, it expires in 15 minutes, and it is refused if the plan, the policy or the live account changed in between.
- Exposure increases (budget up, enable, extending an end date) are refused while the guard heartbeat is stale. Pauses and negatives are never blocked.
- Enabling needs an explicit allow flag in the state file plus a lifetime spend under the cap. Re-enabling is refused while today's spend is at or over the daily pause threshold.
- An ambiguous Google failure is reconciled and reported, never retried blind.
- Guards are the owner's envelope. Agents read them, agents cannot set them. Any change to the envelope is an owner decision, recorded in the state file with a reason.

Set the hard stop honestly. Google can deliver up to twice a day's budget on a given day. A daily pause threshold set at exactly the budget will trip on normal overdelivery and stop a test that was working. Two to three times the daily budget is the sane hard stop, with a separate lifetime cap. `adsops_fleet` repeats the aggregate check using the same two-times assumption.

## 6. Ads and copy checks

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 6.1 | Write the ad copy for one intent, one promise, one price with its scope. | Claude writes | 30 min | Copy exists as text, not in the account yet |
| 6.2 | Validate the draft against Google's limits and the common disapproval causes: lengths, pins, duplicates, caps, punctuation, phone numbers, unsubstantiated superlatives. | `adsops_rsa_validate` | 10 min | Zero errors, warnings read and accepted or fixed |
| 6.3 | Paste the approved draft into the account. | Owner (Ads UI) | 20 min | Ad live and in review |

AdRails has no tool that creates ad groups or ads on Google, by design. Say that plainly instead of implying it happened. What the agent can do is write, check and explain.

Copy rules: pin only what must always show (the exact trading name, the owner qualifier), because pins cut combinations. One promise per ad group. Never state a price without saying what it covers. Never imply an included service that is billed separately.

## 7. Competitors and auction insights

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 7.1 | Export Auction Insights from the Ads UI and import it, replacing the same date range. | `adsops_auction_insights_import` | 10 min | Rows stored locally, level and range recorded |
| 7.2 | Read the weekly intel report. It uses local data only and spends nothing. | `adsops_competitors_report` | 10 min | Overlap, outranking share and top-of-page rate known |
| 7.3 | Split rank from budget before blaming either. | `adsops_diagnostics` | 10 min | The lost impression share is attributed correctly |

Impression share lost to rank is quality and bid. Lost to budget is money. They lead to opposite actions, and confusing them is the most common way a small account gets worse. Auction data tells you who is taking the slot; diagnostics tells you which lever moves it.

Never quote a competitor's price you cannot source. Put the link and the date next to any third-party number, or write "Unverified" as the value.

## 8. Leads, speed to lead, and cost per client

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 8.1 | Put every lead in one book, with a source. Import what exists, add what arrives. | `adsops_leads` (import, add) | 30 min | No lead without a source |
| 8.2 | Check who is still waiting for a first contact, every morning and after lunch. | `adsops_leads_due` | 5 min | Nobody waiting, follow-ups today known |
| 8.3 | Move leads through stages, and close them with a value. | `adsops_leads` (move) | ongoing | Won rows carry a value, lost rows carry a reason |
| 8.4 | Send real outcomes back to the ad platform from the CRM rows. Hashed locally, preview only. | `adsops_leads` (conversions), `adsops_offline_conv_plan` | 20 min | Upload previewed, a redacted count, nothing raw leaves the machine |
| 8.5 | Compare channels on one ruler. | `channel_cac` | 15 min monthly | Cost per lead and per client per channel, small numbers labelled |

Speed beats optimisation. First contact within minutes books far more often than any bid change, and platforms reward responsiveness where they can measure it. If the owner's phone goes to voicemail during ad hours, say so plainly: the phone route is shut, and the landing page must reflect that honestly rather than showing a call button nobody answers.

Ask the owner every week: did anyone call or text you directly? Those leads never reach a CRM and can flip the picture. In the account we followed closest, the one real walk-up lead arrived with no click id at all, while the ad traffic produced nothing.

## 9. Monitors, briefs and reallocation

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 9.1 | Evaluate the policy rules against live metrics. Proposals only, never applied. | `adsops_rules_check` | 10 min | Proposals and a state patch listed for the owner |
| 9.2 | Run monitors: cost per acquisition spike, pacing, zero impressions, disapprovals, budget limited. | `adsops_monitor_check` | 5 min daily | Alerts de-duplicated, delivered if configured |
| 9.3 | One short brief across every campaign. | `adsops_brief` | 2 min daily | Spend, conversions, blended cost per lead, top issues |
| 9.4 | Fleet view with the aggregate cap check, which assumes twice the daily budget. | `adsops_fleet` | 5 min weekly | No cap breach, or a named one |
| 9.5 | Suggest moving budget from weak to budget-limited strong campaigns inside the caps. The total never rises. | `adsops_realloc_suggest` | 10 min monthly | Moves listed as patches for the owner |

## 10. When to stop spending

Stop or cut when the evidence says so. Say it early rather than protecting a campaign.

- The wrong people are clicking. Most spend goes to a query shape the business does not serve. Fix targeting and negatives for two weeks, then pause if it has not moved.
- The page will not convert. After roughly 60 to 75 real clicks with no form start, the page is the problem, not luck. Decide: fix the first screen, or cut the budget to a floor.
- A lead costs more than a client is worth. If cost per lead stays above what the owner can afford across two clean weeks, cut or pause. Show the arithmetic, not an opinion.
- The conversion data is not honest yet. Never recommend scaling on numbers you cannot defend. Fix tracking first; that is a spending decision too.
- One bad day. That is what the guard is for. Do not raise the cap to chase a campaign that just broke it.
- Give the honest odds before you call a losing streak a verdict, then act on the two week trend.

Trust the log over the dashboard. When a dashboard number and server-side lead rows disagree, the lead rows win. Report the disagreement rather than averaging it.

## 11. Cadence

### Week 1
Run the fast path. Baseline written. No bid or budget change in week one unless tracking was found dishonest, in which case tracking is the only project.

### Weekly, about 20 minutes of agent work
1. `adsops_status` 2. `adsops_diagnostics` 3. `adsops_terms` then `adsops_negatives_suggest` 4. `adsops_lp_diagnose` on the worst ad group 5. `adsops_monitor_check` 6. `adsops_leads_due` 7. `adsops_competitors_report`

One variable a week. Do not change bids and copy in the same week, or you will learn nothing from either.

### Monthly
- `adsops_conv_audit`, `adsops_drift`, `channel_cac`, `adsops_realloc_suggest`, `adsops_auction_insights_import`, `adsops_audit_verify`.
- Re-read quality score per keyword. Two weeks is the shortest honest read on a small account.
- Ask the owner for one page only they can publish: a real local price range, a response time, a count from public records. It earns links and answer-engine citations, and it gives the ad account a page worth landing on.

## 12. What to track (one scoreboard, five numbers)

1. Real leads from ads per week: form submits with a click id, plus calls over 60 seconds. Test rows excluded.
2. Cost per real lead, and cost per signed client on the same ruler.
3. Click to form-start rate, and form-start to submit rate, on the main landing page.
4. Cost per click and the top five search terms by spend, this week against last.
5. Share of spend on terms with zero leads in 30 days, and the query shape behind it.

Also keep beside the scoreboard: whether any soft conversion is still primary (yes or no), the guard heartbeat age, and the list of phrases blocked by negatives.

## 13. Myths and mistakes

- "The account says conversions, so we are getting leads." Google-hosted local actions count as conversions. Check which actions count before any number leaves your mouth.
- "Zero leads proves the page fails." Small samples fool people. Give the odds, then look at form starts, not just submits.
- "More budget fixes low volume." If the terms and the page are wrong, more budget buys more of the same.
- "Bid up to win the lost impression share." Rank-lost share is quality and bid. Check the split before touching either.
- "Test submissions are close enough to real ones." They are not. They show up in the same column as results.
- "Widen the match type to find volume." It buys renter, tenant, job-seeker and DIY traffic. Add negatives and write intent-specific ad groups instead.
- "Change everything at once, then you will see what works." You will see nothing.
- "The guard is annoying." The guard is the only reason one bad day cannot eat a month.
- "A new landing page will fix a tracking problem." It cannot. Fix tracking first.
- "Quote the cheapest competitor price you found somewhere." Never without a link and a date, or write "Unverified".
- Guaranteed results, invented savings, review stars on your own business in structured data, and certifications the business does not hold. One challenged claim poisons a page and an ad.
- "One good week means it is working." Hold the claim across two clean reads before telling the owner a channel works.

## 14. Rules for Claude while running this

- Read-only first. The mutating calls are `adsops_apply`, `adsops_set`, `adsops_guard`, `adsops_rollback`, `adsops_approval_request`, `adsops_approval_decide`, `adsops_auction_insights_import` and the mail tools. Validate before you commit anything.
- Never widen a guard, a cap or a policy. If a test needs more room, that is the owner's decision, with a reason written in the state file.
- Do not publish, post or send anything for the owner. Draft it, then wait.
- If a fact is missing, ask the owner. Do not fill in a price, a licence, a service area or a promise.
- Report in plain English: what changed, what is still owed, and the one next step. No jargon, no hype, no em dashes.
