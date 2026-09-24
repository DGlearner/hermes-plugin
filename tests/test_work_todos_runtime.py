"""Run these tests inside the pinned Hermes runtime; no real messages are sent."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if Path("/opt/hermes/hermes_constants.py").exists():
    sys.path.insert(0, "/opt/hermes")
    from agent.secret_scope import get_secret
    from gateway.config import Platform
    from gateway.platforms import weixin
    from hermes_constants import get_hermes_home
    from plugins.profile_rag_mcp import work_todos
else:
    work_todos = None


@unittest.skipIf(work_todos is None, "requires pinned Hermes runtime")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.home = self.root / "profiles/employee-test"
        self.home.mkdir(parents=True)
        (self.home / ".env").write_text("MCP_COMPANY_MCP_API_KEY=employee-test-only-secret\n")
        (self.root / ".env").write_text("MCP_COMPANY_MCP_API_KEY=root-test-only-secret\n")
        self.platform = SimpleNamespace(enabled=True, token="bot-test-only-secret", extra={"account_id": "bot-test"})
        self.binding = {"binding_id": "test-binding", "employee_id": "test-employee", "bound_at": "original"}
        self.reminder = {"profile": "employee-test", "peer": "peer-test", "id": "stable-test-id",
                         "binding_identity": work_todos.binding_identity(self.binding)}
        weixin.ContextTokenStore(str(self.root)).set("bot-test", "peer-test", "context-test-only-secret")

    def test_profile_secret_and_home_scopes_are_restored_after_failure(self):
        original = get_hermes_home()
        with work_todos.profile_scope(self.root):
            self.assertEqual(get_secret("MCP_COMPANY_MCP_API_KEY"), "root-test-only-secret")
            with self.assertRaises(ValueError):
                with work_todos.profile_scope(self.home):
                    self.assertEqual(get_secret("MCP_COMPANY_MCP_API_KEY"), "employee-test-only-secret")
                    self.assertEqual(Path(get_hermes_home()), self.home)
                    raise ValueError("test scope restoration")
            self.assertEqual(get_secret("MCP_COMPANY_MCP_API_KEY"), "root-test-only-secret")
            self.assertEqual(Path(get_hermes_home()), self.root)
        self.assertEqual(get_hermes_home(), original)

    def test_cron_child_does_not_inherit_default_bot_or_missing_employee_pat(self):
        from agent.secret_scope import is_multiplex_active, set_multiplex_active
        original = is_multiplex_active()
        self.addCleanup(set_multiplex_active, original)
        set_multiplex_active(False)  # Fresh cron subprocess, unlike the Gateway.
        (self.home / '.env').write_text('')
        (self.root / '.env').write_text('WEIXIN_TOKEN=root-bot\nWEIXIN_ACCOUNT_ID=root-account\n')
        inherited = {'WEIXIN_TOKEN': 'root-bot', 'WEIXIN_ACCOUNT_ID': 'root-account',
                     'MCP_COMPANY_MCP_API_KEY': 'root-pat'}
        with patch.dict(os.environ, inherited):
            with work_todos.profile_scope(self.home):
                self.assertFalse(get_secret('WEIXIN_TOKEN'))
                self.assertFalse(get_secret('MCP_COMPANY_MCP_API_KEY'))
                from gateway.config import load_gateway_config
                own = load_gateway_config().platforms.get(Platform.WEIXIN)
                self.assertFalse(own and own.enabled and own.token)
            with work_todos.profile_scope(self.root):
                self.assertEqual(get_secret('WEIXIN_TOKEN'), 'root-bot')
            self.assertEqual(os.environ['WEIXIN_TOKEN'], 'root-bot')
            self.assertFalse(is_multiplex_active())

    def test_cron_child_shared_transport_restores_default_context_from_root(self):
        from agent.secret_scope import is_multiplex_active, set_multiplex_active
        original = is_multiplex_active()
        self.addCleanup(set_multiplex_active, original)
        set_multiplex_active(False)
        (self.root / '.env').write_text('WEIXIN_TOKEN=bot-test-only-secret\nWEIXIN_ACCOUNT_ID=bot-test\n')
        (self.root / 'config.yaml').write_text(
            'profile_routes:\n  - profile: employee-test\n    chat_id: peer-test\n    platform: weixin\n')
        with patch.dict(os.environ, {'WEIXIN_TOKEN': 'bot-test-only-secret', 'WEIXIN_ACCOUNT_ID': 'bot-test'}):
            home, platform = work_todos.transport(self.root, 'employee-test', 'peer-test')
            self.assertEqual(home, self.root)
            store = weixin.ContextTokenStore(str(home))
            store.restore(platform.extra['account_id'])
            self.assertEqual(store.get('bot-test', 'peer-test'), 'context-test-only-secret')
            with self.assertRaises(work_todos.DeliveryError):
                work_todos.transport(self.root, 'employee-test', 'another-peer')

    def send_response(self, response=None, error=None):
        calls = []
        async def send(session, **kwargs):
            calls.append(kwargs)
            if error:
                raise error
            return response
        with patch.object(work_todos, "transport", return_value=(self.root, self.platform)), \
                patch.object(work_todos, "active_binding", return_value=self.binding), \
                patch.object(weixin, "_send_message", side_effect=send):
            try:
                work_todos.send_report(self.root, self.reminder, "test report")
            except work_todos.DeliveryError as exc:
                return calls, exc
        return calls, None

    def test_native_sender_keeps_actual_error_and_uses_latest_context(self):
        calls, error = self.send_response({"ret": -2, "errmsg": "prepare failed"})
        self.assertEqual(error.category, "send_rejected")
        self.assertEqual(error.details["ret"], -2)
        self.assertEqual(error.details["errmsg"], "prepare failed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["context_token"], "context-test-only-secret")
        self.assertIsNone(calls[1]["context_token"])
        self.assertTrue(error.details["recovery_attempted"])
        self.assertEqual(calls[0]["to"], "peer-test")
        self.assertEqual(calls[0]["client_id"], "hermes-todos-stable-test-id")

    def test_rejected_old_context_can_recover_without_resending_a_success(self):
        calls = []
        async def send(session, **kwargs):
            calls.append(kwargs)
            return {"ret": -2, "errmsg": "prepare failed"} if len(calls) == 1 else {"message_id": 123}
        with patch.object(work_todos, "transport", return_value=(self.root, self.platform)), \
                patch.object(work_todos, "active_binding", return_value=self.binding), \
                patch.object(weixin, "_send_message", side_effect=send):
            work_todos.send_report(self.root, self.reminder, "report")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[1]["context_token"])
        self.assertEqual(calls[0]["client_id"], calls[1]["client_id"])
        # Recovery never deletes the saved token or fabricates a refreshed session.
        store = weixin.ContextTokenStore(str(self.root))
        store.restore("bot-test")
        self.assertEqual(store.get("bot-test", "peer-test"), "context-test-only-secret")
        queue = work_todos.ReminderQueue(self.root)
        with queue.connect() as db:
            attempts = [json.loads(row[0]) for row in db.execute("SELECT diagnostics FROM delivery_attempts ORDER BY id")]
        self.assertEqual([row["api_accepted"] for row in attempts], [False, True])
        self.assertEqual([row["mode"] for row in attempts], ["stored_context", "without_context"])
        self.assertNotIn("context-test-only-secret", json.dumps(attempts))

    def test_recovery_prefers_new_context_received_during_rejected_send(self):
        calls = []
        async def send(session, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                weixin.ContextTokenStore(str(self.root)).set("bot-test", "peer-test", "new-context-secret")
                return {"ret": -2, "errmsg": "prepare failed"}
            return {"message_id": 456}
        with patch.object(work_todos, "transport", return_value=(self.root, self.platform)), \
                patch.object(work_todos, "active_binding", return_value=self.binding), \
                patch.object(weixin, "_send_message", side_effect=send):
            work_todos.send_report(self.root, self.reminder, "report")
        self.assertEqual(calls[1]["context_token"], "new-context-secret")

    def test_rebind_during_failure_prevents_recovery_send(self):
        calls = []
        async def send(session, **kwargs):
            calls.append(kwargs)
            self.binding["bound_at"] = "rebound"
            return {"ret": -2, "errmsg": "prepare failed"}
        with patch.object(work_todos, "transport", return_value=(self.root, self.platform)), \
                patch.object(work_todos, "active_binding", side_effect=lambda *_: self.binding), \
                patch.object(weixin, "_send_message", side_effect=send):
            with self.assertRaises(work_todos.DeliveryError) as result:
                work_todos.send_report(self.root, self.reminder, "report")
        self.assertEqual(result.exception.category, "binding_changed")
        self.assertEqual(len(calls), 1)

    def test_absent_context_cannot_start_a_tokenless_retry_loop(self):
        with patch.object(weixin.ContextTokenStore, "get", return_value=None):
            calls, error = self.send_response({"ret": -2, "errmsg": "prepare failed"})
        self.assertEqual(len(calls), 1)
        self.assertFalse(error.details["recovery_attempted"])

    def test_genuine_frequency_limit_does_not_trigger_context_recovery(self):
        calls, error = self.send_response({"ret": -2, "errmsg": "freq limit"})
        self.assertEqual(error.category, "rate_limited")
        self.assertEqual(len(calls), 1)

    def test_recovery_timeout_is_uncertain_and_stops(self):
        calls = []
        async def send(session, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {"ret": -2, "errmsg": "prepare failed"}
            raise asyncio.TimeoutError()
        with patch.object(work_todos, "transport", return_value=(self.root, self.platform)), \
                patch.object(work_todos, "active_binding", return_value=self.binding), \
                patch.object(weixin, "_send_message", side_effect=send):
            with self.assertRaises(work_todos.DeliveryError) as result:
                work_todos.send_report(self.root, self.reminder, "report")
        self.assertEqual(result.exception.category, "delivery_uncertain")
        self.assertEqual(len(calls), 2)

    def test_attempt_diagnostics_capture_inbound_age_without_credentials(self):
        queue = work_todos.ReminderQueue(self.root)
        queue.note_interaction("employee-test", "peer-test")
        self.send_response({"message_id": 789})
        with queue.connect() as db:
            row = json.loads(db.execute("SELECT diagnostics FROM delivery_attempts").fetchone()[0])
        self.assertGreaterEqual(row["last_observed_inbound_age_seconds"], 0)
        self.assertTrue(row["message_id_present"])
        self.assertNotIn("bot-test-only-secret", json.dumps(row))

    def test_native_sender_accepts_success_only_on_acknowledgement(self):
        for response in ({"ret": 0}, {"errcode": 0}, {}):
            with self.subTest(response=response):
                calls, error = self.send_response(response)
                self.assertIsNone(error)
                self.assertEqual(len(calls), 1)

    def test_native_sender_marks_timeouts_uncertain_without_resending(self):
        calls, error = self.send_response(error=asyncio.TimeoutError())
        self.assertEqual(error.category, "delivery_uncertain")
        self.assertEqual(len(calls), 1)

    def test_http_rate_limit_and_authentication_failure_are_distinct(self):
        for code, category in ((429, "rate_limited"), (401, "channel_auth_failed"), (503, "delivery_uncertain")):
            with self.subTest(code=code):
                calls, error = self.send_response(error=RuntimeError(f"iLink POST sendmessage HTTP {code}: private-body"))
                self.assertEqual(error.category, category)
                self.assertNotIn("private-body", str(error.details))
                self.assertEqual(len(calls), 1)

    def test_shared_transport_requires_an_exact_employee_route(self):
        own = SimpleNamespace(platforms={Platform.WEIXIN: SimpleNamespace(enabled=False)})
        route = SimpleNamespace(profile="employee-test", chat_id="peer-test", platform="weixin")
        shared = SimpleNamespace(platforms={Platform.WEIXIN: self.platform}, profile_routes=[route])
        def config():
            return own if Path(get_hermes_home()) == self.home else shared
        with patch("gateway.config.load_gateway_config", side_effect=config):
            self.assertEqual(work_todos.transport(self.root, "employee-test", "peer-test")[0], self.root)
            with self.assertRaises(work_todos.DeliveryError):
                work_todos.transport(self.root, "employee-test", "other-peer")

    def test_independent_transport_uses_employee_home_for_context_tokens(self):
        own = SimpleNamespace(platforms={Platform.WEIXIN: self.platform})
        with patch("gateway.config.load_gateway_config", return_value=own):
            self.assertEqual(work_todos.transport(self.root, "employee-test", "peer-test")[0], self.home)

    def test_persisted_context_change_recovers_after_restart_without_inbound_hook(self):
        calls, queries = [], []
        async def send(session, **kwargs):
            calls.append(kwargs)
            return {'ret': -2, 'errmsg': 'prepare failed'} if len(calls) <= 2 else {'message_id': 123}
        def queue():
            return work_todos.ReminderQueue(
                self.root, binding=lambda *_: self.binding,
                query=lambda *_: queries.append('queried') or 'current report')
        with patch.object(work_todos, 'transport', return_value=(self.root, self.platform)), \
                patch.object(work_todos, 'active_binding', return_value=self.binding), \
                patch.object(weixin, '_send_message', side_effect=send):
            first = queue()
            key = first.enqueue('employee-test', 'peer-test', 'employee', 'daily')
            self.assertEqual(first.process(key), 'waiting_interaction')
            # Only update the durable token, as if shutdown interrupted the hook.
            path = weixin.ContextTokenStore(str(self.root))._path('bot-test')
            path.write_text(json.dumps({'peer-test': 'fresh-context-secret'}))
            restarted = queue()
            restarted.tick()
            self.assertEqual(restarted.get(key)['state'], 'sent')
            self.assertEqual(calls[-1]['context_token'], 'fresh-context-secret')
            self.assertEqual(len(queries), 2)
            restarted.tick()
            self.assertEqual(len(calls), 3)
            with restarted.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM interactions').fetchone()[0], 0)
            self.assertNotIn(b'fresh-context-secret', restarted.path.read_bytes())

    def test_unchanged_or_other_peer_context_does_not_replay_rejected_delivery(self):
        async def reject(*args, **kwargs):
            return {'ret': -2, 'errmsg': 'prepare failed'}
        with patch.object(work_todos, 'transport', return_value=(self.root, self.platform)), \
                patch.object(work_todos, 'active_binding', return_value=self.binding), \
                patch.object(weixin, '_send_message', side_effect=reject) as sender:
            queue = work_todos.ReminderQueue(self.root, binding=lambda *_: self.binding, query=lambda *_: 'report')
            key = queue.enqueue('employee-test', 'peer-test', 'employee', 'daily')
            queue.process(key)
            path = weixin.ContextTokenStore(str(self.root))._path('bot-test')
            path.write_text(json.dumps({'peer-test': 'context-test-only-secret', 'someone-else': 'new-secret'}))
            queue.tick()
            self.assertEqual(queue.get(key)['state'], 'waiting_interaction')
            self.assertEqual(sender.call_count, 2)

    def test_legacy_rejection_establishes_baseline_without_sending(self):
        queue = work_todos.ReminderQueue(self.root, binding=lambda *_: self.binding,
                                       query=lambda *_: self.fail('legacy baseline must not send'))
        key = queue.enqueue('employee-test', 'peer-test', 'employee', 'legacy')
        with queue.connect() as db:
            db.execute("UPDATE reminders SET state='waiting_interaction',error=? WHERE id=?",
                       (json.dumps({'category': 'send_rejected'}), key))
        with patch.object(work_todos, 'transport', return_value=(self.root, self.platform)):
            queue.tick()
            queue.tick()
        self.assertEqual(queue.get(key)['state'], 'waiting_interaction')
        self.assertEqual(queue.get(key)['attempts'], 0)
        self.assertTrue(json.loads(queue.get(key)['error'])['context_revision'])

    def test_new_context_does_not_replay_uncertain_or_expired_delivery(self):
        queue = work_todos.ReminderQueue(self.root, binding=lambda *_: self.binding,
                                       query=lambda *_: self.fail('must not query'))
        for state, expired in [('delivery_uncertain', False), ('waiting_interaction', True)]:
            with self.subTest(state=state):
                key = queue.enqueue('employee-test', 'peer-test', 'employee', state)
                with queue.connect() as db:
                    db.execute('UPDATE reminders SET state=?,error=?,expires_at=? WHERE id=?',
                               (state, json.dumps({'context_revision': 'previous'}),
                                0 if expired else 9999999999, key))
                with patch.object(work_todos, 'transport') as resolver:
                    queue.process(key)
                    resolver.assert_not_called()
                self.assertEqual(queue.get(key)['state'], 'expired' if expired else state)


if __name__ == "__main__":
    unittest.main()
