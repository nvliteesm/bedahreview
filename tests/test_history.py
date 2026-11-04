"""Riwayat (history): the table, the page and account deletion agree, and no column can hold review text."""
import json
import re
import unittest
from pathlib import Path

from bedahreview.account import TABLES, AccountAdmin
from bedahreview.events import EVENTS

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
PAGE = (ROOT / "src" / "bedahreview" / "static" / "index.html").read_text(encoding="utf-8")
USER = "11111111-2222-3333-4444-555555555555"


def history_body():
    return re.search(r"create table if not exists public\.history \((.*?)\n\);", SQL, re.S).group(1)


class HistoryTableTests(unittest.TestCase):
    def test_the_table_and_the_page_agree_on_the_columns(self):
        columns = re.findall(r"^\s+(\w+)\s+(?:uuid|text|timestamptz|bigint|integer)\b", history_body(), re.M)
        self.assertEqual(columns, ["id", "user_id", "created_at", "product", "reviews", "fix_n", "fight_n", "ignore_n", "keep_n", "top_theme"])
        page = json.loads(re.search(r"HISTORY_COLS = (\[[^\]]*\])", PAGE).group(1))
        self.assertEqual(page, [c for c in columns if c not in ("id", "created_at")])  # id and the date are filled in by the database

    def test_the_top_complaint_can_only_be_one_of_the_fixed_theme_names(self):
        allowed = re.findall(r"'([^']+)'", re.search(r"top_theme\s+text check \(top_theme in \((.*?)\)\)", history_body(), re.S).group(1))
        model = json.loads((ROOT / "prompts" / "output_schema.json").read_text(encoding="utf-8"))["properties"]["theme"]["enum"]
        self.assertEqual(sorted(allowed), sorted(model))
        page_names = re.findall(r'"([^"]+)":', re.search(r"const THEME_ID = \{(.*?)\n\};", PAGE, re.S).group(1))
        self.assertTrue(set(page_names) <= set(allowed), set(page_names) - set(allowed))  # the page only ever sends a name the table accepts

    def test_each_seller_reads_adds_and_deletes_only_their_own_rows(self):
        for action in ("select", "insert", "delete"):
            self.assertRegex(SQL, r"create policy history_%s_own on public\.history for %s to authenticated" % (action, action))
        self.assertNotIn("history_update", SQL)  # a summary is never edited
        granted = re.search(r"grant ([a-z, ]+?)\s+on public\.history to authenticated", SQL).group(1)
        self.assertEqual([g.strip() for g in granted.split(",")], ["select", "insert", "delete"])
        self.assertIn("alter table public.history  enable row level security", SQL)
        self.assertRegex(SQL, r"create index if not exists \w+ on public\.history \(user_id, created_at desc\)")

    def test_60_days_is_the_same_in_the_page_and_the_optional_clean_up(self):
        self.assertIn("HISTORY_DAYS = 60", PAGE)
        self.assertIn("interval '60 days'", SQL)

    def test_deleting_an_account_also_deletes_its_history(self):
        self.assertIn("history", TABLES)
        self.assertRegex(history_body(), r"user_id\s+uuid[^\n]*references auth\.users \(id\) on delete cascade")
        seen = []
        AccountAdmin("https://example.supabase.co", "sb_secret_test", request=lambda m, url, h, b: seen.append((m, url)) or 200).delete_account(USER)
        self.assertIn(("DELETE", "https://example.supabase.co/rest/v1/history?user_id=eq." + USER), seen)
        self.assertEqual(seen[-1][1], "https://example.supabase.co/auth/v1/admin/users/" + USER)  # the rows go first, the account last

    def test_opening_history_is_an_event_that_carries_nothing(self):
        self.assertEqual(EVENTS["history_opened"], {})


if __name__ == "__main__":
    unittest.main()
