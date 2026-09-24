from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("profile_inspection", Path(__file__).parents[1] / "profile_inspection.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "employee-test"
        self.home.mkdir()
        self.now = 0.0
        self.queries = 0
        self.models = 0
        self.sent = []
        self.data = {"tasks": [{"task_no": "T-1", "status": "open", "version": 1}], "reviews": []}

        def query(_home, _config):
            self.queries += 1
            return self.data

        def model(_home, items):
            self.models += 1
            return f"changed: {len(items)}"

        self.runner = module.InspectionRunner(query, model, lambda _home, text: self.sent.append(text), lambda: self.now)
        self.config = module.InspectionConfig()

    def tearDown(self):
        self.tmp.cleanup()

    def at(self, value):
        from datetime import datetime
        self.now = datetime.fromisoformat(value).timestamp()

    def test_work_hours_and_interval_30(self):
        self.at("2026-09-24T09:00:00+08:00")
        self.assertTrue(self.config.in_work_hours(self.now))
        self.assertEqual(self.runner.run(self.home, self.config), "sent")
        self.assertEqual(self.runner.run(self.home, self.config), "not_due")
        self.now += 30 * 60
        self.assertEqual(self.runner.run(self.home, self.config), "unchanged")
        self.at("2026-09-24T18:00:00+08:00")
        self.assertFalse(self.config.in_work_hours(self.now))
        self.assertEqual(self.runner.run(self.home, self.config), "not_due")

    def test_interval_60(self):
        config = module.InspectionConfig(interval_minutes=60)
        self.at("2026-09-24T09:00:00+08:00")
        self.assertEqual(self.runner.run(self.home, config), "sent")
        self.now += 30 * 60
        self.assertEqual(self.runner.run(self.home, config), "not_due")
        self.now += 30 * 60
        self.assertEqual(self.runner.run(self.home, config), "unchanged")

    def test_new_item_and_status_change_notify_once(self):
        self.at("2026-09-24T09:00:00+08:00")
        self.runner.run(self.home, self.config)
        self.now += 1800
        self.data["tasks"][0]["status"] = "in_progress"
        self.assertEqual(self.runner.run(self.home, self.config), "sent")
        self.now += 1800
        self.assertEqual(self.runner.run(self.home, self.config), "unchanged")
        self.assertEqual(self.models, 2)

    def test_review_id_is_deduplicated_independently(self):
        self.data = {"tasks": [], "reviews": [{"document_id": "D-1", "status": "pending_review", "version": 1}]}
        self.at("2026-09-24T09:00:00+08:00")
        self.assertEqual(self.runner.run(self.home, self.config), "sent")
        self.now += 1800
        self.assertEqual(self.runner.run(self.home, self.config), "unchanged")

    def test_quiet_time_defers_then_sends(self):
        config = module.InspectionConfig(quiet_start="12:00", quiet_end="13:00")
        self.at("2026-09-24T12:00:00+08:00")
        self.assertEqual(self.runner.run(self.home, config), "quiet_deferred")
        self.assertEqual(self.models, 0)
        self.at("2026-09-24T13:00:00+08:00")
        self.assertEqual(self.runner.run(self.home, config), "sent")

    def test_restart_preserves_deduplication(self):
        self.at("2026-09-24T09:00:00+08:00")
        self.assertEqual(self.runner.run(self.home, self.config), "sent")
        runner = module.InspectionRunner(self.runner.query, self.runner.model, self.runner.send, lambda: self.now)
        self.now += 1800
        self.assertEqual(runner.run(self.home, self.config), "unchanged")

    def test_failed_notification_retries(self):
        self.at("2026-09-24T09:00:00+08:00")
        failing = module.InspectionRunner(self.runner.query, lambda *_: (_ for _ in ()).throw(RuntimeError()), self.runner.send, lambda: self.now)
        self.assertEqual(failing.run(self.home, self.config), "retry_pending")
        self.now += 31
        self.assertEqual(self.runner.run(self.home, self.config), "sent")

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            module.InspectionConfig(interval_minutes=15)


if __name__ == "__main__":
    unittest.main()
