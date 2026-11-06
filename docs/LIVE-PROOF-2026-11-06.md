# ChatGPT Ads adapter: live read-only proof (2026-11-06, the maintainers)

Account: adacct_EXAMPLE0000000000000000000000 (a real OpenAI Ads account). Config: mode=read_only, commit_policy.allow_commit=false, throwaway state copy with the market id from the live geo lookup. Nothing was written to the account.

Live run found and fixed one production bug before release: insights `time_ranges.end` must be a full hour in the account timezone. The adapter sent "now", so every spend read would have failed (the guard fails closed). Fix: `openai_ads.insights_end` rounds UP (rounding down would drop the current hour's spend from the guard). The QA FakeAPI now enforces the rule, and `tests/qa_openai_suite.py` proves it at an off-hour (the control run without the fix fails).

```
LIVE READ-ONLY PROOF 2026-11-06T18:12:27+00:00 account mode=read_only, commit_policy.allow_commit=false
OK  adsops_openai_account | {"account": "acme_openai", "mode": "read_only", "id": "adacct_EXAMPLE0000000000000000000000", "account_name": "Acme PM", "brand_name": "Acme PM", "legal_name": "Acme PM", "url": "https://acmepm.example/", "status": "active", "curren
OK  adsops_openai_status | {"campaign": "bt_chatgpt_owners", "exists": false, "live": null, "serving_issues": null, "ad_groups": [], "spend_today": 0.0, "spend_life": 0.0, "cap_usd": 500, "pause_at_usd": 428.0, "daily_pause_at_usd": 54.0, "guard_heartbeat_age_min": null}
ERR adsops_openai_plan | ad flat149: creative.file_id is required to create a chat_card; upload a >=640x640 image with adsops_openai_upload_image and put its file_id in the state
ERR adsops_openai_apply | ad flat149: creative.file_id is required to create a chat_card; upload a >=640x640 image with adsops_openai_upload_image and put its file_id in the state
ERR adsops_openai_apply commit | commit disabled by commit_policy.allow_commit; run without commit to validate
OK  adsops_openai_guard | {"campaign": "bt_chatgpt_owners", "action": "none", "why": ["campaign not active (or not created): nothing to guard"], "committed": false, "spend_today": 0.0, "spend_life": 0.0, "account_spend": <redacted>, "ops_count": 0, "res": {}}
OK  adsops_openai_geo_lookup | {"count": 10, "query": "Tampa", "results": [{"id": "3000039", "type": "market", "canonical_name": "Tampa - St. Petersburg (Sarasota), United States", "country_code": "US", "name": "Tampa - St. Petersburg (Sarasota)", "parent_country": "United States", "parent_id": null}, {"id": "10203009", "type": "
```

**Verified live:** key ownership check, account read, status with live spend (campaign + account), cap/pause math, guard decision, geo lookup, validation refusals. **Not yet exercised live:** creates, image upload, activation, conversions commit, audience commit.
