from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from pathlib import Path


spec = importlib.util.spec_from_file_location("work_todos_test_module", Path(__file__).parents[1] / "work_todos.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
DeliveryError, ReminderQueue = module.DeliveryError, module.ReminderQueue


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 1000.0
        self.binding = {"binding_id": "binding-1", "employee_id": "employee-1", "bound_at": "first-binding"}
        self.report = "current tasks"
        self.queries, self.sent = [], []
        self.send_errors = []
        self.queue = ReminderQueue(self.root, query=self.query, send=self.send,
                                   binding=lambda *_: self.binding, clock=lambda: self.now)

    def query(self, root, profile, username):
        self.queries.append((profile, username))
        return self.report

    def send(self, root, reminder, report):
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.sent.append((reminder["profile"], reminder["peer"], report))

    def enqueue(self, profile="employee-one", peer="peer-one", key="daily-20260918"):
        return self.queue.enqueue(profile, peer, "employee", key)

    def test_success_is_deduplicated_across_queue_restarts(self):
        key = self.enqueue()
        self.assertEqual(self.queue.process(key), "sent")
        second = ReminderQueue(self.root, query=self.query, send=self.send,
                               binding=lambda *_: self.binding, clock=lambda: self.now)
        self.assertEqual(second.enqueue("employee-one", "peer-one", "employee", "daily-20260918"), key)
        self.assertEqual(second.process(key), "sent")
        self.assertEqual(len(self.sent), 1)

    def test_rejected_send_waits_for_own_interaction_and_requeries(self):
        self.send_errors = [DeliveryError("send_rejected", ret=-2, errmsg="prepare failed")]
        key = self.enqueue()
        self.assertEqual(self.queue.process(key), "waiting_interaction")
        self.now += 60
        self.queue.note_interaction("employee-two", "peer-one")
        self.queue.note_interaction("employee-one", "peer-two")
        self.queue.tick()
        self.assertFalse(self.sent)
        self.assertEqual(len(self.queries), 1)
        self.report = "refreshed tasks"
        self.queue.note_interaction("employee-one", "peer-one")
        self.queue.tick()
        self.assertEqual(self.sent, [("employee-one", "peer-one", "refreshed tasks")])
        self.assertEqual(len(self.queries), 2)

    def test_unbound_or_rebound_identity_cancels_waiting_delivery(self):
        self.send_errors = [DeliveryError("context_rejected")]
        key = self.enqueue()
        self.queue.process(key)
        self.binding = {**self.binding, "bound_at": "new-binding"}
        self.queue.note_interaction("employee-one", "peer-one")
        self.queue.tick()
        self.assertEqual(self.queue.get(key)["state"], "cancelled")
        self.assertFalse(self.sent)

    def test_binding_is_checked_again_after_query(self):
        def query(*_):
            self.binding = {**self.binding, "binding_id": "replaced"}
            return "report"
        self.queue.query = query
        key = self.enqueue()
        self.assertEqual(self.queue.process(key), "cancelled")
        self.assertFalse(self.sent)

    def test_rate_limit_retries_are_delayed_and_bounded(self):
        self.send_errors = [DeliveryError("rate_limited")] * 4
        key = self.enqueue()
        for index, delay in enumerate(module.RETRY_DELAYS):
            self.assertEqual(self.queue.process(key), "pending")
            self.assertEqual(self.queue.get(key)["attempts"], index + 1)
            self.queue.process(key)
            self.assertEqual(self.queue.get(key)["attempts"], index + 1)
            self.now += delay
        self.assertEqual(self.queue.process(key), "failed")
        self.queue.note_interaction("employee-one", "peer-one")
        self.queue.tick()
        self.assertEqual(self.queue.get(key)["attempts"], 4)

    def test_interaction_during_send_failure_is_not_lost(self):
        def send(*_):
            self.queue.note_interaction("employee-one", "peer-one")
            raise DeliveryError("send_rejected")
        self.queue.send = send
        key = self.enqueue()
        self.assertEqual(self.queue.process(key), "waiting_interaction")
        self.queue.send = self.send
        self.queue.tick()
        self.assertEqual(self.queue.get(key)["state"], "sent")

    def test_timeout_or_unknown_send_error_is_not_automatically_retried(self):
        for error in (DeliveryError("delivery_uncertain"), RuntimeError("unknown acknowledgement")):
            with self.subTest(error=type(error).__name__):
                self.send_errors = [error]
                key = self.enqueue(key=str(type(error).__name__))
                self.assertEqual(self.queue.process(key), "delivery_uncertain")
                self.queue.note_interaction("employee-one", "peer-one")
                self.queue.tick()
                self.assertEqual(self.queue.get(key)["attempts"], 1)
        self.assertFalse(self.sent)

    def test_expired_delivery_is_not_sent(self):
        key = self.enqueue()
        self.now += module.DELIVERY_TTL
        self.assertEqual(self.queue.process(key), "expired")
        self.assertFalse(self.queries)

    def test_interrupted_worker_does_not_resend_a_possibly_delivered_message(self):
        key = self.enqueue()
        self.queue._claim(key)
        self.now += module.LEASE_SECONDS
        self.queue.tick()
        self.assertEqual(self.queue.get(key)["state"], "delivery_uncertain")
        self.assertFalse(self.sent)

    def test_parallel_workers_claim_only_once(self):
        key = self.enqueue()
        entered, release = threading.Event(), threading.Event()
        def send(*args):
            entered.set()
            release.wait(5)
            self.send(*args)
        self.queue.send = send
        first = threading.Thread(target=self.queue.process, args=(key,))
        first.start()
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.queue.process(key), "sending")
        release.set()
        first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(len(self.sent), 1)

    def test_query_failure_can_retry_without_sending(self):
        self.queue.query = lambda *_: (_ for _ in ()).throw(RuntimeError("MCP unavailable"))
        key = self.enqueue()
        self.assertEqual(self.queue.process(key), "pending")
        self.assertFalse(self.sent)
        self.now += 30
        self.queue.query = self.query
        self.assertEqual(self.queue.process(key), "sent")

    def at(self, value):
        self.now = datetime.fromisoformat(value).timestamp()

    def test_first_chat_before_cron_creates_daily_and_cron_does_not_repeat(self):
        self.at('2026-09-21T08:00:00+08:00')
        self.queue.subscribe_daily('employee-one', 'peer-one', 'employee')
        self.queue.note_interaction('employee-one', 'peer-one')
        key = self.queue.on_daily_interaction('employee-one', 'peer-one')
        self.queue.tick()
        self.assertEqual(self.queue.get(key)['state'], 'sent')
        self.at('2026-09-21T09:00:00+08:00')
        self.assertEqual(self.queue.enqueue_daily('employee-one', 'peer-one', 'employee'), key)
        self.queue.process(key)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.queries), 1)

    def test_successful_cron_is_adopted_without_repeating_on_first_chat(self):
        self.at('2026-09-21T09:00:00+08:00')
        key = self.enqueue(key='work-todos:daily-09:00:2026-09-21')
        self.queue.process(key)
        self.queue.subscribe_daily('employee-one', 'peer-one', 'employee')
        self.at('2026-09-21T14:00:00+08:00')
        self.assertEqual(self.queue.on_daily_interaction('employee-one', 'peer-one'), key)
        self.queue.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.queue.get(key)['local_day'], '2026-09-21')

    def test_uncertain_legacy_send_is_not_replayed_by_first_chat(self):
        self.at('2026-09-21T09:00:00+08:00')
        self.send_errors = [DeliveryError('delivery_uncertain')]
        key = self.enqueue(key='work-todos:daily-09:00:2026-09-21')
        self.queue.process(key)
        self.at('2026-09-21T18:00:00+08:00')
        self.queue.subscribe_daily('employee-one', 'peer-one', 'employee')
        self.queue.note_interaction('employee-one', 'peer-one')
        self.assertEqual(self.queue.on_daily_interaction('employee-one', 'peer-one'), key)
        self.queue.tick()
        self.assertEqual(self.queue.get(key)['state'], 'delivery_uncertain')
        self.assertEqual(self.queue.get(key)['attempts'], 1)
        self.assertFalse(self.sent)

    def test_daily_records_are_isolated_by_employee(self):
        self.at('2026-09-21T09:00:00+08:00')
        first = self.queue.enqueue_daily('employee-one', 'peer-one', 'employee')
        self.queue.process(first)
        self.binding = {**self.binding, 'employee_id': 'employee-two', 'binding_id': 'binding-two'}
        second = self.queue.enqueue_daily('employee-two', 'peer-two', 'employee')
        self.queue.process(second)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.sent), 2)

    def test_expired_legacy_reminder_can_recover_on_afternoon_interaction(self):
        self.at('2026-09-21T09:00:00+08:00')
        self.send_errors = [DeliveryError('send_rejected')]
        key = self.enqueue(key='work-todos:daily-09:00:2026-09-21')
        self.queue.process(key)
        self.at('2026-09-21T15:00:00+08:00')
        self.queue.tick()
        self.assertEqual(self.queue.get(key)['state'], 'expired')
        self.queue.subscribe_daily('employee-one', 'peer-one', 'employee')
        self.queue.note_interaction('employee-one', 'peer-one')
        self.assertEqual(self.queue.on_daily_interaction('employee-one', 'peer-one'), key)
        self.report = 'fresh afternoon tasks'
        self.queue.tick()
        self.assertEqual(self.sent[0][2], 'fresh afternoon tasks')
        self.assertEqual(self.queue.get(key)['state'], 'sent')

    def test_new_daily_reminder_waits_until_end_of_day_and_never_sends_yesterday(self):
        self.at('2026-09-21T09:00:00+08:00')
        self.send_errors = [DeliveryError('send_rejected')]
        old = self.queue.enqueue_daily('employee-one', 'peer-one', 'employee')
        self.queue.process(old)
        self.at('2026-09-21T18:00:00+08:00')
        self.queue.tick()
        self.assertEqual(self.queue.get(old)['state'], 'waiting_interaction')
        self.at('2026-09-22T00:00:00+08:00')
        self.queue.note_interaction('employee-one', 'peer-one')
        self.queue.tick()
        self.assertEqual(self.queue.get(old)['state'], 'expired')
        self.assertFalse(self.sent)
        new = self.queue.enqueue_daily('employee-one', 'peer-one', 'employee', interaction=True)
        self.assertNotEqual(old, new)
        self.queue.process(new)
        self.assertEqual(len(self.sent), 1)

    def test_daily_subscription_and_sent_state_survive_restart(self):
        self.at('2026-09-21T10:00:00+08:00')
        self.queue.subscribe_daily('employee-one', 'peer-one', 'employee')
        key = self.queue.on_daily_interaction('employee-one', 'peer-one')
        self.queue.process(key)
        restarted = ReminderQueue(self.root, query=self.query, send=self.send,
                                  binding=lambda *_: self.binding, clock=lambda: self.now)
        self.assertEqual(restarted.on_daily_interaction('employee-one', 'peer-one'), key)
        restarted.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(restarted.on_daily_interaction('employee-other', 'peer-other'))

    def test_daily_concurrent_cron_and_messages_send_only_once(self):
        self.at('2026-09-21T09:00:00+08:00')
        errors, keys = [], []
        def run():
            try:
                key = self.queue.enqueue_daily('employee-one', 'peer-one', 'employee', interaction=True)
                keys.append(key)
                self.queue.process(key)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(5)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(errors)
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(len(self.sent), 1)

    def test_daily_rebind_does_not_repeat_sent_or_uncertain_occurrence(self):
        self.at('2026-09-21T09:00:00+08:00')
        for state in ('sent', 'sending', 'delivery_uncertain'):
            with self.subTest(state=state):
                key = self.queue.enqueue_daily('employee-one', 'peer-one', 'employee')
                with self.queue.connect() as db:
                    db.execute('UPDATE reminders SET state=?,lease_until=? WHERE id=?', (state, self.now+100, key))
                self.binding = {**self.binding, 'bound_at': state}
                self.assertEqual(self.queue.enqueue_daily('employee-one', 'new-peer', 'employee', interaction=True), key)
                self.assertEqual(self.queue.get(key)['state'], state)
        self.assertFalse(self.sent)

    def test_daily_subscription_rejects_changed_employee(self):
        self.queue.subscribe_daily('employee-one', 'peer-one', 'employee')
        self.binding = {**self.binding, 'employee_id': 'someone-else'}
        with self.assertRaises(DeliveryError):
            self.queue.on_daily_interaction('employee-one', 'peer-one')
        self.assertFalse(self.sent)

    def test_daily_does_not_send_if_query_runs_past_midnight(self):
        self.at('2026-09-21T23:59:59+08:00')
        key = self.queue.enqueue_daily('employee-one', 'peer-one', 'employee')
        def query(*_):
            self.now += 2
            return 'yesterday tasks'
        self.queue.query = query
        self.assertEqual(self.queue.process(key), 'expired')
        self.assertFalse(self.sent)

    def test_response_classification_and_secret_redaction(self):
        cases = [({"ret": 0}, None), ({"ret": -2, "errmsg": "prepare failed"}, "send_rejected"),
                 ({"ret": -2, "errmsg": "frequency limit"}, "rate_limited"),
                 ({"ret": -14}, "context_rejected"), ({"ret": -2, "errmsg": "unknown error"}, "context_rejected"),
                 ({}, None), ({"errcode": 0}, None), ({"error": "failed"}, "send_rejected"),
                 ({"success": False}, "send_rejected")]
        for response, expected in cases:
            error = module.response_error(response)
            self.assertEqual(error.category if error else None, expected)
        error = module.response_error({"ret": -2, "errmsg": "prepare failed secret-value context-value"}, ("secret-value", "context-value"))
        text = json.dumps(error.details)
        self.assertNotIn("secret-value", text)
        self.assertNotIn("context-value", text)

    def test_context_hook_matches_bot_peer_and_profile_and_installs_once(self):
        class Store:
            def __init__(self, home):
                self._root = home / "weixin/accounts"
            def set(self, account, peer, token):
                return "persisted"

        gateway = ModuleType("gateway")
        platforms = ModuleType("gateway.platforms")
        weixin = ModuleType("gateway.platforms.weixin")
        constants = ModuleType("hermes_constants")
        weixin.ContextTokenStore = Store
        constants.get_default_hermes_root = lambda: self.root
        gateway.platforms, platforms.weixin = platforms, weixin
        modules = {"gateway": gateway, "gateway.platforms": platforms,
                   "gateway.platforms.weixin": weixin, "hermes_constants": constants,
                   module.PROCESS_STATE: None}
        provisioner = self.root / "provisioner"
        provisioner.mkdir()
        (self.root / 'profiles/employee-one').mkdir(parents=True)
        (provisioner / "wechat_bindings.json").write_text(json.dumps({"bindings": {"one": {
            **self.binding, "status": "active", "profile_name": "employee-one", "external_user_id": "peer-one"}}}))
        self.queue.subscribe_daily('employee-one', 'peer-one', 'employee')
        with patch.dict(module.sys.modules, modules), patch.object(module, "Thread") as thread, \
                patch.object(module, "transport", return_value=(self.root, SimpleNamespace(extra={"account_id": "own-bot"}))):
            self.assertTrue(module.install_delivery_worker())
            self.assertFalse(module.install_delivery_worker())
            self.assertEqual(thread.call_count, 1)
            own = Store(self.root)
            self.assertEqual(own.set("other-bot", "peer-one", "context-secret"), "persisted")
            own.set("own-bot", "peer-two", "context-secret")
            Store(self.root / "other-profile").set("own-bot", "peer-one", "context-secret")
            with self.queue.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 0)
            own.set("own-bot", "peer-one", "context-secret")
            with self.queue.connect() as db:
                self.assertEqual(db.execute("SELECT version FROM interactions").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM reminders WHERE local_day IS NOT NULL").fetchone()[0], 1)
            own.set("own-bot", "peer-one", "context-secret")
            with self.queue.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)
            self.assertNotIn(b"context-secret", self.queue.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
