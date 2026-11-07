# Local leads playbook: get the call, answer it fast, win the job

For the owner's Claude. This covers paid and free local lead sources, and what happens after a lead arrives. Pair it with `aeo.md` for AI visibility.

Rules for Claude:

- AdRails drafts messages. It never sends texts, emails or review requests. The owner or staff sends.
- Google Local Services Ads budgets live in Google's dashboard. Search campaigns change only through the committed plan flow (`adsops_plan` then `adsops_apply`).
- Never invent a number. If a profile field is missing, ask.
- Tag claims "tested" or "early evidence". Most of what follows is from small samples.

---

## The one idea

At small volume, conversion beats traffic. Our AI-referred visitors were few and hot: one filled in the form 2 minutes after landing. A missed call or a next-morning reply loses a lead that cost real money. Fix speed and follow-up before you buy more traffic.

---

## Step 0: set up the profile and the money math (day 1, 30 min)

| What | Tool | Done when |
|---|---|---|
| Ask the owner the setup questions. | `adsops_local_profile` (action=questions) | Questions answered |
| Save trade, city, service area, services, phone, website, Google Business Profile status, reviews, LSA eligibility, budget, average job value, close rate, goals, competitors. | `adsops_local_profile` (action=set) | Tool returns the max cost per lead the business can afford |
| Build the 90-day plan from the profile. | `adsops_local_plan` (action=build) | Plan exists; every task names its tool |
| Show the owner the checklist. | `adsops_local_plan` (action=markdown) | Owner has read it |

Max affordable cost per lead = average job value x close rate x the margin you can give away. Write it down. Every channel is judged against it.

---

## Step 1: speed to lead

Early evidence and tool guidance: leads answered within 5 minutes book far more often, and Google ranks LSA advertisers that answer and reply fast higher.

| What | Tool | When | Done when |
|---|---|---|---|
| Check new leads waiting for a first contact, overdue follow-ups, follow-ups due today. | `adsops_leads_due` | Every morning and after lunch, plus whenever a lead alert arrives | Zero leads waiting past 5 minutes during business hours |
| Add every lead from every channel to one book. | `adsops_leads` (action=add) | On arrival | Lead has a source and a phone or email |
| Log the first contact. | `adsops_leads` (action=move, stage=contacted) | Right after | Median first-contact minutes shown in pipeline |

Hard-won lessons:

- Find the leak first. The biggest leak we found was calls to the published number going to a dead or unanswered line. Test every number a customer can see (site, profile, ads, listings) by calling it from a phone that is not the owner's. Done when: each number rings a person or a forwarding line that works.
- Add a dedicated after-hours path. Evenings are when homeowners search. A lead that arrived at 10pm waited until the morning. Options: a confirmation text plus booking link, or a drafted personal follow-up queued for the first slot. Whether to auto-text is the owner's call. In our case the owner decided to keep the personal morning follow-up and not auto-text. That is a valid choice, but then measure first-reply time for 4 weeks and decide with data.
- Before any automatic text: confirm consent rules (people agreed to texts), carrier registration, and a clear opt-out. Human-sent first texts have fewer legal limits. Ask the owner or their lawyer.
- Put price logic, the person's name and one tap to call or text on the first mobile screen of the landing page. Keep the form to 3 fields.
- Name the follow-up owner. If two people both think the other will call, nobody does.

---

## Step 2: Local Services Ads (LSA)

LSA is Google's pay-per-lead ad at the top of local results. Check eligibility before you plan on it.

| What | Tool | Time | Done when |
|---|---|---|---|
| Check verification status for license, insurance, background check, business registration, plus profile signals (reviews, rating, response time, photos, hours, messaging, bio). | `adsops_lsa_readiness` | 10 min | Each requirement shows verified, pending, or the reason it failed |
| Pull leads with call and message history, who answered, how fast, junk flags. Syncs into the lead book. | `adsops_lsa_leads` | 10 min weekly | Leads are in the book |
| Report spend, cost per charged lead against the max affordable, credits, booking rate, missed calls, leads by hour and weekday. | `adsops_lsa_report` | 10 min weekly | Cost per charged lead known against the ceiling |
| Get credit for junk leads. First suggest ratings, then submit. | `adsops_lsa_dispute` (no decisions to suggest; commit=true to submit) | 15 min weekly | Credit decisions returned; each lead rated once, inside the window |

Lessons:

- Some trades have no LSA category at all. We checked one service business in one state: Google listed 94 categories and none fit, the nearest one needed a real estate agent license for each person. Our pre-check took hours of reading. Do it first. If there is no category, do not apply. Use standard Google Ads and Maps. Treat blog posts that say "just add it as a secondary category" as unverified until Google's own requirements page says so.
- Verification takes time (license, insurance, background check). Start it before you need the leads.
- Missed calls hurt LSA ranking. Answer or reply fast (Step 1).
- Dispute junk every week: spam, solicitation, duplicate, wrong area, wrong job. Rate booked jobs "very satisfied" so Google learns what you want.
- If the readiness report shows a rejected check, fix the named reason, then resubmit. Do not reapply blindly.
- Work with Google's live API, not guesses: our first live LSA query failed on a field Google rejected, and a contract check now pins fields to Google's own list. Early evidence that "it passed mocks" is not enough.

---

## Step 3: Google Business Profile (GBP)

GBP drives Maps, the local pack and a lot of what AI assistants pull for local businesses.

| What | Tool | Time | Done when |
|---|---|---|---|
| Claim or request ownership. Add a manager (a staff member) so more than one person can post. | Owner | 30 min | Profile owned; two people can log in |
| Verify. Follow Google's flow. | Owner | varies | Verified |
| Fill in everything: primary and secondary categories, services, description (use your 40-word standard text), hours, photos of real work, Q&A, and a post. | Claude writes, owner enters | 1 to 2 hours | Profile shows 100 percent filled |
| Check name, address and phone match every other listing. | `adsops_local_nap` | 30 min | No mismatches left |
| Add Bing Places and Apple Business Connect after GBP is claimed. | Owner | 1 hour | Both claimed with the same facts |
| Generate LocalBusiness schema for the website. | `adsops_local_schema` | 15 min | Pasted in head |

Lessons:

- Service-area business: hide the address and show the service area. A coworking or shared-office address may make you ineligible if there is no real staffing or reception. Do not use a virtual-address workaround.
- Listings can lag. A profile claim window, ownership dispute or phone verification can take days. Put a nag on the owner's calendar. In our case a reminder job kept chasing until the claim was done.
- "Local actions" conversions from GBP (directions, website visits) inflate lead counts. Keep them secondary. Keep real leads primary (see Step 6).
- Reply to every review. Reply to legal or safety complaints personally, not with a template.

---

## Step 4: reviews

Reviews drive LSA ranking, Maps ranking and trust.

| What | Tool | Time | Done when |
|---|---|---|---|
| Make the Google review link from the Place ID. | `adsops_reviews` (action=link) | 5 min | Link opens the review box |
| Draft request texts or emails for a customer. | `adsops_reviews` (action=request) | 5 min each | Owner approves the wording |
| Draft replies. | `adsops_reviews` (action=reply) | 5 min each | Owner posts reply |
| Check pace and gap to competitors. | `adsops_reviews` (action=velocity) | 10 min monthly | Reviews needed to reach 4.5 and 4.8 known |

Policy built in and worth repeating:

- Ask every customer. Do not screen for happy ones, do not offer rewards, do not ask staff, do not stuff keywords.
- Tie the ask to an event: job finished, lease signed, first payment, thank-you call.
- Early evidence: after a one-week count, the helper who could have asked had asked zero customers. A scheduled daily prompt with the ready-to-send text fixed it better than a speech.
- Count asks per week and new reviews per month. Done when: asks are logged and at least 2 new reviews arrive per month.

---

## Step 5: the lead book and follow-up cadence

One book for every channel.

| What | Tool | Done when |
|---|---|---|
| Add leads from any channel; dedupe by source id, phone or email. | `adsops_leads` (action=add) | No duplicates |
| Import old leads from a spreadsheet. | `adsops_leads` (action=import) | Rows imported with a column mapping |
| Move through stages: new, contacted, quoted, booked, won, lost, spam. Won needs a value. Lost needs a reason. | `adsops_leads` (action=move) | Every lead has a stage |
| Add a note with a follow-up date (a date or +3d). | `adsops_leads` (action=note) | Follow-up appears in `adsops_leads_due` |
| See counts, win rate and value by source, plus median minutes to first contact. | `adsops_leads` (action=pipeline) | Reviewed weekly |
| Export. | `adsops_leads` (action=export) | CSV saved |

### A follow-up cadence that does not annoy people

We got this wrong first. A new lead received an acknowledgment, a welcome email, two personal emails and a drip email all inside three days. Fix:

- Steps at about day 0, 3, 7, 14, 28 and 45.
- At least 72 hours after any personal message or previous drip.
- Skip the welcome if the form already sent an acknowledgment, or a person already wrote.
- Any reply from the lead pauses the automation. Then a person takes over.
- For long-sales-cycle leads (owners deciding months out), keep a slow, helpful drip until they sign or ask to stop.
- No texts once a person has spoken to them.
- Always honor opt-out.
- Verify the pause works: check one lead who replied before the first drip and confirm nothing was sent.

Done when: pipeline shows no lead sitting in "new" for over a day, and no lead got more than one message in 72 hours.

---

## Step 6: close the loop with Google Ads

| What | Tool | Done when |
|---|---|---|
| Audit conversion tracking: primary goals, dead tags, double counting. | `adsops_conv_audit` | Findings fixed |
| Set only real leads as primary: form leads, calls over about 60 seconds, click-to-call. Demote views, directions and other soft actions to secondary. | Owner in Google Ads (or via the guarded plan flow) | Primary list matches real leads |
| Turn won jobs into offline conversions so Google learns which clicks became customers. | `adsops_leads` (action=conversions), then `adsops_offline_conv_plan` | Preview built; hashed locally; validate-only |
| Check wasted spend and shortfalls. | `adsops_diagnostics` | Top waste named |
| Find junk searches and add negatives (renter, job, DIY, wrong service). | `adsops_negatives_suggest`, then `adsops_set` | Negatives added |
| Check landing page echoes the query and town. | `adsops_lp_diagnose` | Findings fixed |
| Cost per lead and per customer per channel, one ruler. | `channel_cac` | Monthly table |

Lessons:

- Bid cap while you cannot trust conversion data. A per-click ceiling stopped a bad day from spending the budget on low-quality clicks. Early evidence, not a rule.
- Small numbers: `channel_cac` labels small samples "too few to judge". Believe it.
- Never publish customer details. Offline conversion rows are hashed on your machine, and the tool never sends them itself.

---

## Step 7: weekly and monthly rhythm

### Every morning and after lunch (5 minutes)
`adsops_leads_due`, then act on every row.

### Weekly (30 minutes)
1. `adsops_lsa_leads` and `adsops_lsa_report` if LSA is on.
2. `adsops_lsa_dispute` for junk.
3. `adsops_leads` (action=pipeline) by source.
4. `adsops_reviews` count of asks this week.
5. `adsops_monitor_check` and `adsops_brief` for spend and CPA alarms.

### Monthly (1 hour)
1. `channel_cac`.
2. `adsops_reviews` (action=velocity).
3. `adsops_local_nap` re-check.
4. `adsops_local_plan` (action=progress), mark done, see what is overdue.
5. `adsops_leads` (action=conversions) then upload preview.

### What to track (5 numbers)
1. Median minutes to first contact.
2. Leads by source, and win rate by source.
3. Cost per lead and per customer per channel.
4. Reviews: new per month, average rating.
5. Missed calls and leads waiting over a day.

---

## Mistakes to avoid

- Buying traffic before answering the phone.
- Counting soft conversions as leads.
- Auto-texting without consent or registration checked.
- Applying to LSA in a category that does not fit your work.
- Using a virtual address for a service-area profile.
- Gating or rewarding reviews.
- Too many messages in the first three days.
- Quoting a lead count you have not reconciled against the lead book.
