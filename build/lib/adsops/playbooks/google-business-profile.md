# Google Business Profile playbook: get to 100 percent and stay there

For the owner's Claude. The profile is the free listing that drives Maps, the local pack and much of what AI assistants say about a local business. Work it in this order, and pair it with `local-leads.md` for speed to lead and `aeo.md` for AI visibility.

Rules for Claude:

- Claude drafts. The owner or staff posts, uploads and clicks publish. Never say something was published when it was not.
- Never invent a fact: no door counts, years in business, awards, guarantees, response times or fees you were not given. If a field is missing, ask.
- A field that is not true stays blank. A guess in a public listing is worse than an empty slot.
- Tag claims "tested" or "early evidence". Most of what follows comes from small samples.
- `adsops_gbp` scores a profile snapshot to 100 and lists the fixes. Run it before and after every change.

## Fast path: first 7 days

| Day | Do | Tool | Time | Done when |
|---|---|---|---|---|
| 1 | Ask the setup questions, save the profile. | `adsops_local_profile` (action=questions, then set) | 30 min | Trade, city, service area, services, phone, website, job value, close rate saved |
| 1 | Find who owns the listing and add a second owner. | Owner | 15 min | Two people can log in, not one |
| 1 | Fill a snapshot of the live profile and score it. | `adsops_gbp` (action=audit) | 30 min | Score and a ranked fix list in hand |
| 2 | Fix name, phone, website, address or service area, categories. | `adsops_gbp` (action=audit) for gaps, Owner enters | 1 hour | Core fields match the site |
| 2 | Check the description before pasting. | `adsops_gbp` (action=description) | 10 min | No problems returned |
| 3 | Start verification. Touch nothing on name or address while it runs. | Owner | 20 min | Submitted, not edited again |
| 3 | Set hours, then special hours for the next holiday. | Owner | 10 min | All 7 days set, holiday closed |
| 4 | Load services with real descriptions, answer every attribute. | `adsops_gbp` (action=audit) | 1 hour | Answered attributes equals shown attributes |
| 5 | Upload logo, cover and 5 to 10 real job photos, named after the service. | Owner | 45 min | Photo count up, none stock or AI |
| 6 | Make the review link, ask the first customers. | `adsops_reviews` (action=link, then request) | 30 min | Link opens the box, first asks sent |
| 7 | Build the post plan, queue the first two posts, then score again. | `adsops_gbp` (action=posts, then audit) | 30 min | Two posts a week queued, next three fixes written down |

## 1. Verification, and what NOT to edit while Google is processing it

Verification is the gate. Nothing else counts until it clears.

- Check whether the profile is verified before planning anything else: `adsops_gbp` (action=audit). The verified check passes, or it is the top fix.
- Start the flow Google offers. Postcard, phone, video and documents all take time. Owner does this.
- Ask whether the address is a real staffed office or a shared suite. The answer decides section 7.
- Put a nag on the owner's calendar. Done when a reminder fires weekly until verified.

Do not touch these while verification is processing:

- The business name. A name edit can restart re-verification.
- The address. Moving the pin mid-review is the commonest way to lose a week.
- The primary category, on a new profile.
- Creating a second listing for the same business.

Lessons:

- Start verification before you need the leads, not the week the ad budget goes live.
- If a check is rejected, read the named reason and fix that reason. Do not resubmit the same thing.
- Duplicate rows split reviews and confuse Maps. Report one only after you know which row is the live listing with the reviews. Its manage link can resolve to the good profile, so a careless removal can delete the real one.
- Losing access is common. Day one job is a second owner account, so the listing cannot be stranded.

## 2. Categories: one primary, few secondaries, no stuffing

- The primary is the main money service. It drives which searches the profile can appear in. Pick the service that pays the bills, not the widest word. Check it in `adsops_gbp` (action=audit).
- A category that pulls the wrong searcher costs money. Renter categories bring renters. Agent and agency categories bring agents and buyers.
- Add at most 1 to 3 secondaries, each one a service really performed. Claude writes the candidates and the reason for each; Owner enters them.
- `adsops_gbp` flags more than 4 secondaries as stuffing risk. More than 9 is a hard fail. Most small businesses need one.
- Blog posts saying "just add it as a secondary category" are unverified until Google's own requirements page says so. Check the live list, in the owner's country, on the day you do it.
- If the trade has no category at all, do not force one. Use Search and Maps. A wrong category is worse than none.

## 3. Description rules

- Draft from facts the owner gave (Claude writes), then check it with `adsops_gbp` (action=description, business_name=). Done when ok is true and no problems come back. Owner pastes it.
- 750 characters maximum. Aim for 500 to 750 so it reads complete and is never clipped. Under 250 is a warning in its own right.
- No URLs. Google rejects links in this field.
- No phone numbers. The phone has its own field and a number in prose gets flagged.
- No superlatives: best, number one, cheapest, top rated. `adsops_gbp` lists them as problems because Google may reject them.
- No ALL CAPS runs. Do not start by repeating the business name twice.
- Write what the business does, where, for whom, and what a customer gets. Plain sentences.
- If a price is stated it must be the exact price the owner gave, and anything outside it must be named in the same breath. The commonest error in the files behind this playbook was a description that quietly folded an extra service into the headline price.

## 4. Services with real descriptions

- List every service actually sold, one per line, from the owner's answers. Each must map to something real.
- Give each a description of 50 characters or more in plain language. Claude writes them. Five or more must pass the length check in `adsops_gbp` (action=audit).
- Each description stands alone. A reader who lands on one understands it without the others.
- State what is included and what is a separate charge. Never let one service imply another is free.
- Fifteen real services beat five vague ones, but only if each is something done this month. Do not add one to look bigger. The tool cannot check truth. The owner can.
- Where a service has a live page on the site, keep the wording consistent. Two stories on two Google surfaces is a trust problem.

## 5. Attributes: answer every one

- Open the live attribute list for the chosen category and country on the profile itself. Both change the list. Owner.
- Answer every line yes or no, and leave it blank only when it is unknown. Done when answered divided by total is 0.9 or better in `adsops_gbp` (action=audit).
- A false yes is a complaint waiting. Blank is neutral and honest.
- Optional self-descriptions, such as women-owned or veteran-owned, are the owner's call. Claude does not volunteer them.
- Accessibility answers only apply when the address is shown and someone has walked the building. On a hidden-address profile they do not apply.
- Payment answers need the owner to confirm which methods work, not which exist. Google sets some highlights itself, so do not chase those.

## 6. Hours and special hours

- Ask when a person actually answers the listed number. Owner sets all 7 days, including the closed ones. Done when the hours check passes.
- List only hours when a human answers. A day marked closed hurts ranking less than a listing open when nobody picks up.
- Never list 24 hours unless a person or a paid answering service really covers 24 hours.
- An emergency line for existing customers is not open hours.
- Set special hours before every holiday, a week ahead. Doing it the morning of is the usual failure. Done when special_hours_set is true.

## 7. Service-area businesses: hide the street address, list the areas

- Decide first: does the business serve customers at its address, or travel to them? Claude asks, Owner answers.
- If it travels, hide the street address and list the service areas. Owner does both. Done when address_hidden is true and service_area is not empty.
- Keep the list to 20 places or fewer, all really served. `adsops_gbp` (action=audit) warns past 20.
- A shared office or coworking address can only be shown if it is staffed, with signage, during the posted hours. Otherwise hide it. No virtual address as a workaround, and never show an address that is not a real office.
- The verification address is not the displayed address. Verify at a real address, still hide it.
- List areas in the words customers use. For parts of two counties, name the cities, not just the counties.
- A hidden address with an empty area list is the worst of both worlds. Fix both fields in one sitting.

## 8. Photos and video: real job photos, no stock and no AI

- Set a logo and a cover that match the brand used in ads. Owner. Done when has_logo and has_cover are true.
- Upload 10 or more real photos: finished jobs, the team, the van, the tools. Owner. Photos check passes.
- Upload one video shot on a phone, landscape, 20 to 45 seconds. Owner. Video check passes.
- Name every file after the service and the place before uploading. Claude writes the names, Owner renames.
- No stock and nothing AI-generated. The files behind this playbook contain AI brand images made for a serious business that looked wrong on first look. If the picture is not the real business, it does not go on the profile.
- No customer names, no faces without written permission, no house numbers unless the owner agrees in writing.
- One new real photo every month. An old photo set reads as a stale business. Ten photos is the floor, not the goal.

## 9. Posts: 2 a week, rotating services, a call to action, tagged links

- Build the plan with `adsops_gbp` (action=posts, services=, weeks=, per_week=2, cta_url=). Every post gets a week and a day.
- Draft each post from the plan, 80 to 150 words. Claude writes. One offer, one call to action, one link.
- Tag every link: Claude writes the UTM. Each link carries source, medium and campaign.
- Post Tuesday and Friday. Owner. Done when posts_last_30d is 4 or more.
- Rotate services. The same offer four weeks running reads like a dead account.
- Every post links to its own service page, never the homepage, so the traffic is measurable at all.
- Use only the call to action buttons the platform offers on screen. If the one you wanted is absent, pick from what is there.
- One offer per post. Two offers in 120 words means nobody acts. One owner look per batch of four is enough.

## 10. Q&A seeded from real customer questions

- Collect the questions customers really asked, from calls, texts and the lead book: `adsops_leads` (action=find, text=). Target 5 or more written down.
- Draft each answer, answer first then detail. Claude writes. Answers stay short and factual.
- Owner posts them. Done when qa_pairs is 5 or more.
- Seed only questions someone really asked. Inventing friendly questions is the same class of error as inventing a review.
- Anyone can answer Q&A on the profile, including competitors. Check monthly and answer the real ones yourself.
- Money answers must match the description and the services exactly. One price told three ways is a trust leak.

## 11. Reviews: ask everyone, no gating, no incentives, reply to all

- Make the review link from the Place ID: `adsops_reviews` (action=link). Done when the link opens the review box.
- Draft the ask, text or email: `adsops_reviews` (action=request). Owner approves the wording and sends it. AdRails never sends.
- Log the ask so it can be counted: `adsops_leads` (action=note). Asks logged per week.
- Draft a reply to every review, good and bad: `adsops_reviews` (action=reply). Owner posts.
- Check pace and gap to competitors: `adsops_reviews` (action=velocity). Reviews needed for the next rating known.
- Ask every customer. Do not screen for happy ones, do not offer rewards, do not ask staff or family, do not stuff keywords into the ask.
- Tie the ask to an event: job finished, keys handed over, first payment, a thank-you call. One ask, one follow-up at five days, then stop.
- Reply within 24 to 48 hours, bad ones first. Neutral wording.
- Never write "5 stars" in a reply or an ask, and never write a review for anyone. Asking for a rating breaks the rules. Asking for honest feedback does not.
- A reply to a complaint does not argue and does not deny a fact you cannot check. Thank them, state the fact, take it offline with a named human and a real contact route.
- If a review has no text, the reply says nothing specific. A warm reply that invents an experience is fabrication.
- Do not draw attention to reviews from people connected to the business. Silence is quieter than a public reply under a review from your own side.
- Spread the asks. A burst on one day looks like a campaign.

## 12. Website and appointment buttons, tested

- Point the website button at a page with a working form. Owner, checked by Claude. Done when the page loads and the form works.
- Tag the URL so the click survives to the lead record. Claude writes the UTM. Done when a test lead arrives tagged with the profile as source.
- Point the appointment button at the booking page, tagged separately. Owner, checked by Claude. Done when a test booking reaches the owner.
- Check the tag survives the page's own scripts: `adsops_local_nap` (urls=) plus a real test click. Done when the lead record shows the profile tag, not a generic site tag.
- The commonest failure in the files behind this playbook: a page rewrote the incoming tag with its own, so every profile click counted as generic site traffic. The button worked, so the test looked fine. What broke was the measurement.
- If the tagged page converts worse over a fixed window, change the target. Decide on the window's data, not a hunch.

## 13. Listings consistency

- Fix one canonical name, address, phone and website in writing. Claude writes it, Owner approves.
- Compare every listing against it: `adsops_local_nap` (urls= or pages=). Done when no mismatches are left.
- Work the queue: `adsops_local_nap` (action=citations). Each listing claimed or fixed.
- Claim the second-tier engines after the main profile is verified. They can import from it. Owner.
- One spelling of the name everywhere. Two spellings split the entity and weaken both.
- One phone number everywhere, and it must be one a person answers. A published number ringing a dead line was the biggest single leak found in the files behind this playbook. Call every number a customer can see, from a phone that is not the owner's.
- One website form everywhere. Same domain, no shortener. Non-www and www are not the same citation, so pick one and hold it.
- Carry the same facts into the site's structured data: `adsops_local_schema`, then paste into the page head.

## 14. Weekly and monthly cadence

Weekly, 30 minutes: `adsops_gbp` (action=audit) on a fresh snapshot and fix the top item; two posts from `adsops_gbp` (action=posts); reply to every new review with `adsops_reviews` (action=reply); count asks and new reviews with `adsops_reviews` (action=velocity); answer any new Q&A.

Monthly, 1 hour: re-check the listings with `adsops_local_nap`; run `adsops_gbp` (action=audit) again plus `adsops_gbp` (action=description) after any wording change; upload one new real photo; compare against competitors with `adsops_reviews` (action=velocity); see what is overdue with `adsops_local_plan` (action=progress); set special hours for the month's holidays.

## 15. The scoreboard: 5 numbers

1. Profile score from `adsops_gbp` (action=audit), and the three fixes behind it.
2. Reviews: new per month and average rating.
3. Review reply rate, and the age of the oldest unanswered review.
4. Profile actions that became real leads, counted by source in the lead book, not in the dashboard.
5. Calls to the listed number that were answered, and missed calls.

Keep dashboard numbers secondary. Directions and views inflate lead counts. Real leads live in the lead book.

## 16. Mistakes to avoid

- Chasing traffic and posts while the phone rings out.
- Editing the name or address while verification is processing.
- Creating a second listing to start clean, or leaving one person with the only login.
- Keyword stuffing the business name, or buying another name to do it. Profiles get suspended for that.
- Showing a shared office address that is not staffed or signed, or using a virtual address for a service-area business.
- A description with a URL, a phone number, or the best, cheapest, number one.
- Folding one paid service into the headline price.
- Gating reviews, rewarding reviews, writing one, asking for a specific star rating, or stuffing the ask with keywords.
- Replying to a complaint with a defence, or inventing a detail to sound warm.
- Stock photos and AI images on a real business profile.
- A published phone number nobody answers, or counting views and directions as leads.
- A post plan with no call to action, or a link with no tag.
