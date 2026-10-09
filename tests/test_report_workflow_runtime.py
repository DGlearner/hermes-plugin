"""Native Hermes registration/hook/Registry integration; no network or users."""
from contextlib import ExitStack
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


@unittest.skipUnless(Path("/opt/hermes/hermes_cli/plugins.py").is_file(), "requires native Hermes")
class NativeReportWorkflowTests(unittest.TestCase):
    def test_registered_native_hooks_and_dispatch_share_profile_and_session(self):
        sys.path.insert(0, "/opt/hermes")
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from tools.registry import registry
        import plugins.profile_rag_mcp as plugin
        from plugins.profile_rag_mcp import report_workflow

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            # Other runtime tests may leave model configuration wrappers
            # installed; use inert test values without enabling network I/O.
            stack.enter_context(patch.dict(os.environ, {
                "HERMES_SHARED_MODEL_API_KEY": "test-unused-model-key",
                "HERMES_DEEPSEEK_API_KEY": "test-unused-deepseek-key",
            }))
            home = Path(temporary) / "profiles/employee-runtime"
            home.mkdir(parents=True)
            (home / ".rag-mcp-employee.json").write_text(json.dumps({
                "profile_name": home.name, "username": "runtime",
            }))
            home_token = set_hermes_home_override(home)
            stack.callback(reset_hermes_home_override, home_token)
            report_workflow._state()[1].clear()
            manager = PluginManager(scope_key=str(home))
            manifest = PluginManifest(name="profile-rag-mcp", key="profile_rag_mcp", kind="backend",
                                      path=str(Path(plugin.__file__).parent))
            context = PluginContext(manifest, manager)
            # Isolate unrelated workers/platform patches. The actual report
            # registration, lifecycle dispatch, Profile and Registry stay real.
            for module_name, functions in {
                "inspection_ack": ["register_ack"],
                "gateway_quiet": ["install_gateway_quiet"],
                "weixin_delivery": ["install_weixin_delivery"],
                "work_todos": ["install_delivery_worker"],
                "telegram_binding": ["install_telegram_binding"],
                "wecom_binding": ["install_wecom_binding"],
                "employee_models": ["install_employee_models"],
                "weixin_media": ["install_weixin_document_validation", "install_weixin_receipt_diagnostics"],
            }.items():
                module = importlib.import_module("plugins.profile_rag_mcp." + module_name)
                for name in functions:
                    stack.enter_context(patch.object(module, name, return_value=False))
            for name in (
                "_configure_shared_model_secret", "_install_profile_transcript_scope",
                "_install_profile_hot_reload", "_install_weixin_provider_status_filter",
                "_install_attachment_capture", "_install_attachment_queue_policy",
                "_install_weixin_media_batching", "_install_weixin_clarify_reply_routing",
                "_start_attachment_maintenance_worker",
            ):
                stack.enter_context(patch.object(plugin, name, return_value=False))
            stack.enter_context(patch.object(plugin, "_configure_pairing_capacity", return_value=100))
            stack.enter_context(patch.object(plugin, "_load_tool_schemas", return_value=plugin._cached_tool_schemas()))
            plugin.register(context)
            self.assertEqual(plugin._current_profile_home(), home.resolve())
            self.assertTrue((home / "skills/profile-daily-report/SKILL.md").is_file())
            self.assertGreaterEqual(len(manager._hooks["pre_llm_call"]), 2)
            self.assertEqual(len(manager._hooks["post_llm_call"]), 1)
            prompt_section = manager._system_prompt_sections["profile_rag_mcp.employee_policy"]
            self.assertLessEqual(len(plugin._employee_policy_prompt({})), prompt_section.max_chars)

            server_calls = []
            report = "# 工作进展同步日报\n\n| 同步内容 | 内容 |\n|---|---|\n| 当前状态 | 按计划推进 |\n\n完整的测试日报正文。"
            validation = {"passed": True, "draft": {"draft_id": "draft-runtime", "version": 1, "content": report},
                          "confirmation_token": "test-server-token", "content_digest": "test-server-digest"}

            def fake_mcp(endpoint, timeout, name, args):
                server_calls.append((name, args))
                if name == "validate_daily_report_draft":
                    return json.dumps(validation, ensure_ascii=False)
                return '{"report":{"version":1}}'

            stack.enter_context(patch.object(plugin, "_call_mcp", side_effect=fake_mcp))
            args = {"draft_id": "draft-runtime", "expected_version": 1,
                    "confirmation_token": "test-server-token", "content_digest": "test-server-digest",
                    "idempotency_key": "test-publication"}
            for platform in ("wecom_callback", "weixin", "telegram"):
                with self.subTest(platform=platform):
                    session_id = platform + "-session"
                    manager.invoke_hook("pre_llm_call", session_id=session_id, turn_id="draft-turn",
                                        platform=platform, sender_id="employee-runtime", parent_session_id="",
                                        user_message="请整理日报", conversation_history=[])
                    result = registry.dispatch("validate_daily_report_draft", {
                        "draft_id": "draft-runtime", "expected_version": 1,
                    }, scope=manager.scope_key, session_id=session_id, task_id="model-task")
                    self.assertTrue(json.loads(result)["passed"])
                    self.assertEqual(json.loads(result), validation)
                    blocked = registry.dispatch("publish_daily_report_draft", args, scope=manager.scope_key,
                                                session_id=session_id, user_task="确认提交日报")
                    self.assertIn("error", json.loads(blocked))
                    manager.invoke_hook("post_llm_call", session_id=session_id, turn_id="draft-turn",
                                        platform=platform, user_message="请整理日报",
                                        assistant_response=report + "\n请回复确认提交日报。", conversation_history=[])
                    if platform == "wecom_callback":
                        from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter
                        from plugins.profile_rag_mcp.wecom_binding import patch_reply_delivery
                        patch_reply_delivery(WecomCallbackAdapter)
                        adapter = object.__new__(WecomCallbackAdapter)
                        adapter._resolve_app_for_chat = lambda chat: {"agent_id": "1", "name": "offline-test"}
                        adapter._get_access_token = AsyncMock(return_value="unused-offline-token")
                        post = AsyncMock(return_value=SimpleNamespace(json=lambda: {"errcode": 0, "msgid": "offline"}))
                        adapter._http_client = SimpleNamespace(post=post)
                        sent = asyncio.run(adapter.send("test:member", report + "\n请回复确认提交日报。"))
                        self.assertTrue(sent.success)
                        displayed = "".join(call.kwargs["json"]["text"]["content"] for call in post.call_args_list)
                        self.assertIn("当前状态：按计划推进", displayed)
                        self.assertNotIn("|---|", displayed)
                        self.assertIn("确认提交日报", displayed)
                    manager.invoke_hook("pre_llm_call", session_id=session_id, turn_id="confirmation-turn",
                                        platform=platform, sender_id="employee-runtime", parent_session_id="",
                                        user_message="确认提交日报", conversation_history=[])
                    published = registry.dispatch("publish_daily_report_draft", args, scope=manager.scope_key,
                                                  session_id=session_id, task_id="different-model-task")
                    self.assertEqual(json.loads(published)["report"]["version"], 1)
                    foreign = registry.dispatch("publish_daily_report_draft", args, scope=manager.scope_key,
                                                session_id="different-session", user_task="确认提交日报")
                    self.assertIn("error", json.loads(foreign))
            self.assertEqual([name for name, _ in server_calls],
                             ["validate_daily_report_draft", "publish_daily_report_draft"] * 3)


if __name__ == "__main__":
    unittest.main()
