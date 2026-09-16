from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def test_weixin_provider_status_is_suppressed_but_final_and_other_statuses_are_preserved(monkeypatch):
    spec = importlib.util.spec_from_file_location("provider_status_plugin_test", Path(__file__).parents[1] / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    run = ModuleType("gateway.run")
    run._looks_like_gateway_provider_error = lambda text: text.startswith("API call failed")
    run._prepare_gateway_status_message = lambda platform, event_type, message: f"safe:{message}"
    run._gateway_provider_error_reply = lambda message: "final error"
    gateway = ModuleType("gateway")
    gateway.run = run
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.run", run)
    final_reply = run._gateway_provider_error_reply

    assert plugin._install_weixin_provider_status_filter()
    assert not plugin._install_weixin_provider_status_filter()
    prepare = run._prepare_gateway_status_message
    assert prepare(SimpleNamespace(value="weixin"), "error", "API call failed after 5 retries") is None
    assert prepare("weixin", "error", "API call failed") is None
    assert prepare("weixin", "info", "Waiting for approval") == "safe:Waiting for approval"
    assert prepare("telegram", "error", "API call failed") == "safe:API call failed"
    assert run._gateway_provider_error_reply is final_reply
