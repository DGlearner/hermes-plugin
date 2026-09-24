"""Suppress cross-employee discovery hints in native skill management."""
from functools import wraps


def install_profile_skills():
    from hermes_constants import get_hermes_home
    from tools import skill_manager_tool

    original = skill_manager_tool._find_skill_in_other_profiles
    if getattr(original, "_employee_isolation", False):
        return

    @wraps(original)
    def scoped(name):
        home = get_hermes_home()
        if home.name.startswith("employee-") or home.name == "test-rag-mcp":
            return []
        return original(name)

    scoped._employee_isolation = True
    skill_manager_tool._find_skill_in_other_profiles = scoped
