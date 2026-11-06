import csv
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone

from adsops.local.leadbook import LeadBook, ToolError


BASE_TS = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()


class FakeClock:
    def __init__(self, ts):
        self.ts = ts

    def __call__(self):
        return self.ts

    def advance(self, seconds):
        self.ts += seconds


class LeadBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "leadbook.json")
        self.clock = FakeClock(BASE_TS)
        self.lb = LeadBook(self.path, now=self.clock)
        self.lb.load()

    def test_missing_file_loads_empty_book(self):
        self.lb.load()
        result = self.lb.find()
        self.assertEqual(result["leads"], [])
        self.assertEqual(result["total"], 0)

    def test_corrupt_file_raises_and_is_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json")
        before = open(self.path, "r", encoding="utf-8").read()

        with self.assertRaises(ToolError):
            LeadBook(self.path, now=self.clock).load()

        after = open(self.path, "r", encoding="utf-8").read()
        self.assertEqual(before, after)

    def test_upsert_creates_new_lead_with_defaults(self):
        result = self.lb.upsert({
            "name": "Alice",
            "phone": "(813) 555-0100",
            "email": "ALICE@EXAMPLE.COM",
            "source": "website",
            "service": "Plumbing",
            "area": "Tampa",
            "value": 100,
        })

        self.assertTrue(result["created"])
        lead = result["lead"]
        self.assertEqual(lead["stage"], "new")
        self.assertEqual(lead["next_follow_up"], "2024-01-15")
        self.assertEqual(lead["phone"], "+18135550100")
        self.assertEqual(lead["email"], "alice@example.com")
        self.assertEqual(lead["source"], "website")
        self.assertEqual(lead["value"], 100.0)
        self.assertEqual(lead["created_at"], "2024-01-15T12:00:00Z")
        self.assertTrue(lead["id"].startswith("L"))
        self.assertEqual(len(lead["id"]), 9)

    def test_dedupe_by_source_id_updates_existing_lead(self):
        first = self.lb.upsert({
            "source": "google_lsa",
            "source_id": "LSA-1",
            "name": "Alice",
            "phone": "8135550100",
        })
        second = self.lb.upsert({
            "source": "google_lsa",
            "source_id": "LSA-1",
            "name": "Alice Smith",
            "phone": "8135550100",
            "service": "Roofing",
        })

        self.assertFalse(second["created"])
        self.assertEqual(second["lead"]["id"], first["lead"]["id"])
        self.assertEqual(second["lead"]["name"], "Alice Smith")
        self.assertEqual(second["lead"]["service"], "Roofing")
        self.assertEqual(self.lb.find()["total"], 1)

    def test_merge_by_phone_across_formats_keeps_original_source(self):
        first = self.lb.upsert({
            "source": "website",
            "phone": "(813) 555-0100",
            "name": "Alice",
            "email": "alice@example.com",
        })
        second = self.lb.upsert({
            "source": "google_lsa",
            "source_id": "LSA-2",
            "phone": "813.555.0100",
            "name": "Alice",
            "email": "alice@example.com",
        })

        self.assertFalse(second["created"])
        lead = second["lead"]
        self.assertEqual(lead["id"], first["lead"]["id"])
        self.assertEqual(lead["source"], "website")
        self.assertEqual(lead["phone"], "+18135550100")
        self.assertIn("repeat", lead["tags"])
        note_texts = [n["text"] for n in lead["notes"]]
        self.assertIn("also came in via google_lsa", note_texts)
        self.assertEqual(self.lb.find()["total"], 1)

    def test_merge_by_email_only(self):
        first = self.lb.upsert({
            "source": "website",
            "name": "Alice",
            "email": "a@example.com",
        })
        second = self.lb.upsert({
            "source": "referral",
            "name": "Alice Again",
            "email": "A@EXAMPLE.COM",
        })

        self.assertFalse(second["created"])
        self.assertEqual(second["lead"]["id"], first["lead"]["id"])
        self.assertEqual(second["lead"]["source"], "website")
        self.assertIn("repeat", second["lead"]["tags"])

    def test_no_merge_into_won_lead(self):
        first = self.lb.upsert({
            "source": "website",
            "phone": "8135550100",
            "name": "Alice",
        })
        self.lb.move(first["lead"]["id"], "won", value=500)

        second = self.lb.upsert({
            "source": "google_lsa",
            "phone": "813.555.0100",
            "name": "Alice",
        })

        self.assertTrue(second["created"])
        self.assertNotEqual(second["lead"]["id"], first["lead"]["id"])
        self.assertEqual(self.lb.find()["total"], 2)

    def test_merge_never_downgrades_stage(self):
        first = self.lb.upsert({
            "source": "website",
            "phone": "8135550100",
            "name": "Alice",
        })
        self.lb.move(first["lead"]["id"], "contacted")

        second = self.lb.upsert({
            "source": "google_lsa",
            "phone": "8135550100",
            "name": "Alice",
        })

        self.assertFalse(second["created"])
        self.assertEqual(second["lead"]["stage"], "contacted")

    def test_unknown_source_maps_to_other(self):
        result = self.lb.upsert({
            "source": "carrier_pigeon",
            "name": "Pigeon",
            "phone": "8135550100",
        })
        self.assertEqual(result["lead"]["source"], "other")

    def test_phone_and_email_normalization(self):
        cases = [
            ("(813) 555-0100", "+18135550100"),
            ("813.555-0101", "+18135550101"),
            ("+1 813 555 0102", "+18135550102"),
            ("1-813-555-0103", "+18135550103"),
            ("12345678", "+12345678"),
            ("1234567", ""),
        ]
        for i, (raw, expected) in enumerate(cases):
            result = self.lb.upsert({
                "source": "website",
                "name": "Phone %d" % i,
                "phone": raw,
            })
            self.assertEqual(result["lead"]["phone"], expected)

        result = self.lb.upsert({
            "source": "website",
            "name": "Email One",
            "email": "  ALICE@Example.COM  ",
        })
        self.assertEqual(result["lead"]["email"], "alice@example.com")

        result = self.lb.upsert({
            "source": "website",
            "name": "Email Two",
            "email": "not-an-email",
        })
        self.assertEqual(result["lead"]["email"], "")

    def test_name_and_note_length_caps(self):
        result = self.lb.upsert({
            "source": "website",
            "name": "N" * 250,
            "phone": "8135550100",
        })
        self.assertEqual(len(result["lead"]["name"]), 200)

        lead = self.lb.note(result["lead"]["id"], "x" * 2500)
        self.assertEqual(len(lead["notes"][-1]["text"]), 2000)

    def test_move_stage_validation_and_won_lost_rules(self):
        first = self.lb.upsert({
            "source": "website",
            "name": "Alice",
            "phone": "8135550100",
        })
        lead_id = first["lead"]["id"]

        with self.assertRaises(ToolError):
            self.lb.move(lead_id, "not_a_stage")
        with self.assertRaises(ToolError):
            self.lb.move(lead_id, "lost")
        with self.assertRaises(ToolError):
            self.lb.move(lead_id, "won")

        won = self.lb.move(lead_id, "won", value=123)
        self.assertEqual(won["stage"], "won")
        self.assertEqual(won["won_value"], 123)
        self.assertTrue(won["won_at"])

        second = self.lb.upsert({
            "source": "website",
            "name": "Bob",
            "phone": "8135550101",
        })
        with self.assertRaises(ToolError):
            self.lb.move(second["lead"]["id"], "lost")

        lost = self.lb.move(second["lead"]["id"], "lost", lost_reason="price")
        self.assertEqual(lost["stage"], "lost")
        self.assertEqual(lost["lost_reason"], "price")

    def test_reopen_requires_note(self):
        first = self.lb.upsert({
            "source": "website",
            "name": "Alice",
            "phone": "8135550100",
        })
        lead_id = first["lead"]["id"]
        self.lb.move(lead_id, "won", value=10)

        with self.assertRaises(ToolError):
            self.lb.move(lead_id, "contacted")

        reopened = self.lb.move(lead_id, "contacted", note="customer called back")
        self.assertEqual(reopened["stage"], "contacted")

        second = self.lb.upsert({
            "source": "website",
            "name": "Bob",
            "phone": "8135550101",
        })
        self.lb.move(second["lead"]["id"], "lost", lost_reason="price")
        with self.assertRaises(ToolError):
            self.lb.move(second["lead"]["id"], "new")

        reopened = self.lb.move(second["lead"]["id"], "new", note="reopen")
        self.assertEqual(reopened["stage"], "new")

    def test_follow_up_auto_dates(self):
        result = self.lb.upsert({
            "source": "website",
            "name": "Alice",
            "phone": "8135550100",
        })
        lead_id = result["lead"]["id"]
        self.assertEqual(result["lead"]["next_follow_up"], "2024-01-15")

        lead = self.lb.move(lead_id, "contacted")
        self.assertEqual(lead["next_follow_up"], "2024-01-17")

        lead = self.lb.move(lead_id, "quoted")
        self.assertEqual(lead["next_follow_up"], "2024-01-18")

        lead = self.lb.move(lead_id, "booked")
        self.assertEqual(lead["next_follow_up"], "")

        lead = self.lb.move(lead_id, "won", value=0)
        self.assertEqual(lead["next_follow_up"], "")

    def test_note_relative_follow_up_and_get_unknown(self):
        result = self.lb.upsert({
            "source": "website",
            "name": "Alice",
            "phone": "8135550100",
        })
        lead_id = result["lead"]["id"]

        lead = self.lb.note(lead_id, "follow up", follow_up="+3")
        self.assertEqual(lead["next_follow_up"], "2024-01-18")
        self.assertEqual(lead["notes"][-1]["text"], "follow up")

        lead = self.lb.note(lead_id, "later", follow_up="2024-02-01")
        self.assertEqual(lead["next_follow_up"], "2024-02-01")
        self.assertEqual(lead["notes"][-1]["text"], "later")

        with self.assertRaises(ToolError):
            self.lb.get("LNOTAREALID")

    def test_due_lists_overdue_today_and_new_uncontacted(self):
        overdue = self.lb.upsert({
            "source": "website",
            "name": "Overdue",
            "phone": "8135550200",
        })
        self.lb.move(overdue["lead"]["id"], "contacted")
        self.lb.note(overdue["lead"]["id"], "schedule", follow_up="2024-01-10")

        today = self.lb.upsert({
            "source": "website",
            "name": "Today",
            "phone": "8135550201",
        })
        self.lb.move(today["lead"]["id"], "contacted")
        self.lb.note(today["lead"]["id"], "schedule", follow_up="2024-01-15")

        speed = self.lb.upsert({
            "source": "website",
            "name": "Speed",
            "phone": "8135550202",
        })

        self.clock.advance(20 * 60)
        result = self.lb.due(today="2024-01-15")

        overdue_ids = [lead["id"] for lead in result["overdue"]]
        today_ids = [lead["id"] for lead in result["today"]]
        new_ids = [lead["id"] for lead in result["new_uncontacted"]]

        self.assertIn(overdue["lead"]["id"], overdue_ids)
        self.assertIn(today["lead"]["id"], today_ids)
        self.assertIn(speed["lead"]["id"], new_ids)

        speed_row = next(
            lead for lead in result["new_uncontacted"]
            if lead["id"] == speed["lead"]["id"]
        )
        self.assertGreaterEqual(speed_row["waiting_minutes"], 15)

    def test_pipeline_math_and_median_first_contact(self):
        a = self.lb.upsert({
            "source": "website",
            "name": "A",
            "phone": "8135550300",
            "value": 1000,
        })
        b = self.lb.upsert({
            "source": "website",
            "name": "B",
            "phone": "8135550301",
            "value": 300,
        })
        c = self.lb.upsert({
            "source": "google_lsa",
            "name": "C",
            "phone": "8135550302",
            "value": 200,
        })

        self.clock.advance(10 * 60)
        self.lb.move(a["lead"]["id"], "contacted")

        self.clock.advance(10 * 60)
        self.lb.move(c["lead"]["id"], "lost", lost_reason="price")

        self.clock.advance(5 * 60)
        self.lb.move(a["lead"]["id"], "quoted")
        self.lb.move(a["lead"]["id"], "won", value=500)

        pipe = self.lb.pipeline()

        self.assertEqual(pipe["by_stage"].get("won"), 1)
        self.assertEqual(pipe["by_stage"].get("new"), 1)
        self.assertEqual(pipe["by_stage"].get("lost"), 1)

        website = pipe["by_source"]["website"]
        self.assertEqual(website["leads"], 2)
        self.assertEqual(website["won"], 1)
        self.assertEqual(website["won_value"], 500)
        self.assertEqual(website["win_rate"], 0.5)

        lsa = pipe["by_source"]["google_lsa"]
        self.assertEqual(lsa["leads"], 1)
        self.assertEqual(lsa["won"], 0)
        self.assertEqual(lsa["won_value"], 0)
        self.assertEqual(lsa["win_rate"], 0.0)

        self.assertEqual(pipe["open_value"], 300)
        self.assertEqual(pipe["won_value"], 500)
        self.assertEqual(pipe["median_first_contact_minutes"], 15)

    def test_pipeline_zero_division_win_rate_is_none(self):
        spam = self.lb.upsert({
            "source": "social",
            "name": "Spam",
            "phone": "8135550400",
        })
        self.lb.move(spam["lead"]["id"], "spam")

        pipe = self.lb.pipeline()
        social = pipe["by_source"]["social"]
        self.assertEqual(social["leads"], 1)
        self.assertEqual(social["won"], 0)
        self.assertIsNone(social["win_rate"])

    def test_find_text_stage_source_limit_and_newest_first(self):
        r1 = self.lb.upsert({
            "source": "website",
            "name": "Alice",
            "phone": "8135550500",
            "email": "alice@example.com",
            "service": "Plumbing",
            "area": "Tampa",
        })
        self.lb.note(r1["lead"]["id"], "urgent leak")

        self.clock.advance(60)
        r2 = self.lb.upsert({
            "source": "google_lsa",
            "name": "Bob",
            "phone": "8135550501",
            "service": "Roofing",
        })

        self.clock.advance(60)
        r3 = self.lb.upsert({
            "source": "website",
            "name": "Carol",
            "phone": "8135550502",
            "service": "Electrical",
        })

        result = self.lb.find(text="alice")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["leads"][0]["id"], r1["lead"]["id"])

        result = self.lb.find(text="URGENT")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["leads"][0]["id"], r1["lead"]["id"])

        result = self.lb.find(stage="new", source="website")
        self.assertEqual(result["total"], 2)

        result = self.lb.find(limit=1)
        self.assertEqual(len(result["leads"]), 1)
        self.assertEqual(result["total"], 3)

        result = self.lb.find()
        self.assertEqual(result["leads"][0]["id"], r3["lead"]["id"])

    def test_export_csv_injection_escape_and_header(self):
        self.lb.upsert({
            "source": "website",
            "name": "=SUM(A1)",
            "phone": "8135550600",
            "service": "+plumbing",
        })

        text = self.lb.export_csv()
        header = next(csv.reader(io.StringIO(text)))
        for field in [
            "id", "created_at", "name", "phone", "email", "source",
            "service", "area", "stage", "value", "won_value", "won_at",
            "next_follow_up",
        ]:
            self.assertIn(field, header)

        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["name"].startswith("'="))
        self.assertTrue(rows[0]["service"].startswith("'+"))

    @unittest.skipIf(os.name == "nt", "chmod semantics differ on Windows")
    def test_atomic_save_backup_and_chmod_600(self):
        self.lb.upsert({
            "source": "website",
            "name": "A",
            "phone": "8135550700",
        })
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

        with open(self.path, "r", encoding="utf-8") as f:
            first = f.read()

        self.lb.upsert({
            "source": "website",
            "name": "B",
            "phone": "8135550701",
        })

        backup_path = self.path + ".bak"
        self.assertTrue(os.path.exists(backup_path))
        with open(backup_path, "r", encoding="utf-8") as f:
            backup = f.read()
        self.assertEqual(backup, first)

        with open(self.path, "r", encoding="utf-8") as f:
            second = f.read()
        self.assertNotEqual(first, second)
        json.loads(second)

    def test_import_rows_mapping_skip_and_update(self):
        rows = [
            {
                "Phone": "(813) 555-0800",
                "Name": "Alice",
                "Email": "ALICE@EXAMPLE.COM",
                "SourceId": "a1",
            },
            {"Phone": "", "Name": "", "Email": ""},
            {
                "Name": "Bob",
                "Phone": "813.555.0801",
                "SourceId": "b1",
            },
        ]
        mapping = {
            "Phone": "phone",
            "Name": "name",
            "Email": "email",
            "SourceId": "source_id",
        }

        result = self.lb.import_rows(rows, source="website", mapping=mapping)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn(result["skipped"][0]["row"], (1, 2))
        self.assertIn("why", result["skipped"][0])

        result = self.lb.import_rows(
            [{"Phone": "813.555.0800", "Name": "Alice Smith"}],
            source="google_lsa",
            mapping={"Phone": "phone", "Name": "name"},
        )
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.lb.find()["total"], 2)

    def test_conversions_filtering_and_format(self):
        g1 = self.lb.upsert({
            "source": "google_ads",
            "name": "G1",
            "phone": "8135550900",
            "gclid": "g1",
            "value": 100,
        })
        self.lb.move(g1["lead"]["id"], "won", value=250)

        self.clock.advance(10 * 24 * 60 * 60)
        g2 = self.lb.upsert({
            "source": "google_ads",
            "name": "G2",
            "phone": "8135550901",
            "gclid": "g2",
            "value": 50,
        })
        self.lb.move(g2["lead"]["id"], "won", value=75)

        g3 = self.lb.upsert({
            "source": "google_ads",
            "name": "G3",
            "phone": "8135550902",
            "value": 10,
        })
        self.lb.move(g3["lead"]["id"], "won", value=15)

        self.lb.upsert({
            "source": "google_ads",
            "name": "G4",
            "phone": "8135550903",
            "gclid": "g4",
        })

        result = self.lb.conversions("2024-01-15", "2024-01-16")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["gclid"], "g1")
        self.assertEqual(result[0]["value"], 250)
        self.assertEqual(
            result[0]["conversion_date_time"],
            "2024-01-15 12:00:00+00:00",
        )

    def test_move_records_event_in_file(self):
        result = self.lb.upsert({
            "source": "website",
            "name": "Alice",
            "phone": "8135551000",
        })
        lead_id = result["lead"]["id"]
        self.lb.move(lead_id, "contacted")

        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertTrue(any(
            event.get("lead_id") == lead_id
            and event.get("from") == "new"
            and event.get("to") == "contacted"
            for event in data["events"]
        ))


if __name__ == "__main__":
    unittest.main()
