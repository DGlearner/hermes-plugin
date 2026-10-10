"""Completed review notices never require a model or an employee read receipt."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import fcntl
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_inspection_ack  # noqa: F401 - initializes the isolated plugin package.
from ack_test import work_todos  # noqa: F401 - import before temporary sys.modules patches.
from ack_test.review_result_delivery import (
    ReviewResultNotificationRunner,
    acknowledge_review_notification,
    active_review_bindings,
    query_review_notifications,
    render_review_notification,
    run_review_result_notifications,
)


def notification(**changes):
    result = dict(notification_id=str(uuid4()), document_id=str(uuid4()), document_version=2,
                knowledge_id="K000123", title="团队操作手册", file_name="操作手册.pdf",
                decision="rejected", reason="请补充第二章的操作步骤。", reviewer_username="reviewer",
                reviewer_display_name="审核员", reviewed_at="2026-10-09T01:00:00+00:00",
                created_at="2026-10-09T01:00:00+00:00")
    result.update(changes)
    return result


def page(rows, cursor=None, more=False):
    return {"count": len(rows), "notifications": rows, "next_cursor": cursor, "has_more": more}


class MCPContractTests(unittest.TestCase):
    def mcp(self, pages, *, username="ordinary-employee", acknowledgement=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        calls = []
        responses = iter(pages)

        def call(endpoint, timeout, name, arguments):
            calls.append((name, arguments))
            if name == "get_current_user":
                result = {"username": username, "organization_role": "employee", "system_roles": []}
            elif name == "list_my_knowledge_review_notifications":
                result = next(responses)
            elif name == "acknowledge_knowledge_review_notification":
                result = acknowledgement
            else:
                raise AssertionError(name)
            if isinstance(result, Exception):
                raise result
            return json.dumps(result)

        stack.enter_context(patch("ack_test.work_todos.profile_scope"))
        for name, value in {"_call_mcp": call, "DEFAULT_ENDPOINT": "test",
                            "ENDPOINT_ENV": "REVIEW_NOTIFICATION_TEST_ENDPOINT",
                            "_validate_endpoint": lambda value: value}.items():
            stack.enter_context(patch("ack_test." + name, value, create=True))
        return calls

    def test_ordinary_employee_gets_every_page_starting_at_none(self):
        first, second = notification(), notification()
        calls = self.mcp([page([first], "cursor-one", True), page([second])])
        result = query_review_notifications(Path("/tmp"), {"expected_username": "ordinary-employee", "include_reviews": False})
        self.assertEqual(result, [first, second])
        self.assertEqual(calls, [
            ("get_current_user", {}),
            ("list_my_knowledge_review_notifications", {"limit": 50, "cursor": None}),
            ("list_my_knowledge_review_notifications", {"limit": 50, "cursor": "cursor-one"}),
        ])

    def test_identity_mismatch_stops_both_query_and_ack(self):
        calls = self.mcp([], username="someone-else")
        config = {"expected_username": "ordinary-employee"}
        with self.assertRaisesRegex(ValueError, "identity"):
            query_review_notifications(Path("/tmp"), config)
        with self.assertRaisesRegex(ValueError, "identity"):
            acknowledge_review_notification(Path("/tmp"), config, str(uuid4()))
        self.assertEqual(calls, [("get_current_user", {}), ("get_current_user", {})])

    def test_ack_uses_profile_identity_and_exact_notification_id(self):
        identity = str(uuid4())
        reply = {"notification_id": identity, "delivered_at": "2026-10-09T01:00:00Z", "already_delivered": True}
        calls = self.mcp([], acknowledgement=reply)
        self.assertEqual(acknowledge_review_notification(Path("/tmp"),
                         {"expected_username": "ordinary-employee"}, identity), reply)
        self.assertEqual(calls, [("get_current_user", {}),
            ("acknowledge_knowledge_review_notification", {"notification_id": identity})])

    def test_invalid_pagination_is_rejected_without_partial_snapshot(self):
        item = notification()
        invalid_item = dict(item)
        del invalid_item["reason"]
        cases = [
            [dict(page([item]), count=2)],
            [dict(page([item]), has_more="false")],
            [page([item], "unexpected", False)],
            [page([], "cursor", True)],
            [page([item], None, True)],
            [page([invalid_item])],
            [page([item], "cursor", True), page([item])],
            [page([item], "cursor", True), page([notification()], "cursor", True)],
            [page([item], "cursor", True), RuntimeError("unavailable")],
            [page([dict(item, notification_id="not-a-uuid")])],
            [page([dict(item, reviewed_at="2026-10-09T09:00:00")])],
        ]
        for pages in cases:
            with self.subTest(pages=pages):
                self.mcp(pages)
                with self.assertRaises((ValueError, RuntimeError)):
                    query_review_notifications(Path("/tmp"), {"expected_username": "ordinary-employee"})


class ReviewDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "profiles" / "employee-test"
        self.home.mkdir(parents=True)
        self.now = datetime.fromisoformat("2026-10-09T09:00:00+08:00").timestamp()
        self.config = {"enabled": True, "interval_minutes": 2, "expected_username": "ordinary-employee",
                       "include_reviews": False,
                       "notification_targets": [{"platform": "wecom_callback", "chat_id": "corp:member"}]}
        self.rows = [notification()]
        self.sent, self.acks, self.queries = [], [], 0
        self.generation = "bound-v1"
        self.runner = self.make_runner()

    @property
    def database(self):
        return self.home / "inspection/review-results/state.sqlite3"

    def make_runner(self, **overrides):
        ports = dict(query=self.query, ack=self.ack, send=self.send,
                     clock=lambda: self.now, bindings=self.bindings)
        ports.update(overrides)
        return ReviewResultNotificationRunner(self.home, **ports)

    def bindings(self, home, config):
        return {"targets": deepcopy(config["notification_targets"]), "generation": self.generation}

    def query(self, *args):
        self.queries += 1
        return deepcopy(self.rows)

    def send(self, home, config, message, batch_id, *, retry_rejected=False):
        self.assertTrue(retry_rejected)
        self.sent.append((tuple(message.parts), batch_id, deepcopy(config["notification_targets"])))
        return {target["platform"]: "sent" for target in config["notification_targets"]}

    def ack(self, home, config, identity):
        self.acks.append(identity)
        return {"notification_id": identity, "delivered_at": "2026-10-09T01:00:00Z", "already_delivered": False}

    def tick(self, seconds=0):
        self.now += seconds
        return self.runner.run(self.config)

    def state(self):
        with sqlite3.connect(self.database) as db:
            return db.execute("SELECT state,attempts,last_error FROM notifications").fetchall()

    def test_default_enabled_for_ordinary_employee_without_reviewer_feed(self):
        result = self.tick()
        self.assertEqual(result["status"], "acked")
        self.assertEqual(self.acks, [self.rows[0]["notification_id"]])
        self.assertEqual(self.state(), [("acked", 1, None)])
        text = "\n".join(self.sent[0][0])
        for expected in ("文件：操作手册.pdf", "版本：2", "审核驳回", "审核员（reviewer）",
                         "2026-10-09 09:00:00（上海时间）", self.rows[0]["reason"]):
            self.assertIn(expected, text)
        self.assertNotIn("已读", text)
        self.assertEqual(self.sent[0][1], "knowledge-review-" + self.rows[0]["notification_id"])
        self.tick(120)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.acks), 1)

    def test_explicit_optout_and_disabled_do_no_query_or_send(self):
        self.config["include_review_results"] = False
        self.assertEqual(self.tick()["status"], "disabled")
        self.config["include_review_results"] = True
        self.config["enabled"] = False
        self.assertEqual(self.tick()["status"], "disabled")
        self.assertEqual((self.queries, self.sent, self.acks), (0, [], []))

    def test_schedule_interval_quiet_and_work_hours(self):
        self.tick()
        self.assertEqual(self.tick(60)["status"], "not_due")
        self.tick(60)
        self.assertEqual(self.queries, 2)
        self.config["quiet_hours"] = {"enabled": True, "start": "09:00", "end": "10:00"}
        self.assertEqual(self.tick(120)["status"], "quiet_deferred")
        self.assertEqual(self.queries, 2)
        self.now = datetime.fromisoformat("2026-10-09T18:00:00+08:00").timestamp()
        self.assertEqual(self.tick()["status"], "outside_work_hours")

    def test_no_binding_or_revoked_binding_cannot_query_send_or_ack(self):
        self.config["notification_targets"] = []
        self.assertEqual(self.tick()["status"], "no_binding")
        self.config["notification_targets"] = [{"platform": "wecom_callback", "chat_id": "corp:member"}]
        self.runner.bindings = lambda *args: (_ for _ in ()).throw(ValueError("binding changed"))
        self.assertEqual(self.tick()["status"], "binding_changed")
        self.assertEqual((self.queries, self.sent, self.acks), (0, [], []))

    def test_failed_or_partial_query_sends_nothing(self):
        self.runner.query = lambda *args: (_ for _ in ()).throw(RuntimeError("other service unavailable"))
        self.assertEqual(self.tick()["status"], "query_failed")
        self.runner.query = self.query
        self.rows.append({"notification_id": str(uuid4())})
        self.assertEqual(self.tick(120)["status"], "query_failed")
        self.assertEqual(self.state(), [])
        self.assertEqual((self.sent, self.acks), ([], []))

    def test_duplicate_snapshot_is_rejected_before_any_send(self):
        self.rows.append(deepcopy(self.rows[0]))
        self.assertEqual(self.tick()["status"], "query_failed")
        self.assertEqual(self.sent, [])

    def test_long_reason_is_complete_unicode_safe_and_within_each_channel_budget(self):
        self.rows[0]["reason"] = ("第一段：需要完整保留。🙂\n\n第二段：不能裁剪。\n" * 700) + "最后一句。"
        for targets, limit in ((self.config["notification_targets"], 4000),
                               ([{"platform": "weixin", "chat_id": "wx"}], 1900),
                               (self.config["notification_targets"] + [{"platform": "weixin", "chat_id": "wx"}], 1900)):
            with self.subTest(limit=limit):
                parts = render_review_notification(self.rows[0], targets)
                self.assertGreater(len(parts), 1)
                self.assertTrue(all(len(part.encode("utf-8")) <= limit for part in parts))
                body = "".join(part.split("\n", 2)[2] for part in parts)
                self.assertEqual(body.split("审核原因：\n", 1)[1], self.rows[0]["reason"])

    def test_ack_failure_restart_only_retries_ack_even_if_no_longer_pending(self):
        def failed_ack(*args):
            self.acks.append(args[-1])
            raise TimeoutError("ACK response lost")
        self.runner.ack = failed_ack
        self.assertEqual(self.tick()["status"], "ack_failed")
        self.assertEqual(self.state(), [("sent", 1, "ack_failed")])
        original = deepcopy(self.sent)
        self.rows = []  # The server may have committed before its ACK response was lost.
        self.runner = self.make_runner()
        with patch("ack_test.review_result_delivery.render_review_notification", side_effect=AssertionError("must not re-render")):
            self.assertEqual(self.tick(120)["status"], "acked")
        self.assertEqual(self.sent, original)
        self.assertEqual(len(self.acks), 2)
        self.assertEqual(self.state(), [("acked", 1, None)])

    def test_binding_change_after_sent_never_resends_or_acknowledges_new_target(self):
        self.runner.ack = lambda *args: (_ for _ in ()).throw(TimeoutError())
        self.tick()
        self.runner = self.make_runner()
        self.config["notification_targets"][0]["chat_id"] = "corp:new-member"
        self.assertEqual(self.tick(120)["status"], "binding_changed")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.acks, [])
        self.config["notification_targets"][0]["chat_id"] = "corp:member"
        self.generation = "bound-v2"
        self.assertEqual(self.tick(120)["status"], "binding_changed")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.acks, [])

    def test_binding_revoked_during_send_keeps_sent_but_does_not_ack(self):
        def revoked(*args, **kwargs):
            outcomes = self.send(*args, **kwargs)
            self.generation = "revoked"
            return outcomes
        self.runner.send = revoked
        self.assertEqual(self.tick()["status"], "binding_changed")
        self.assertEqual(self.state(), [("sent", 1, None)])
        self.assertEqual(self.acks, [])

    def test_partial_uncertain_delivery_never_acknowledges_or_replays(self):
        self.config["notification_targets"].append({"platform": "telegram", "chat_id": "123"})
        def partial(*args, **kwargs):
            self.send(*args, **kwargs)
            error = TimeoutError()
            error.outcomes = {"wecom_callback": "sent", "telegram": "uncertain"}
            raise error
        self.runner.send = partial
        self.assertEqual(self.tick()["status"], "unconfirmed")
        self.runner = self.make_runner()
        self.assertEqual(self.tick(120)["status"], "unconfirmed")
        self.assertEqual((len(self.sent), self.acks), (1, []))

    def test_missing_platform_or_empty_success_is_unconfirmed(self):
        self.runner.send = lambda *args, **kwargs: {}
        self.assertEqual(self.tick()["status"], "unconfirmed")
        self.assertEqual(self.acks, [])

    def test_explicit_rejection_reuses_persisted_batch_and_parts_next_cycle(self):
        def rejected(*args, **kwargs):
            self.send(*args, **kwargs)
            error = RuntimeError("upstream rejected request")
            error.outcomes = {"wecom_callback": "rejected"}
            raise error
        self.runner.send = rejected
        self.assertEqual(self.tick()["status"], "rejected")
        original = deepcopy(self.sent[0])
        self.runner = self.make_runner()
        self.assertEqual(self.tick(60)["status"], "not_due")
        with patch("ack_test.review_result_delivery.render_review_notification", side_effect=AssertionError("must not re-render")):
            self.assertEqual(self.tick(60)["status"], "acked")
        self.assertEqual(self.sent, [original, original])
        self.assertEqual(self.state(), [("acked", 2, None)])

    def test_send_crash_is_recovered_as_unconfirmed(self):
        self.runner.ack = lambda *args: (_ for _ in ()).throw(TimeoutError())
        self.tick()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE notifications SET state='sending'")
        self.runner = self.make_runner()
        self.assertEqual(self.tick(120)["status"], "unconfirmed")
        self.assertEqual(self.state(), [("unconfirmed", 1, "interrupted_send")])
        self.assertEqual((len(self.sent), self.acks), (1, []))

    def test_existing_payload_mismatch_blocks_whole_round(self):
        self.tick()
        self.rows[0]["reason"] = "unexpected mutation"
        self.rows.append(notification())
        self.assertEqual(self.tick(120)["status"], "query_failed")
        self.assertEqual(len(self.sent), 1)

    def test_invalid_ack_response_retains_sent_and_does_not_resend(self):
        self.runner.ack = lambda *args: {"notification_id": str(uuid4()), "already_delivered": False,
                                       "delivered_at": "2026-10-09T01:00:00Z"}
        self.assertEqual(self.tick()["status"], "ack_failed")
        self.runner = self.make_runner()
        self.assertEqual(self.tick(120)["status"], "acked")
        self.assertEqual(len(self.sent), 1)

    def test_lock_and_safe_last_result(self):
        directory = self.home / "inspection/review-results"
        directory.mkdir(parents=True)
        with (directory / "run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.tick(), {"status": "busy"})
        self.tick()
        contents = (directory / "last_result.json").read_text()
        self.assertEqual(json.loads(contents)["states"], {"acked": 1})
        self.assertNotIn(self.rows[0]["reason"], contents)
        self.assertNotIn(self.rows[0]["file_name"], contents)
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)

    def test_public_entry_accepts_isolated_ports(self):
        result = run_review_result_notifications(self.home, self.config, query=self.query, ack=self.ack,
            send=self.send, clock=lambda: self.now, bindings=self.bindings)
        self.assertEqual(result["status"], "acked")


class ActiveBindingTests(unittest.TestCase):
    def test_enabled_direct_routes_must_match_every_target(self):
        import sys
        from types import ModuleType
        module = ModuleType("gateway.config")
        route = SimpleNamespace(profile="employee-test", platform="wecom_callback", chat_id="corp:member",
                                name="route-one", chat_type="dm", enabled=True)
        module.load_gateway_config = lambda: SimpleNamespace(profile_routes=[route])
        config = {"notification_targets": [{"platform": "wecom_callback", "chat_id": "corp:member"}]}
        with patch.dict(sys.modules, {"gateway.config": module}), patch("ack_test.work_todos.profile_scope"):
            self.assertEqual(active_review_bindings(Path("/tmp/profiles/employee-test"), config),
                             [{"platform": "wecom_callback", "chat_id": "corp:member", "route": "route-one"}])
            route.enabled = False
            with self.assertRaises(ValueError):
                active_review_bindings(Path("/tmp/profiles/employee-test"), config)
            route.enabled = True
            route.chat_type = "group"
            with self.assertRaises(ValueError):
                active_review_bindings(Path("/tmp/profiles/employee-test"), config)


if __name__ == "__main__":
    unittest.main()
