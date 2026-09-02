"""Exercise the plugin through Hermes' real Profile and tool Registry scopes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

LOCAL_TOOL_COUNT = 2


def _expected_tool_count() -> int:
    payload = json.loads(Path(__file__).with_name("tool_catalog_cache.json").read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("tool_catalog_cache.json must contain a non-empty list")
    names = [item.get("name") for item in payload if isinstance(item, dict)]
    if len(names) != len(payload) or any(not isinstance(name, str) or not name for name in names):
        raise ValueError("tool_catalog_cache.json contains an invalid tool entry")
    if len(set(names)) != len(names):
        raise ValueError("tool_catalog_cache.json contains duplicate tool names")
    return len(names) + LOCAL_TOOL_COUNT


def smoke_profile(profile_home: Path) -> dict:
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )
    from hermes_cli.plugins import get_plugin_manager
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.registry import registry

    resolved_home = profile_home.expanduser().resolve()
    set_multiplex_active(True)
    home_token = set_hermes_home_override(resolved_home)
    secret_token = set_secret_scope(build_profile_secret_scope(resolved_home))
    try:
        manager = get_plugin_manager()
        manager.discover_and_load()
        profile_tools = [entry for entry in registry.get_all_entries() if entry.toolset == "profile_rag_mcp"]
        raw_identity = registry.dispatch("get_current_user", {}, scope=manager.scope_key)
        identity = json.loads(raw_identity)
        expected_count = _expected_tool_count()
        if len(profile_tools) != expected_count:
            raise RuntimeError(f"expected {expected_count} profile_rag_mcp tools, found {len(profile_tools)}")
        if not isinstance(identity, dict) or identity.get("error"):
            raise RuntimeError(f"get_current_user failed for profile {resolved_home.name}: {raw_identity}")
        return {
            "profile": resolved_home.name,
            "tool_count": len(profile_tools),
            "identity": identity,
        }
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_homes", nargs="+", type=Path)
    args = parser.parse_args()
    for profile_home in args.profile_homes:
        print(json.dumps(smoke_profile(profile_home), ensure_ascii=False))


if __name__ == "__main__":
    main()
