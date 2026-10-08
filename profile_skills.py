"""Install neutral employee workflows and isolate native skill discovery."""
from functools import wraps
import json
import logging
import os
from pathlib import Path
import tempfile


logger = logging.getLogger(__name__)
REPORT_SKILL = "profile-daily-report"


def _managed_profile(home):
    """Require a provisioned identity before writing into an employee Profile."""
    home = Path(home)
    if home.is_symlink() or home.parent.name != "profiles":
        return False
    if home.name == "employee-template":
        return home.is_dir()
    if not home.name.startswith("employee-"):
        return False
    marker = home / ".rag-mcp-employee.json"
    if marker.is_symlink():
        return False
    try:
        identity = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(identity, dict) and identity.get("profile_name") == home.name
            and bool(identity.get("username")))


def install_report_skill(home):
    """Atomically refresh this managed skill only; never copy employee state."""
    home = Path(home)
    if not _managed_profile(home):
        return False
    skills = home / "skills"
    directory = skills / REPORT_SKILL
    path = directory / "SKILL.md"
    if any(p.is_symlink() for p in (home.parent, skills, directory, path)):
        raise ValueError("refusing a symlinked employee skill destination")
    content = (Path(__file__).parent / "skills" / REPORT_SKILL / "SKILL.md").read_bytes()
    if path.exists() and path.read_bytes() == content:
        return False
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".daily-report-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def sync_report_skills(root):
    """Converge existing employees and the neutral onboarding template."""
    profiles = Path(root) / "profiles"
    if not profiles.is_dir() or profiles.is_symlink():
        return 0
    installed = 0
    for home in sorted(profiles.iterdir()):
        try:
            installed += int(install_report_skill(home))
        except (OSError, ValueError):
            logger.warning("could not install daily-report skill for %s", home.name)
    return installed


def install_profile_skills():
    from hermes_constants import get_hermes_home
    from tools import skill_manager_tool, skills_tool

    home = Path(get_hermes_home())
    root = home.parent.parent if home.parent.name == "profiles" else home
    sync_report_skills(root)

    original_dir = skills_tool._skills_dir
    if not getattr(original_dir, "_employee_report_skill", False):
        @wraps(original_dir)
        def scoped_dir():
            current_home = Path(get_hermes_home())
            # Discovery happens in the active Profile, including newly bound
            # employees created after plugin registration. Respect native test
            # or administrator overrides instead of writing into those paths.
            directory = original_dir()
            if Path(directory) == current_home / "skills":
                try:
                    install_report_skill(current_home)
                except (OSError, ValueError):
                    logger.warning("could not refresh the current Profile daily-report skill")
            return directory

        scoped_dir._employee_report_skill = True
        skills_tool._skills_dir = scoped_dir

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
