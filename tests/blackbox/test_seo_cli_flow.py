"""QA round 2 N6/N15: the whole operator flow through the CLI, end to end, against a loopback model API and config-driven
test fixtures (Search Console rows + site pages): --init -> profile -> approve facts -> plan -> run -> request approval
-> approve -> publish -> results. Human-mode publish must work from the CLI, and the bundle must be on the chain."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.blackbox.test_seo_content import ModelServer, QUOTE, IRS_TEXT, html

BODY = ("## What renters insurance covers\n\nRenters insurance protects the tenant's own belongings and liability; the owner's "
        "policy covers the building. Ask every tenant for proof of a policy before move-in and keep a copy with the lease. "
        "The IRS says that \"%s\", so keep insurance and rent records together for tax time. Our management fee is $149 per "
        "month per home, and checking proof of insurance is part of the move-in process we run for owners. A clear policy "
        "list makes claims simpler for everyone, and it helps owners compare what each tenant carries. Review the policy "
        "each year at renewal time and note any change in the tenant file. If a tenant lets the policy lapse, ask for a new "
        "certificate in writing and record the date it arrived." % QUOTE)


class CliFlow(unittest.TestCase):
    def test_init_to_publish_to_results(self):
        models = ModelServer()
        self.addCleanup(models.close)
        d = tempfile.mkdtemp(prefix="adrails-cli-")
        env = dict(os.environ, PYTHONPATH=os.getcwd(), T_GLM_KEY="glm-k", T_GPT_KEY="gpt-k",
                   ADRAILS_APPROVAL_SECRET="s" * 40)

        def cli(*args):
            p = subprocess.run([sys.executable, "-m", "adsops", "seo-run"] + list(args), capture_output=True, text=True,
                               env=env, timeout=120, cwd=os.getcwd())
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return json.loads(p.stdout)

        out = cli("--init", d)
        self.assertEqual(len(out["written"]), 6)
        prof = json.load(open(os.path.join(d, "openrouter.example.json")))
        prof.update(site="https://example.com/", gsc_site=None, facts_file="FACTS.json",
                    author={"name": "Jane Doe", "url": "/team/jane/", "id": "https://example.com/team/jane/#person"},
                    allowed_source_domains=["*.gov", "example.com"], reference_sources=["https://www.irs.gov/taxtopics/tc414"],
                    min_words={"new": 120, "extend": 30}, publish={"target": "static", "dir": "site"},
                    writers=[{"name": "glm", "kind": "openai_compatible", "base_url": models.url("w"), "key_env": "T_GLM_KEY",
                              "model": "glm-5.3", "retries": 0}],
                    reviewers=[{"name": "gpt", "kind": "openai_compatible", "base_url": models.url("r"),
                                "key_env": "T_GPT_KEY", "model": "openai/gpt-5", "retries": 0}])
        prof.pop("_comment", None)
        json.dump(prof, open(os.path.join(d, "site-profile.json"), "w"))
        os.makedirs(os.path.join(d, "site"))
        json.dump({"version": 1, "facts": [{"id": "fee", "text": "Our management fee is $149 per month per home."}]},
                  open(os.path.join(d, "FACTS.json"), "w"))
        P = os.path.join(d, "site-profile.json")
        self.assertEqual(cli("--profile", P, "--approve-facts", "Jane Doe")["approved_by"], "Jane Doe")
        rows = [{"keys": [q, ""], "impressions": 6, "clicks": 0, "position": 25.0}
                for q in ("how much is renters insurance", "what is renters insurance cost", "why is renters insurance required")]
        json.dump({"query,page": rows, "date,page": []}, open(os.path.join(d, "gsc.json"), "w"))
        pages = {"https://example.com/sitemap.xml": "<urlset><url><loc>https://example.com/</loc></url></urlset>",
                 "https://example.com/": html("Home", "We manage rental homes for owners with clear monthly pricing."),
                 "https://www.irs.gov/taxtopics/tc414": html("Topic 414", IRS_TEXT)}
        json.dump(pages, open(os.path.join(d, "pages.json"), "w"))
        json.dump({"tenant": "t", "accounts": [], "local": {"business": "x"}, "audit_log": os.path.join(d, "state", "audit.jsonl"),
                   "seo_content": {"test_fixtures": {"gsc": "gsc.json", "site": "pages.json"}}},
                  open(os.path.join(d, "config.json"), "w"))
        C = os.path.join(d, "config.json")
        plan = cli("--profile", P, "--config", C, "--stage", "plan")
        self.assertTrue(plan["fixtures"])
        self.assertEqual([c["mode"] for c in plan["candidates"]], ["new"])
        slug = plan["candidates"][0]["slug"]
        draft = {"mode": "new", "slug": slug, "title": "Renters insurance for rental owners",
                 "description": "What renters insurance covers, what the owner's policy covers, and how to keep proof of "
                                "insurance with the lease.", "content_type": "guide", "body_md": BODY,
                 "faq": [{"q": "Who needs renters insurance?", "a": "The tenant, for their own belongings."}],
                 "sources": [{"url": "https://www.irs.gov/taxtopics/tc414", "quote": QUOTE}], "live_changes": [],
                 "internal_links": [{"from": "/", "anchor": "renters insurance"}], "jsonld": None}
        models.script("w", ("glm-5.3", draft, "stop"))
        models.script("r", ("openai/gpt-5", {"verdict": "approve", "findings": []}, "stop"))
        run = cli("--profile", P, "--config", C, "--stage", "run")
        self.assertEqual(run["verdict"], "approve", run)
        bid = os.path.basename(run["bundle"])
        req = cli("--profile", P, "--config", C, "--stage", "request", "--bundle", bid)
        dec = cli("--profile", P, "--config", C, "--approve-token", req["approve_token"])
        self.assertEqual(dec["status"], "approved")
        pub = cli("--profile", P, "--config", C, "--stage", "publish", "--bundle", bid)
        self.assertEqual(pub["refused"], [], pub)
        self.assertTrue(os.path.exists(os.path.join(d, "site") + slug + "index.md"))
        chain = open(os.path.join(d, "state", "audit-chain.jsonl")).read()
        for event in ("content_bundle", "content_approval_request", "content_approval_decision", "content_publish"):
            self.assertIn(event, chain)
        res = cli("--profile", P, "--config", C, "--stage", "results")
        self.assertEqual(res["pieces"], 1)


if __name__ == "__main__":
    unittest.main()
