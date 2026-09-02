from __future__ import annotations

import argparse

from .hermes_control import InstalledHermesControl
from .service import ProvisioningError
from .settings import ProvisionerSettings


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the Hermes employee Profile template.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create-template", help="Clone and scrub a credential-free employee template.")
    create.add_argument("--clone-from", required=True, help="Existing configured Profile to copy.")
    sync = subcommands.add_parser(
        "sync-employee-profiles",
        help="Converge the employee template and existing employee Profile configurations.",
    )
    sync.add_argument("--model-source", required=True, help="Existing working Profile that owns the model settings.")
    sync.add_argument(
        "--include-profile",
        action="append",
        default=[],
        help="Also converge this unmanaged/test Profile; may be repeated.",
    )
    args = parser.parse_args()

    settings = ProvisionerSettings.from_env(require_token=False)
    control = InstalledHermesControl(settings)
    try:
        if args.command == "create-template":
            path = control.create_employee_template(args.clone_from)
            print(f"Created credential-free employee template at {path}")
        else:
            profiles = control.sync_employee_profiles(
                args.model_source,
                include_profiles=tuple(args.include_profile),
            )
            print(f"Synchronized employee template and {len(profiles)} Profile(s)")
    except ProvisioningError as exc:
        parser.exit(1, f"{exc.code}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
