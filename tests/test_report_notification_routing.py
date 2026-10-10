"""Notification scope is explicit and old cached reports cannot bypass it."""
from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import test_inspection_ack  # noqa: F401 - initializes the isolated plugin package.
from ack_test.daily_inspection import (
    DailyInspectionRunner,
    REPORT_NOTIFICATION_POLICY,
    fingerprint,
    query_reports,
    report_key,
)
from ack_test.inspection_ack import AckInspectionRunner, acknowledge


class ReportQueryTests(unittest.TestCase):
    def query(self, response):
        calls = []

        def call(endpoint, timeout, name, arguments):
            calls.append((name, arguments))
            if name == "get_current_user":
                return json.dumps({"username": "test-user", "organization_role": "company_leader"})
            if name != "list_daily_reports":
                raise AssertionError("must reject unsupported report scope before querying other sources")
            return json.dumps(response(arguments) if callable(response) else response)

        with ExitStack() as stack:
            stack.enter_context(patch("ack_test.work_todos.profile_scope"))
            for name, value in {
                "_call_mcp": call, "DEFAULT_ENDPOINT": "test", "ENDPOINT_ENV": "INSPECTION_TEST_ENDPOINT",
                "_validate_endpoint": lambda value: value,
            }.items():
                stack.enter_context(patch("ack_test." + name, value, create=True))
            result = query_reports(Path("/tmp"), {"expected_username": "test-user"}, "2026-10-09")
        return result, calls

    def test_explicit_notification_scope_is_required(self):
        result, calls = self.query({"count": 0, "reports": [], "notification_policy": REPORT_NOTIFICATION_POLICY})
        self.assertEqual(result, [])
        self.assertEqual(calls[1:], [("list_daily_reports", {
            "start_date": day, "end_date": day, "limit": 100, "notification_only": True,
        }) for day in ("2026-10-09", "2026-10-08", "2026-10-07")])

    def test_lookback_day_without_notification_policy_rejects_whole_snapshot(self):
        def response(arguments):
            result = {"count": 0, "reports": []}
            if arguments['start_date'] == '2026-10-09':
                result['notification_policy'] = REPORT_NOTIFICATION_POLICY
            return result
        with self.assertRaisesRegex(ValueError, 'policy'):
            self.query(response)

    def test_old_backend_or_wrong_policy_never_falls_back_to_readable_reports(self):
        for extra in ({}, {"notification_policy": "old-policy"}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "policy"):
                self.query({"count": 0, "reports": [], **extra})


class CachedNotificationScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = datetime.fromisoformat("2026-10-09T09:00:00+08:00").timestamp()
        self.config = {"enabled": True, "interval_minutes": 30,
                       "acknowledgement": {"enabled": True, "reminder_minutes": 2, "max_reminders_per_day": None}}
        (self.home / "inspection.json").write_text(json.dumps(self.config))
        self.report = dict(report_type="personal", employee_username="other-department", report_date="2026-10-09",
                           department_code="other", content="OLD REPORT", version=1)
        self.task = dict(item_type="task", task_no="T1", title="本人任务", status="open", content="TASK", version=1)
        self.review = dict(item_type="review", document_id="D1", content="文件：REVIEW", version=1)
        self.rows = [self.report, self.task, self.review]
        self.sent, self.queries = [], 0
        self.runner = AckInspectionRunner(self.home, self.query, lambda *args: "summary", self.send, lambda: self.now)

    def query(self, *args):
        self.queries += 1
        return self.rows

    def send(self, home, config, text, key):
        self.sent.append(text)
        return {"test": "sent"}

    def old_policy(self, *, retry_at=0):
        with sqlite3.connect(self.home / "inspection/ack.sqlite3") as db:
            db.execute("DELETE FROM meta WHERE key='report_notification_policy'")
            db.execute("UPDATE meta SET value=? WHERE key='query_retry_at'", (str(retry_at),))

    def test_upgrade_refreshes_before_due_and_preserves_task_review_receipts(self):
        self.runner.run(self.config)
        acknowledge(self.home, "已读", "R3", "test", lambda: self.now)
        self.old_policy()
        self.rows = [self.task, self.review]
        self.now += 120  # The next report query is still 28 minutes away.
        self.runner = AckInspectionRunner(self.home, self.query, lambda *args: "summary", self.send, lambda: self.now)
        result = self.runner.run(self.config)
        self.assertTrue(result["queried"])
        self.assertEqual(self.queries, 2)
        self.assertNotIn("OLD REPORT", self.sent[-1])
        self.assertIn("[R2]", self.sent[-1])
        self.assertNotIn("[R3]", self.sent[-1])
        with sqlite3.connect(self.home / "inspection/ack.sqlite3") as db:
            self.assertEqual(db.execute("SELECT id,state FROM notices ORDER BY id").fetchall(),
                             [(1, "superseded"), (2, "unread"), (3, "acked")])
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='report_notification_policy'").fetchone()[0],
                             REPORT_NOTIFICATION_POLICY)
        self.now += 60
        self.runner.run(self.config)
        self.assertEqual(self.queries, 2, "the migration refresh must not change the normal query cadence")

    def test_upgrade_query_failure_and_backoff_cannot_send_old_cache(self):
        self.runner.run(self.config)
        self.old_policy()
        self.now += 120

        def failed(*args):
            raise RuntimeError("unavailable")

        self.runner.query = failed
        self.assertEqual(self.runner.run(self.config)["status"], "query_failed")
        self.assertEqual(self.runner.run(self.config)["status"], "query_retry_wait")
        self.assertEqual(len(self.sent), 1)
        with sqlite3.connect(self.home / "inspection/ack.sqlite3") as db:
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='report_notification_policy'").fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM notices WHERE state='unread'").fetchone()[0], 3)
        self.now += 60
        self.runner.query = self.query
        self.rows = []
        self.runner.run(self.config)
        self.assertEqual(len(self.sent), 1)

    def test_existing_backoff_is_respected_during_upgrade_without_sending(self):
        self.runner.run(self.config)
        self.old_policy(retry_at=self.now + 600)
        self.now += 120
        self.assertEqual(self.runner.run(self.config)["status"], "query_retry_wait")
        self.assertEqual(self.queries, 1)
        self.assertEqual(len(self.sent), 1)

    def test_all_legacy_pending_batches_are_reconciled_before_selecting_next(self):
        self.rows = []
        legacy = DailyInspectionRunner(self.home, self.query,
            lambda home, items: "\n".join(item["report"]["content"] for item in items), self.send, lambda: self.now)
        legacy.run(self.config)

        def item(row):
            return {"key": report_key(row), "fingerprint": fingerprint(row), "report": row}

        with sqlite3.connect(self.home / "inspection/state.sqlite3") as db:
            for key, rows in (("first", [self.report]), ("second", [self.report, self.task])):
                db.execute("INSERT INTO batches VALUES (?,?,?,'pending',0,0)",
                           (key, json.dumps([item(row) for row in rows]), "OLD REPORT"))
        self.rows = [self.task]
        self.now += 60
        self.assertEqual(legacy.run(self.config)["status"], "sent")
        self.assertEqual(self.sent, ["TASK"])
        with sqlite3.connect(self.home / "inspection/state.sqlite3") as db:
            self.assertEqual(db.execute("SELECT id,state FROM batches ORDER BY rowid").fetchall(),
                             [("first", "cancelled"), ("second", "sent")])
