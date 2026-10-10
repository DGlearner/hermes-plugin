"""Review-result transport checks; all sends are local mocks."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_inspection_ack
from ack_test import daily_inspection as inspection
from ack_test import review_result_delivery as results
from ack_test.notification_errors import NotificationRejected


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'profiles/employee-test'
        self.home.mkdir(parents=True)
        self.cfg = {'notification_targets': [
            {'platform': 'wecom_callback', 'chat_id': 'corp:test'},
            {'platform': 'telegram', 'chat_id': '123'}]}

    def send(self, **kwargs):
        return inspection.send_notification(self.home, self.cfg,
            inspection.ItemNotification(['第一页', '第二页']), 'review-result-test', **kwargs)

    def test_rejected_part_retries_without_replaying_successful_parts(self):
        def deliver(home, target, message, batch):
            if target['platform'] == 'wecom_callback' and message == '第二页':
                raise NotificationRejected('api_rejected')
        with patch.object(inspection, '_send_notification_target', side_effect=deliver) as send:
            with self.assertRaises(inspection.UncertainDelivery) as error:
                self.send(retry_rejected=True)
            self.assertEqual(error.exception.outcomes, {'wecom_callback': 'rejected', 'telegram': 'sent'})
            self.assertEqual(send.call_count, 4)
        with patch.object(inspection, '_send_notification_target') as send:
            self.assertEqual(self.send(retry_rejected=True), {'wecom_callback': 'sent', 'telegram': 'sent'})
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], '第二页')
            self.send(retry_rejected=True)
            send.assert_called_once()

    def test_uncertainty_dominates_rejection_and_is_never_replayed(self):
        def deliver(home, target, message, batch):
            if target['platform'] == 'wecom_callback':
                if message == '第一页':
                    raise NotificationRejected('api_rejected')
                raise TimeoutError()
        with patch.object(inspection, '_send_notification_target', side_effect=deliver):
            with self.assertRaises(inspection.UncertainDelivery) as error:
                self.send(retry_rejected=True)
            self.assertEqual(error.exception.outcomes['wecom_callback'], 'uncertain')
        with patch.object(inspection, '_send_notification_target') as send:
            with self.assertRaises(inspection.UncertainDelivery) as error:
                self.send(retry_rejected=True)
            self.assertEqual(error.exception.outcomes['wecom_callback'], 'uncertain')
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], '第一页')

    def test_existing_report_delivery_does_not_gain_retry_behavior(self):
        with patch.object(inspection, '_send_notification_target', side_effect=NotificationRejected('no')) as send:
            for _ in range(2):
                with self.assertRaises(inspection.UncertainDelivery) as error:
                    self.send()
                self.assertEqual(set(error.exception.outcomes.values()), {'uncertain'})
            self.assertEqual(send.call_count, 4)

    def test_inflight_parts_are_not_reclaimed(self):
        with patch.object(inspection, '_send_notification_target'):
            self.send(retry_rejected=True)
        with sqlite3.connect(self.home / 'inspection/deliveries.sqlite3') as db:
            db.execute("UPDATE deliveries SET state='sending' WHERE platform='wecom_callback'")
        with patch.object(inspection, '_send_notification_target') as send:
            with self.assertRaises(inspection.UncertainDelivery):
                self.send(retry_rejected=True)
            send.assert_not_called()

    def test_main_keeps_result_notification_independent_of_report_failure(self):
        (self.home / 'inspection.json').write_text(json.dumps(self.cfg))
        with patch.object(inspection.DailyInspectionRunner, 'run', side_effect=RuntimeError()), \
                patch.object(results, 'run_review_result_notifications', return_value={'status': 'acked'}) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = inspection.main(self.home)
        self.assertEqual(result, {'status': 'failed', 'review_results': {'status': 'acked'}})
        run.assert_called_once_with(self.home, self.cfg)
        self.assertEqual(json.loads(output.getvalue()), {'wakeAgent': False})

    def test_review_failure_preserves_existing_report_result(self):
        (self.home / 'inspection.json').write_text(json.dumps(self.cfg))
        with patch.object(inspection.DailyInspectionRunner, 'run', return_value={'status': 'sent', 'changes': 2}), \
                patch.object(results, 'run_review_result_notifications', side_effect=RuntimeError()), \
                contextlib.redirect_stdout(io.StringIO()):
            result = inspection.main(self.home)
        self.assertEqual(result, {'status': 'sent', 'changes': 2, 'review_results': {'status': 'failed'}})


@unittest.skipUnless(Path('/opt/hermes/gateway/config.py').exists(), 'requires Hermes runtime')
class NativeTransportTests(unittest.TestCase):
    def test_model_handler_cannot_ack_delivery(self):
        import plugins.profile_rag_mcp as plugin
        with patch.object(plugin, '_call_mcp') as call:
            response = json.loads(plugin._make_handler('unused', 30,
                'acknowledge_knowledge_review_notification')({'notification_id': 'unused'}))
        self.assertIn('native_delivery_required', json.dumps(response))
        call.assert_not_called()

    def test_wecom_definite_rejection_and_uncertain_response_are_distinct(self):
        import httpx
        from gateway.config import Platform
        from ack_test import wecom_binding as wecom
        from ack_test import wecom_egress as egress
        adapter = SimpleNamespace(_apps=[{'corp_id': 'corp', 'agent_id': 1}],
                                  _get_access_token=AsyncMock(return_value='test-token'))
        config = SimpleNamespace(platforms={Platform.WECOM_CALLBACK: SimpleNamespace(enabled=True)})
        cases = [
            (httpx.Response(200, json={'errcode': 0}), None),
            (httpx.Response(200, json={'errcode': 40014}), NotificationRejected),
            (httpx.Response(200, json={'errcode': 0, 'invaliduser': 'test'}), NotificationRejected),
            (httpx.ConnectError('connect'), NotificationRejected),
            (httpx.ReadTimeout('response lost'), RuntimeError),
            (httpx.WriteTimeout('partial write'), RuntimeError),
            (httpx.Response(503, json={'errcode': 1}), RuntimeError),
            (httpx.Response(200, json={}), RuntimeError),
            (httpx.Response(200, text='broken'), RuntimeError),
        ]
        for outcome, expected in cases:
            with self.subTest(outcome=type(outcome).__name__, expected=expected):
                post = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
                with patch('ack_test.work_todos.profile_scope', side_effect=lambda root: contextlib.nullcontext()), \
                        patch('gateway.config.load_gateway_config', return_value=config), \
                        patch('plugins.platforms.wecom.callback_adapter.WecomCallbackAdapter', return_value=adapter), \
                        patch.object(egress, 'relay_settings', return_value=('http://unused', 'test-auth')), \
                        patch.object(egress, 'RelayHTTPClient', return_value=SimpleNamespace(post=post)):
                    if expected is None:
                        wecom.send_notification(Path('/tmp'), {'chat_id': 'corp:test'}, '通知')
                    else:
                        with self.assertRaises(expected) as caught:
                            wecom.send_notification(Path('/tmp'), {'chat_id': 'corp:test'}, '通知')
                        self.assertIs(type(caught.exception), expected)
                post.assert_awaited_once()

    def test_token_failure_is_rejected_before_message_attempt(self):
        from gateway.config import Platform
        from ack_test import wecom_binding as wecom
        from ack_test import wecom_egress as egress
        post = AsyncMock()
        adapter = SimpleNamespace(_apps=[{'corp_id': 'corp', 'agent_id': 1}],
                                  _get_access_token=AsyncMock(side_effect=TimeoutError()))
        config = SimpleNamespace(platforms={Platform.WECOM_CALLBACK: SimpleNamespace(enabled=True)})
        with patch('ack_test.work_todos.profile_scope', side_effect=lambda root: contextlib.nullcontext()), \
                patch('gateway.config.load_gateway_config', return_value=config), \
                patch('plugins.platforms.wecom.callback_adapter.WecomCallbackAdapter', return_value=adapter), \
                patch.object(egress, 'relay_settings', return_value=('http://unused', 'test-auth')), \
                patch.object(egress, 'RelayHTTPClient', return_value=SimpleNamespace(post=post)):
            with self.assertRaises(NotificationRejected):
                wecom.send_notification(Path('/tmp'), {'chat_id': 'corp:test'}, '通知')
        post.assert_not_awaited()
