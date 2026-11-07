# AEO playbook: get recommended by ChatGPT, Perplexity, Gemini, Google AI Overviews and Claude

For the owner's Claude. Read all of it, then run it step by step with the AdRails tools named in each step.

AEO means answer engine optimization. People now ask an AI "who should I hire?" or "what does this cost?" and get one short answer with a few names. This playbook is how a small business gets named in that answer.

Ground rules before you start:

- No one can guarantee a citation. No crawler score predicts an AI answer. We measure what we can: access, page structure, and a repeatable panel of questions.
- Tag every claim you make to the owner as "tested" or "early evidence". Most AEO advice online is untested. Ours came from a few weeks of work on a handful of sites. Where the sample is small, this file says so.
- Facts on the site must be true and sourced. Engines repeat what pages say, including mistakes.
- AdRails makes no AI calls itself. YOU ask the engines the questions and pass the answers in.

---

## Fast path: a brand-new business with almost no web presence (first 7 days)

Goal: exist as one clear entity, be crawlable, and have a handful of pages that answer real questions. Do not try to do everything.

| Day | Do | Tool | Time | Done when |
|---|---|---|---|---|
| 1 | Save the business profile: trade, city, service area, services, phone, website, goals, competitors. | `adsops_local_profile` (action=questions, then action=set) | 20 min | Profile has no "missing" fields that matter |
| 1 | Freeze the facts: exact business name, address (or service area only), phone, hours, license number, founding year, one 40-word description. Write them in one place. Use them word for word everywhere. | Owner decides, Claude writes the sheet | 20 min | One facts sheet the owner approved |
| 1 | Check the name is not shared with another business in a different city or industry. If it is, always write the name with the city ("Name, [city] [state]"). | Owner / Claude searches | 15 min | Written note: collision found or not |
| 2 | Check the site can be reached by AI crawlers. | `aeo_site_audit` | 10 min | Score above 80 and no named crawler blocked |
| 2 | Fix robots.txt: one explicit Allow per crawler you want. Add a sitemap line. | Claude writes it, owner or web host applies it | 20 min | `aeo_site_audit` rerun shows the fixes |
| 2 | Verify the site in Google Search Console and Bing Webmaster Tools. Submit the sitemap to both. | Owner (login needed) | 30 min | Both show the sitemap as read |
| 3 | Create or claim the Google Business Profile. Start verification. | Owner; see `local-leads.md` | 30 min | Profile claimed, verification started |
| 3 | Generate LocalBusiness schema from the profile. Paste in the site head. | `adsops_local_schema` | 15 min | Schema validates; it matches visible page text |
| 4 | Write the homepage first screen: one sentence saying who you are, where, what it costs or how pricing works, and one way to contact you. | Claude writes | 45 min | `aeo_page_audit` on the homepage shows an answer-first block |
| 4 | Write a 10 to 15 question FAQ from real customer questions. Short, numeric answers. | Claude writes, owner checks facts | 60 min | FAQ is on the site as visible text |
| 5 | Add FAQPage schema that matches the visible FAQ exactly. | `aeo_schema_generate` (kind=faq) | 10 min | `aeo_page_audit` shows no schema mismatch |
| 5 | Build 2 service x area pages for the most valuable service and the nearest areas. | `adsops_local_area_page`, then Claude writes | 2 hours | Both live, each with a unique local insight |
| 6 | Publish `/llms.txt` and tell Bing about the new URLs. | `aeo_llms_txt`, `aeo_indexnow` (submit=true) | 20 min | llms.txt validates; IndexNow returns 200 or 202 |
| 7 | Record the BASELINE panel: 8 to 12 prompts across at least 3 engines. Expect zero mentions. That is normal. | `aeo_visibility_record` | 60 min | A stored run with the date. Do not skip it, you need the zero row. |

After day 7, the 30 and 90 day sections below apply. A new business will not show up in AI answers in week one. The first goal is to be indexed and consistent. Expect weeks, not days.

---

## 1. Measure first: the AI visibility panel

What we learned: our first panel showed 1 of 40 unbranded answers named the product, even though every engine described it correctly when asked by name. Asking by name proves the engine can find you. It does not prove it will recommend you.

### How to build the prompt set

- 8 to 15 prompts total. A set that changes every week measures noise. Lock it.
- Write most of them as the customer would, in the first person, with a place and a situation. Example shape: "I own a [thing] in [city] and [situation]. Who should I call and what will it cost?"
- Keep 3 or 4 plain list questions as controls ("best [service] in [city]").
- Why: in our best evidence, the real AI-referred leads asked situation-shaped questions, not list questions. List-shaped prompts get answered from "best of" articles the business is not on, so they under-report real visibility. Tracking only list prompts made us look invisible while the AI was sending leads. This is early evidence from one business, but it matched across two leads.
- Do not count prompts that contain your brand name as wins. Those always pass. Only unbranded rows measure recommendation.

### How to run it

1. Ask each engine yourself. Use the consumer interface with search on when you can. Use a clean session: no memory, no project files that mention the business. Why: our own agent read local project notes, "knew" the product, and polluted the first run. Those rows were thrown away.
2. Use at least three engines. Fewer is fine for week one. Record which engine and version.
3. Pass all answers in one call: `aeo_visibility_record` with answers [{engine, prompt, text, citations}], brand, and a competitors list. It scores mentions, rank against competitors, citations and sentiment, and shows the change from the last run.
4. Read the citations, not just the mentions. The list of URLs engines cite is your real search results page. Note which hosts keep appearing.
5. Look for wrong-but-mentioned rows. An engine that names you with a wrong price or wrong service area points at a page feeding it a mistake. Fix that page first.

Time: 60 minutes for the first run, 30 minutes weekly after. Done when: a stored run exists with the same prompts as last week.

### What the citations told us (early evidence, local services and software)

- Engines cited a small set of hosts again and again: Reddit threads, GitHub repos, directories, and a few "best of" articles. For local services, one competitor's pricing page and town pages were quoted for cost and area questions.
- One industry directory we expected to matter was cited in 0 of 71 answers, and a professional-association listing in 2 of 71 (both quoting a competitor's own page copy). Check your own panel before paying for any listing.
- Reddit was cited 26 times in the same sample. That is a strong hint, but posting there is the owner's call. See section 6.

---

## 2. Make sure you can be reached

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 2.1 | Audit crawler access for 18 AI and search crawlers, sitemap, canonical, noindex, and whether body text is in the HTML. | `aeo_site_audit` | 10 min | Score and ranked fixes returned |
| 2.2 | Fix named failures. Give each crawler you want its own Allow rule. | Claude writes, owner applies | 30 min | Rerun shows the score rising |
| 2.3 | Make sure the main text is in the HTML, not injected by script. | Claude checks with the audit | 30 min | Audit confirms body text present |
| 2.4 | Add Bing. Verify and submit the sitemap in Bing Webmaster Tools too. | Owner | 30 min | Bing shows more than the homepage indexed |

Why each matters:

- A blocked crawler cannot cite you. A blanket rule written for one bot often blocks another.
- Bing matters more than people think. ChatGPT search leans on Bing results (early evidence). In one audit, Bing held only 7 of 40 URLs for a site Google had mostly indexed. Both AI-referred leads landed on the one page Bing had. Find out why each missing URL was excluded: canonicals, crawl errors, thin internal links. Resubmitting alone does not fix those.
- IndexNow reaches Bing and a few others. It does not reach Google. Resubmit the sitemap in Search Console for Google.
- A page can be fetched by an AI crawler and never cited. That is a page-quality problem, not a crawl problem. See section 3.
- Do not block AI crawlers on a marketing site unless there is a real reason. Block only private app areas, at the server, with noindex.

---

## 3. Build the pages engines can quote

Engines lift one self-contained chunk from a page. Write pages in chunks.

### The page recipe

1. First 45 to 70 words: a plain answer that stands alone if lifted out. Say who, what, where, and the price or how pricing works. No marketing words.
2. H2 headings written as the question a person would ask. The answer follows in the first sentence.
3. At least one real number per section: a price, a time, a limit, a date. Vague copy is not quotable.
4. A real HTML table for any comparison or price list. Engines extract tables.
5. A visible "last updated" date and a named, licensed author or owner. Why: dated, attributed pages get re-read, undated ones rot.
6. Short paragraphs. Lists where they fit.
7. Say who the page is NOT for. Pages that concede real weaknesses read as honest and get quoted more (early evidence from the comparison-page work, not tested head to head).

Check each page with `aeo_page_audit` (give it the brand and the questions you want answered). It returns scores, the answer blocks it found, and the questions the page never answers. Those missing questions are your next headings. Time: 15 minutes per page to audit, 1 to 2 hours to write. Done when: the audit finds an answer-first block and no missing top questions.

### Which pages to build, in order

1. **Homepage.** Treat the first screen as the landing page for AI visitors. In our case, AI-referred visitors landed on the homepage, not a deep guide. So it must show who you are, the price logic, the person's name and one contact path on the first mobile screen. Early evidence, two visits.
2. **FAQ page.** 15 to 20 real questions, 40 to 60 word answers, each opening with a direct answer. Give each answer an id so it can be linked.
3. **Pricing or cost page**, with a real table and a date. If a competitor's pricing page is the one engines quote for cost questions, study its format, then do it better and with sources.
4. **Situation guides.** One page per real customer situation, not per keyword. Examples of the shape: "I'm moving and keeping my old home", "I live out of state and own here". Each: definition lede, cited law or official source, dated, a plain "not legal or tax advice" line where it applies. This matched the query shape that produced leads.
5. **Service x area pages**, but few and good. See below.
6. **A page only you can publish.** One original, dated, reproducible number from your own data (a local price range, response times, a count from public records). Engines cite numbers they cannot find elsewhere. Early evidence, it was planned and not yet proven to be cited.

### Service x area pages: the rule

- Use `adsops_local_area_page` for ONE service and ONE area at a time. It returns title, meta, H1, outline, the questions people ask AI there, and what to avoid.
- Start with 3 or 4 areas, the highest-value ones. Then measure indexing, impressions and inquiries before building more. A second opinion (a different model) pushed us to cap the first batch for this reason.
- Each page needs a local fact a competitor's template page lacks: local rules, local costs, local building or neighborhood quirks, dated sources. If you cannot write one, do not publish the page.
- Do not mass-produce thin pages. After a first wave of about 18 town pages, our own plan said "stop adding town pages" until the logs showed demand. Thin pages dilute the site and give engines nothing new to quote.
- If you are a service-area business, keep the home address off every page and listing. Show the service area.
- Do not publish pages that are near-duplicates in intent. If two guides answer the same question, merge them.

### What did not matter much (so do not spend time here)

- Markdown copies of pages and a long llms.txt. Both are cheap, and we built them. A second opinion called them "finished, stop spending time on them". Do them once, do not iterate.
- A press release on a paid newswire. Wire links are syndication copies, which engines treat as one self-claim. We held ours. Earned coverage from a real story is different.
- A calculator that the owner thinks "won't look good in this market". Check the owner's taste before building interactive tools.

---

## 4. Schema and llms.txt

### Schema

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 4.1 | LocalBusiness (right type for the trade, areas, hours, license, profile links) on the home page. | `adsops_local_schema` | 15 min | Schema generated with no warnings |
| 4.2 | Organization with logo and same_as links to your real profiles. | `aeo_schema_generate` (kind=org) | 10 min | Audit passes |
| 4.3 | FAQPage that matches the visible FAQ exactly. | `aeo_schema_generate` (kind=faq) | 10 min | Every question appears in the page body |
| 4.4 | BreadcrumbList on pages below the root. | `aeo_schema_generate` (kind=breadcrumbs) | 10 min | Audit passes |
| 4.5 | Software products only: SoftwareApplication. | `aeo_schema_generate` (kind=software) | 10 min | Audit passes |

Rules that came from real defects:

- Schema must match visible text. Schema with content not on the page is a mismatch, not a boost.
- Give a person or business ONE stable @id and reuse it everywhere (author, publisher, founder). Several partial copies of the same entity confuse resolution.
- Leave a field out rather than guess it. A wrong postal code or phone in structured data is very hard to correct.
- Never add self-serving review stars (aggregateRating on your own business). It risks penalties and `adsops_local_schema` refuses to emit them.
- An Offer with no valid price fails Google validation. Omit it or give a real price.
- Do not claim certifications you do not hold, in prose or schema.

### llms.txt

- Build it with `aeo_llms_txt` from your best pages, or validate an existing one. 20 minutes.
- Be honest with the owner: it is a young convention. One search-industry voice has said no major AI uses it, and we have no proof any engine does. It costs a static file. Treat it as a signal you control, not a switch.
- Put value in the file: a short "facts you can quote" block (prices, service area, license, hours), a "what we do not do" block, and links to pages that already pass `aeo_page_audit`. An index of weak pages does nothing.
- For a product that agents use, an ordered "if you are an AI agent and need X, follow these steps" block near the top copied a rival's strongest asset. Early evidence, not measured.
- Done when: `aeo_llms_txt` validates and every linked URL returns 200.

---

## 5. Entity: make "the business" resolve to one thing

Engines resolve entities, not keywords. Inconsistency is the failure mode.

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 5.1 | Check name, address and phone across every listing. List every mismatch. | `adsops_local_nap` (urls) | 30 min | One ledger of mismatches with a fix status each |
| 5.2 | Get the prioritized listing checklist. | `adsops_local_nap` (action=citations) | 10 min | A ranked list of listing sites |
| 5.3 | Claim and fill Google Business Profile, Bing Places, Apple Business Connect. | Owner (logins) | 2 hours | Each profile claimed with identical facts |
| 5.4 | Fix wrong addresses on directories. Before asking for a correction, confirm who else uses the address (shared offices do exist). | Owner + Claude drafts | 1 hour | Ledger shows each fixed or submitted with a date |
| 5.5 | Add same_as links from the site to every real profile. | `aeo_schema_generate` (kind=org) | 10 min | Links present |

Why:

- Bing Places and Apple Business Connect feed Copilot, Siri and ChatGPT-style lookups (tool guidance; not tested by us).
- A same-name business in another city, or another business at your address, splits the entity. Use "Name, [city] [state]" consistently to separate yourself.
- Same 40-word description everywhere: site, listings, social, code hosting.
- Shared-office addresses can make a Google profile ineligible. If you cannot show real staffing and reception at the address, use a service-area setup honestly. Do not use a virtual-address workaround.
- Do not buy backlinks, buy a Wikipedia page, or paste one description into 40 directories. Wikidata only after independent sources exist.

### Reviews and third-party mentions

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 5.6 | Build the review link and request drafts. Ask every customer. | `adsops_reviews` (action=link, then request) | 15 min | Link works; template approved by owner |
| 5.7 | Draft replies, flagging legal or safety reviews for the owner. | `adsops_reviews` (action=reply) | 10 min each | Reply posted by owner |
| 5.8 | Track pace and the gap to competitors. | `adsops_reviews` (action=velocity) | 10 min monthly | Number of reviews needed to reach 4.5 and 4.8 known |

- Policy: ask every customer, no gating by score, no incentives, never ask staff. Why: the FTC rule on fake and incentivized reviews applies, and filtered reviews look filtered.
- In our case the owner's helper asked zero customers in a week. The fix was a one-line prompt in a daily message, not a new tool. Make asking a habit tied to an event (lease signed, job done, first payment).
- AdRails drafts requests and replies. It never sends them. The owner or staff sends.
- Third-party mentions: engines quote Reddit threads, GitHub repos, local directories and "best of" lists. Find which of those your panel citations show. Then be present only where the panel proves it matters. Posting on forums is outbound comms: the owner decides, discloses who they are, and never posts as a company account. Early evidence only.
- Earned press beats paid wires: a data story from your own numbers (section 3, item 6) gives a reporter a reason to write.

---

## 6. How ads data feeds AEO (the ads_to_aeo bridge)

A clicked search term is a real question with money behind it. Keyword tools estimate. Your search terms report is a list of what people typed and paid to see.

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 6.1 | Pull the raw clicked terms for the last 30 days. | `adsops_terms` | 5 min | List with clicks and cost |
| 6.2 | Turn terms into questions, check which pages already answer them, return briefs for the gaps. Pass your page URLs and the facts you can stand behind in site_facts. | `aeo_content_gaps` | 10 min | Top briefs listed with sources of every fact |
| 6.3 | Write the top 2 briefs as pages. | Claude writes, owner checks | 2 hours | Pages live and pass `aeo_page_audit` |
| 6.4 | Tell Bing about them. | `aeo_indexnow` (submit=true) | 5 min | 200 or 202 response |
| 6.5 | Check the landing pages for the ads also echo the query and town. | `adsops_lp_diagnose` | 10 min | Findings worst first, fixed |
| 6.6 | Find new questions from planner data. | `adsops_keyword_ideas` | 10 min | Clusters labelled by intent |

Rules:

- Feed it terms that had clicks and cost, not the zero-impression long tail.
- Briefs use only facts you supply. Never let the writer invent a price, a license, or a statistic.
- Add the pages to ad sitelinks when they help a buyer decide (a fee comparison, an area page, a "meet the person" page). Edit the desired state file and use the normal `adsops_plan` then `adsops_apply` flow.
- Do not read too much into a small AI-versus-ads comparison. In one week, ads spent $160 on 32 clicks for 0 leads while AI sent 2 owner leads. That is two leads. It says only that both channels need tracking.
- Track conversions properly. Audit with `adsops_conv_audit`. Platform "local actions" conversions (directions, website visits) can inflate lead counts. We demoted them to secondary and kept form leads, calls over 60 seconds and click-to-call as primary.
- While conversion data is unreliable, a per-click bid ceiling stops one bad day from eating the budget (early evidence, a judgment call).

---

## 7. Track what AI sends you

| Step | What | Tool | Time | Done when |
|---|---|---|---|---|
| 7.1 | Read a web-server log for AI referrals and AI crawler fetches. | `aeo_ai_traffic` (log_path) | 15 min | Verified, unverified and spoofed counts |
| 7.2 | Add a lead-form question: "If an AI sent you, which one, and what did you ask it?" | Claude edits form, owner approves | 30 min | Field live; answers stored in the lead book |
| 7.3 | Log every lead with its source. | `adsops_leads` (action=add, source=...) | ongoing | Pipeline by source works |
| 7.4 | Read pipeline by source. | `adsops_leads` (action=pipeline) | 5 min | AI-attributed leads counted |
| 7.5 | Compare cost per lead per channel on one ruler. | `channel_cac` | 15 min monthly | Cost per lead per channel |

Why:

- Many AI engines send no referrer. Some UTM tags survive, many do not. The form question is the only attribution we trust for now.
- A visit by an AI fetch bot before a lead does not prove it caused the lead. We retracted one such claim ourselves. User-agent strings are free to forge, so quote only verified counts.
- A counter that counts any form post from an AI-referred visitor will count your own test posts. Reconcile the counter with the lead book before you quote a number.
- Read the pattern: verified crawler hits up but referrals flat means the pages are fetched and not cited (fix pages). Referrals up but few leads means conversion (fix the first screen, see `local-leads.md`).

---

## 8. Cadence

### Week 1
Run the fast path above. Zero-row panel recorded.

### Days 8 to 30
- Weekly (30 minutes): `aeo_visibility_record`, then `aeo_site_audit`, then `aeo_page_audit` on the three most important pages.
- Weekly: `aeo_content_gaps`, write the top 1 or 2 briefs, then `aeo_indexnow` with the new URLs.
- Finish listing claims (section 5), get Bing near full coverage of your real pages, start asking every customer for a review.
- Build the first 3 or 4 service x area pages and the situation guides.
- Day 30 check: do Bing and Google show your real pages indexed? If not, fix that before writing more.

### Days 31 to 90
- Keep the weekly loop. Do not rewrite pages after one noisy panel run. Change a page only when two runs agree.
- Publish the one original-data page. Pitch it to a local reporter as a plain, honest email the owner approves.
- Build more area pages ONLY for areas whose first pages earned impressions or inquiries.
- Read `aeo_ai_traffic` monthly. Decide where to put effort by what the log and panel show.
- Day 90 check: compare the panel with the zero row. Honest target for a small business: unbranded mention in 10 to 20 percent of situation prompts, held across two runs. That was our planning number, not a forecast. One planning estimate said 10x growth is a 12-month goal, not 90 days.

### The weekly routine in one list (about 15 minutes of agent work)
1. `aeo_site_audit` 2. `aeo_page_audit` on 3 pages 3. Ask prompts, then `aeo_visibility_record` 4. `aeo_ai_traffic` 5. `aeo_content_gaps`, write, `aeo_indexnow`.

---

## 9. What to track (one scoreboard, five numbers)

1. Unbranded mention rate in the panel, per engine.
2. Number of your real URLs indexed in Bing and Google.
3. Verified AI crawler fetches and AI referrals per week.
4. AI-attributed leads per month, reconciled with the lead book (not just the counter).
5. Median minutes to first contact on every lead, and review count and pace.

Also keep: wrong-but-mentioned rows (fix list), and the list of hosts engines cite.

---

## 10. Myths and mistakes to avoid

- "Our schema score is 100 so we will be cited." No. Structure helps extraction. It does not decide recommendations.
- "More pages equals more visibility." Thin pages did not help and were stopped.
- "Press release on a wire." Not supported by our evidence.
- "Join every directory." Check the panel citations first.
- "Rank-style list prompts show real visibility." They can mislead (section 1).
- "Hide the weakness." Honest comparison pages with real "choose them when" sections are the format we would copy.
- Unverified savings numbers, "guaranteed" outcomes, fake or seeded reviews, and certifications you do not hold. One challenged claim poisons a page. State what the product does, with the date.
- Never quote a competitor's price you cannot source. Put the source link and "as of" date next to every third-party number. Use "Unverified" as a table value when that is the truth.
- Before big edits, run the page through a second reviewer (another model or a person). Our dual review caught off-brand claims that the writing model had made.

## 11. Rules for Claude while running this

- Read-only first. Every AEO tool is read-only except `aeo_indexnow` with submit=true. Validate before submitting.
- Do not publish or post anything for the owner. Draft it, then wait for approval.
- If a fact is missing, ask the owner. Do not fill it in.
- Report in plain English: what changed, what is still owed, and the one next step.
