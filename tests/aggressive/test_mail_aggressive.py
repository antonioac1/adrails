"""Aggressive tests for adsops.mail (the maintainers, 2026-11-05). Written by hand, independent of the module selftests and the DeepSeek
black-box suites. Replays the real failures of a live postcard program (truncated ZIP+4 / unit numbers, foreign addresses,
institutional owners, stale suppression, pre-mail test scans, link-preview bots) plus property and adversarial checks.
Run: PYTHONPATH=. python3 tests/aggressive/test_mail_aggressive.py"""
import html, itertools, json, math, random, re, unittest


from adsops.mail import cac, codes, hygiene, labels, response, scans
from adsops.errors import ToolError

VALID = codes.generate(1, seed=123)[0]   # a code with a correct check character


class Codes(unittest.TestCase):
    def test_check_char_catches_every_single_substitution_and_adjacent_swap(self):
        rng = random.Random(0)
        for code in codes.generate(300, seed=1):
            self.assertTrue(codes.is_valid(code))
            for i, a in itertools.product(range(len(code)), codes.ALPHABET):   # every single-character substitution
                if a != code[i]:
                    self.assertFalse(codes.is_valid(code[:i] + a + code[i + 1:]), (code, i, a))
            for i in range(len(code) - 1):                                  # every adjacent transposition
                if code[i] != code[i + 1]:
                    self.assertFalse(codes.is_valid(code[:i] + code[i + 1] + code[i] + code[i + 2:]), (code, i))

    def test_unique_no_collisions_with_existing_and_deterministic(self):
        a = codes.generate(5000, seed=42)
        self.assertEqual(len(set(a)), 5000)
        self.assertEqual(a, codes.generate(5000, seed=42))
        b = codes.generate(2000, existing=set(a), seed=43)
        self.assertFalse(set(a) & set(b))

    def test_no_ambiguous_characters_and_phone_readback(self):
        for c in codes.generate(1000, seed=2):
            self.assertFalse(set(c) & set("01ILOU"))
            self.assertEqual(codes.normalize(" " + "-".join(c[:3]) + c[3:].lower() + " "), c)   # read back with dashes/case

    def test_no_guessing(self):
        c = codes.generate(1, seed=3)[0]
        self.assertIsNone(codes.normalize(c.replace(c[0], "O", 1)))       # a lookalike is refused, never "corrected"
        self.assertIsNone(codes.normalize(""))
        self.assertIsNone(codes.normalize("'; DROP TABLE"))

    def test_space_too_small_refused(self):
        with self.assertRaises(ToolError):
            codes.generate(1000, length=3)

    def test_tracking_url(self):
        u = codes.tracking_url("https://example.com/rent-estimate/?x=1#top", VALID, utm={"utm_source": "direct_mail",
                                                                                            "utm_campaign": "dec 2026"})
        self.assertTrue(u.startswith("https://example.com/rent-estimate/?"))
        self.assertIn("x=1", u); self.assertIn("c=" + VALID, u); self.assertIn("utm_campaign=dec+2026", u.replace("%20", "+"))
        self.assertTrue(u.endswith("#top"))
        for bad in ("http://example.com/", "javascript:alert(1)", "https:///nohost", "ftp://x.com/"):
            with self.assertRaises(ToolError):
                codes.tracking_url(bad, VALID)
        self.assertEqual(codes.parse_code_from_url(codes.tracking_url("https://e.com/", codes.generate(1, seed=9)[0]).lower()),
                         codes.generate(1, seed=9)[0])


def rec(**kw):
    base = {"name": "Pat Owner", "address_line_1": "123 Main Street", "address_line_2": "", "city": "Tampa", "state": "FL",
            "zip": "33602"}
    base.update(kw)
    return base


class Hygiene(unittest.TestCase):
    def test_zip4_and_unit_never_truncated(self):
        r = rec(name="Christopher Alexander Montgomery-Wellington III", address_line_1="27560 Longwood Plantation Boulevard",
                address_line_2="APT 4B-NORTH TOWER", city="Wesley Chapel", zip="33544-5712")
        lines = hygiene.label_lines(r)
        flat = " ".join(lines)
        for piece in ("33544-5712", "APT 4B-NORTH TOWER", "Montgomery-Wellington", "Plantation"):
            self.assertIn(piece, flat)                 # the exact bytes that a 40-char cut used to drop
        self.assertTrue(all(len(l) <= 40 for l in lines))

    def test_overflow_is_refused_not_cut(self):
        with self.assertRaises(ToolError):
            hygiene.label_lines(rec(name="X " * 100), max_lines=5)

    def test_foreign_incomplete_institutional_own_suppressed(self):
        recs = [rec(),                                                                  # 0 keep
                rec(country="Canada", state="ON", zip="M5V 2T6"),                        # 1 foreign
                rec(city=""),                                                            # 2 incomplete
                rec(name="TRICON SFR 2020-1 BORROWER LLC"),                              # 3 institutional
                rec(name="Smith Family Trust", address_line_1="9 Oak Ave"),             # 4 keep (small landlord trust)
                rec(name="Bay Holdings LLC", address_line_1="10 Elm Rd"),               # 5 keep (plain LLC)
                rec(name="WELLS FARGO BANK NA", address_line_1="1 Bank Plz"),            # 6 institutional
                rec(address_line_1="500 Own Property Dr"),                               # 7 own property
                rec(name="Current Client", address_line_1="77 Client Ct"),               # 8 suppressed client
                rec(address_line_1="123 MAIN ST."),                                      # 9 duplicate of 0 after normalising
                rec(zip="2134", state="MA", address_line_1="4 Beacon St", city="Boston"),   # 10 keep, zip fixed to 02134
                rec(zip="3360", address_line_1="4 Short Zip Ln")]                        # 11 invalid zip (FL can't lose a zero)
        own = [hygiene.normalize_address(rec(address_line_1="500 Own Property Drive"))["key"]]
        out = hygiene.clean(recs, suppress={"clients": [rec(name="current client", address_line_1="77 CLIENT COURT")]},
                            own_addresses=own)
        dropped = {d["index"]: d["reason"] for d in out["dropped"]}
        self.assertEqual(sorted(dropped), [1, 2, 3, 6, 7, 8, 9, 11], dropped)
        self.assertTrue(dropped[1].startswith("foreign")); self.assertTrue(dropped[3].startswith("institutional"))
        self.assertTrue(dropped[8].startswith("suppressed:clients")); self.assertEqual(dropped[9], "duplicate")
        self.assertEqual(dropped[7], "own_property")
        kept_zip = [k for k in out["kept"] if "Beacon" in k["address_line_1"]][0]
        self.assertIn("02134", " ".join(kept_zip["_label"]))
        self.assertEqual(out["counts"]["input"], 12); self.assertEqual(out["counts"]["kept"], 4)

    def test_empty_client_list_is_warned(self):
        st = hygiene.suppression_stats({"clients": [], "leads": [rec()]})
        self.assertTrue(any("client" in str(w).lower() for w in (st.get("warnings") or [st.get("warning")] if isinstance(st, dict) else [st])), st)

    def test_normalisation_symmetry(self):
        a = hygiene.normalize_address(rec(address_line_1="12 North West Avenue", address_line_2="Suite 5"))
        b = hygiene.normalize_address(rec(address_line_1="12 N W AVE.", address_line_2="ste 5"))
        self.assertEqual(a["key"], b["key"])


class Labels(unittest.TestCase):
    def pieces(self, n):
        return [rec(name="Owner %d <script>" % i, address_line_1="%d Long Avenue" % i, zip="33602-1234",
                    tracking_code=codes.generate(n, seed=7)[i]) for i in range(n)]

    def test_sample_is_default_and_text_escaped(self):
        h = labels.sheet_html(self.pieces(3))
        self.assertIn("SAMPLE", h); self.assertNotIn("<script>", h); self.assertIn("&lt;script&gt;", h)
        self.assertNotIn("SAMPLE - DO NOT MAIL", labels.sheet_html(self.pieces(3), sample=False))

    def test_every_piece_and_zip4_present_pages_exact(self):
        ps = self.pieces(61)
        h = labels.sheet_html(ps, sample=False)
        for p in ps:
            self.assertIn(html.escape(p["name"]), h); self.assertIn(p["tracking_code"], h)
        self.assertEqual(h.count("33602-1234"), 61)
        self.assertEqual(labels.counts(ps, "avery_5160"), {"pieces": 61, "sheets": 3, "labels_last_sheet": 1})
        self.assertIn("2.75", json.dumps(labels.TEMPLATES["avery_5160"]))

    def test_csv_injection_guard(self):
        out = labels.vendor_csv([rec(name="=HYPERLINK(\"http://evil\")"), rec(name="+1 555"), rec(name="@SUM(A1)")])
        for line in out.splitlines()[1:]:
            self.assertFalse(re.match(r'^"?[=+@-]', line), line)


class Scans(unittest.TestCase):
    MAILED = {VALID: "2026-11-02T18:00:00+00:00"}

    def line(self, ip, ts, path, ua="Mozilla/5.0 (iPhone)", status=200, method="GET"):
        return '%s - - [%s] "%s %s HTTP/1.1" %d 512 "-" "%s"' % (ip, ts, method, path, status, ua)

    def test_filters_and_privacy(self):
        L = [self.line("8.8.8.8", "03/Nov/2026:10:00:00 -0500", "/rent-estimate/?c=" + VALID.lower()),         # real
             self.line("8.8.8.8", "03/Nov/2026:10:05:00 -0500", "/rent-estimate/?c=" + VALID),         # same visitor again
             self.line("9.9.9.9", "04/Nov/2026:09:00:00 -0500", "/?c=" + VALID, ua="Mozilla/5.0 (Android)"),  # 2nd visitor
             self.line("1.2.3.4", "01/Nov/2026:10:00:00 -0500", "/?c=" + VALID),                          # before mailing
             self.line("47.0.0.9", "03/Nov/2026:11:00:00 -0500", "/?c=" + VALID),                         # own CIDR
             self.line("2001:db8::5", "03/Nov/2026:11:00:00 -0500", "/?c=" + VALID),                      # own v6 CIDR
             self.line("5.5.5.5", "03/Nov/2026:11:00:00 -0500", "/?c=" + VALID, ua="facebookexternalhit/1.1"),
             self.line("5.5.5.6", "03/Nov/2026:11:00:00 -0500", "/?c=" + VALID, ua="Slackbot-LinkExpanding 1.0"),
             self.line("5.5.5.7", "03/Nov/2026:11:00:00 -0500", "/?c=ZZZ9999"),                          # unknown code
             self.line("5.5.5.8", "03/Nov/2026:11:00:00 -0500", "/?c=" + VALID, method="POST"),
             self.line("5.5.5.9", "03/Nov/2026:11:00:00 -0500", "/?c=" + VALID, status=404),
             "garbage line"]
        r = scans.count(L, self.MAILED, own_cidrs=("47.0.0.0/8", "2001:db8::/32"), normalizer=codes.normalize)
        self.assertEqual(r["scans"][VALID]["visitors"], 2, r)
        d = r["dropped"]
        self.assertEqual((d["before_mailing"], d["own_ip"], d["bot"], d["unknown_code"]), (1, 2, 2, 1), d)
        self.assertNotIn("8.8.8.8", json.dumps(r)); self.assertNotIn("9.9.9.9", json.dumps(r))   # raw IPs never leave
        self.assertGreater(r["scans"][VALID]["days_to_first_scan"], 0.5)

    def test_merge_is_idempotent(self):
        L = [self.line("8.8.8.8", "03/Nov/2026:10:00:00 -0500", "/?c=" + VALID)]
        r = scans.count(L, self.MAILED, normalizer=codes.normalize)
        m = scans.merge(r, r)
        self.assertEqual(m["scans"][VALID]["visitors"], 1)


class Response(unittest.TestCase):
    def test_wilson_reference_values(self):
        lo, hi = response.wilson(0, 500)
        self.assertEqual(lo, 0.0); self.assertAlmostEqual(hi, 0.00762, places=4)    # 0 of 500: "could still be up to 0.76%"
        lo, hi = response.wilson(5, 500)
        self.assertAlmostEqual(lo, 0.00428, places=4); self.assertAlmostEqual(hi, 0.02320, places=3)
        self.assertIsNone(response.wilson(0, 0))

    def test_holdout_size_matches_textbook_formula(self):
        p1, p2, za, zb = 0.015, 0.01, 1.959964, 0.841621
        pbar = (p1 + p2) / 2
        n = ((za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2) / (p1 - p2) ** 2
        got = response.holdout_size(0.01, 0.5)
        self.assertLess(abs(got["treatment"] - n) / n, 0.03, (got, n))   # within 3% of the textbook pooled formula

    def test_too_early_and_no_signal(self):
        pieces = [{"tracking_code": c, "mailed_at": "2026-11-02", "batch": "nov"} for c in codes.generate(500, seed=4)]
        rows = response.rate_table(pieces, {}, today="2026-11-05")
        self.assertIn("too early", rows[0]["verdict"])
        rows = response.rate_table(pieces, {}, today="2027-01-30")
        self.assertRegex(rows[0]["verdict"], r"up to 0\.(76|8)")   # 0 of 500 -> upper bound 0.762%

    def test_compare_symmetry(self):
        a = response.compare(30, 1000, 15, 1000); b = response.compare(15, 1000, 30, 1000)
        self.assertAlmostEqual(a["diff"], -b["diff"]); self.assertAlmostEqual(a["p_value"], b["p_value"])
        self.assertTrue(a["significant"])


class CAC(unittest.TestCase):
    def test_one_ruler_and_precedence(self):
        mailed = {VALID}
        costs = [{"channel": "google_ads", "date": "2026-11-01", "amount": 701.0},
                 {"channel": "direct_mail", "date": "2026-11-02", "amount": 395.0},
                 {"channel": "apm", "date": "2026-11-01", "amount": 267.0}]
        leads = [{"id": 1, "created_at": "2026-11-03", "stage": "lead", "tracking_code": VALID, "utm_source": "google",
                  "utm_medium": "cpc"},                                  # code beats utm
                 {"id": 2, "created_at": "2026-11-03", "stage": "client", "utm_source": "google", "utm_medium": "cpc"},
                 {"id": 3, "created_at": "2026-11-03", "stage": "lead", "self_reported": "got your postcard"},
                 {"id": 4, "created_at": "2026-11-03", "stage": "lead"}]
        r = cac.channel_report(costs, leads, mailed_codes=mailed, client_value=3000)
        ch = {c["channel"]: c for c in r["channels"]}
        self.assertEqual(ch["direct_mail"]["leads"], 2); self.assertEqual(ch["google_ads"]["leads"], 1)
        self.assertEqual(ch["google_ads"]["clients"], 1); self.assertAlmostEqual(ch["google_ads"]["cac"], 701.0)
        self.assertEqual(ch["apm"]["leads"], 0); self.assertIn("spend without leads", ch["apm"]["note"])
        self.assertIsNone(ch["apm"]["cpl"]); self.assertEqual(r["unattributed"], 1)

    def test_breakeven_and_piece_cost(self):
        pc = cac.piece_cost(0.096, 0.65, 0.04)
        self.assertAlmostEqual(pc["per_piece"], 0.786, places=3)
        b = cac.breakeven(0.786, 0.25, 3000)
        self.assertAlmostEqual(b["breakeven_lead_rate"], 0.786 / 750, places=6)
        with self.assertRaises(ToolError):
            cac.breakeven(0, 0.25, 3000)


if __name__ == "__main__":
    unittest.main(verbosity=1)
