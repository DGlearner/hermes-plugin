"""Conversation confirmation uses native hooks, never model-provided claims."""
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import report_workflow as workflow
import profile_skills


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "profiles/employee-a"
        self.home.mkdir(parents=True)
        workflow._state()[1].clear()
        self.args = dict(draft_id="draft-1", expected_version=2,
                         confirmation_token="server-token", content_digest="server-digest",
                         idempotency_key="attempt-1")
        self.validated = dict(passed=True,
                              draft=dict(draft_id="draft-1", version=2, content="完整日报\n全部八部分"),
                              confirmation_token="server-token", content_digest="server-digest")
        self.calls = []

    def pre(self, text="整理日报", turn="turn-1", session="session-a", platform="wecom_callback", **kw):
        workflow.pre_turn(lambda: self.home, session_id=session, turn_id=turn,
                          platform=platform, sender_id="employee-a", user_message=text, **kw)

    def validate(self, result=None, session="session-a"):
        return workflow.call_tool("validate_daily_report_draft",
            {"draft_id": "draft-1", "expected_version": 2}, session_id=session,
            resolve_home=lambda: self.home, call=lambda: json.dumps(self.validated if result is None else result))

    def preview(self, content=None, turn="turn-1", session="session-a", platform="wecom_callback"):
        workflow.post_turn(lambda: self.home, session_id=session, turn_id=turn,
                           platform=platform, assistant_response=content or "完整日报\n全部八部分\n请回复确认提交日报")

    def publish(self, args=None, session="session-a", **kw):
        def call():
            self.calls.append(dict(args or self.args))
            return '{"report": {"version": 1}}'
        return json.loads(workflow.call_tool("publish_daily_report_draft", args or self.args,
            session_id=session, resolve_home=lambda: self.home, call=call))

    def test_later_real_confirmation_publishes_and_uncertain_retry_reuses_key(self):
        for platform in ("wecom_callback", "weixin", "telegram"):
            workflow._state()[1].clear()
            self.pre(platform=platform)
            self.validate()
            self.preview(platform=platform)
            self.pre("确认提交日报", turn="turn-2", platform=platform)
            self.assertIn("report", self.publish())
            self.assertIn("report", self.publish())
            self.assertIn("error", self.publish(dict(self.args, idempotency_key="another-attempt")))
        self.assertEqual(len(self.calls), 6)

    def test_model_cannot_confirm_same_turn_or_spoof_arguments(self):
        self.pre("确认提交日报")
        self.validate()
        self.preview()
        self.assertIn("error", self.publish(dict(self.args, confirmed=True)))
        self.assertFalse(self.calls)

    def test_requires_complete_preview_and_confirmation_phrase(self):
        for output in ("日报已合格，请回复确认提交日报", "完整日报\n全部八部分"):
            workflow._state()[1].clear()
            self.pre()
            self.validate()
            self.preview(output)
            self.pre("确认提交日报", turn="turn-2")
            self.assertIn("error", self.publish())
        self.assertFalse(self.calls)

    def test_confirmation_must_be_exact_real_text(self):
        for text in ("好", "确认提交", "请把下一步改成确认提交日报", "不要确认提交日报", "已读"):
            workflow._state()[1].clear()
            self.pre()
            self.validate()
            self.preview()
            self.pre(text, turn="turn-2")
            self.assertIn("error", self.publish())
        self.assertFalse(self.calls)

    def test_new_draft_or_validation_invalidates_previous_confirmation(self):
        for tool in ("prepare_daily_report_draft", "validate_daily_report_draft"):
            workflow._state()[1].clear()
            self.pre()
            self.validate()
            self.preview()
            self.pre("确认提交日报", turn="turn-2")
            workflow.call_tool(tool, {}, session_id="session-a", resolve_home=lambda: self.home,
                               call=lambda: '{"error":"failed"}')
            self.assertIn("error", self.publish())
        self.assertFalse(self.calls)

    def test_mismatched_passed_content_or_scope_is_not_accepted(self):
        self.pre()
        self.validate()
        self.preview()
        self.pre("确认提交日报", turn="turn-2")
        for field, value in (("draft_id", "other"), ("expected_version", 3),
                             ("confirmation_token", "forged"), ("content_digest", "forged")):
            self.assertIn("error", self.publish(dict(self.args, **{field: value})))
        self.assertIn("error", self.publish(session="session-other"))
        self.home = self.home.parent / "employee-b"
        self.assertIn("error", self.publish())
        self.assertFalse(self.calls)

    def test_restart_subagent_and_cron_fail_closed(self):
        self.pre()
        self.validate()
        self.preview()
        workflow._state()[1].clear()
        self.pre("确认提交日报", turn="turn-2")
        self.assertIn("error", self.publish())
        for kw in ({"parent_session_id": "parent"}, {"platform": "cron"}):
            workflow._state()[1].clear()
            self.pre(**kw)
            self.assertIn("error", json.loads(self.validate()))
        self.assertFalse(self.calls)

    def test_old_write_tools_never_reach_server(self):
        call = Mock()
        for tool in ("submit_daily_report", "update_daily_report", "save_daily_report_draft"):
            result = workflow.call_tool(tool, {}, session_id="session-a", resolve_home=lambda: self.home, call=call)
            self.assertIn("error", json.loads(result))
        call.assert_not_called()

    def test_failed_or_mismatched_validation_cannot_be_armed(self):
        for result in ({"passed": False}, dict(self.validated, passed="true"),
                       dict(self.validated, draft=dict(draft_id="other", version=2, content="完整日报"))):
            workflow._state()[1].clear()
            self.pre()
            self.validate(result)
            self.preview()
            self.pre("确认提交日报", turn="turn-2")
            self.assertIn("error", self.publish())
        self.assertFalse(self.calls)


class SkillInstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def employee(self, name):
        home = self.root / "profiles" / name
        home.mkdir(parents=True)
        (home / ".rag-mcp-employee.json").write_text(json.dumps({"profile_name": name, "username": name}))
        return home

    def test_existing_profiles_and_template_install_without_other_state_copy(self):
        a = self.employee("employee-a")
        b = self.employee("employee-b")
        template = self.root / "profiles/employee-template"
        template.mkdir()
        unmanaged = self.root / "profiles/employee-unmanaged"
        unmanaged.mkdir()
        secret = a / ".env"
        secret.write_text("untouched secret")
        self.assertEqual(profile_skills.sync_report_skills(self.root), 3)
        self.assertEqual(profile_skills.sync_report_skills(self.root), 0)
        for home in (a, b, template):
            self.assertTrue((home / "skills/profile-daily-report/SKILL.md").is_file())
        self.assertFalse((unmanaged / "skills").exists())
        self.assertFalse((b / ".env").exists())
        self.assertEqual(secret.read_text(), "untouched secret")

    def test_destination_symlinks_are_not_followed(self):
        home = self.employee("employee-a")
        outside = self.root / "outside"
        outside.mkdir()
        (home / "skills").symlink_to(outside)
        with self.assertRaises(ValueError):
            profile_skills.install_report_skill(home)
        self.assertEqual(list(outside.iterdir()), [])

    def test_new_profile_discovery_installs_skill_and_preserves_isolation(self):
        active = [self.root]
        constants = ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: active[0]
        native_skills = ModuleType("tools.skills_tool")
        native_skills._skills_dir = lambda: active[0] / "skills"
        native_manager = ModuleType("tools.skill_manager_tool")
        native_manager._find_skill_in_other_profiles = lambda name: ["foreign"]
        tools = ModuleType("tools")
        tools.skill_manager_tool = native_manager
        tools.skills_tool = native_skills
        with patch.dict(sys.modules, {"hermes_constants": constants, "tools": tools}):
            profile_skills.install_profile_skills()
            active[0] = self.employee("employee-new")
            directory = native_skills._skills_dir()
            self.assertTrue((directory / "profile-daily-report/SKILL.md").is_file())
            self.assertEqual(native_manager._find_skill_in_other_profiles("anything"), [])
            first_wrapper = native_skills._skills_dir
            profile_skills.install_profile_skills()
            self.assertIs(first_wrapper, native_skills._skills_dir)
            active[0] = self.root
            self.assertEqual(native_manager._find_skill_in_other_profiles("anything"), ["foreign"])


if __name__ == "__main__":
    unittest.main()
