from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from pathlib import Path

PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_ALLOWED_CIDRS = "127.0.0.0/8,::1/128,100.64.0.0/10"


@dataclass(frozen=True)
class ProvisionerSettings:
    token: str
    hermes_home: Path
    template_profile: str
    allowed_networks: tuple[IPv4Network | IPv6Network, ...]
    restart_timeout_seconds: float

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> ProvisionerSettings:
        token = ""
        if require_token:
            token = os.environ.get("HERMES_WECHAT_PROVISIONER_TOKEN", "").strip()
            token_file = os.environ.get("HERMES_WECHAT_PROVISIONER_TOKEN_FILE", "").strip()
            if token_file:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            if len(token) < 32:
                raise ValueError("Hermes provisioner token must contain at least 32 characters")

        home = Path(os.environ.get("HERMES_HOME", "/opt/data")).expanduser().resolve()
        template = os.environ.get("HERMES_EMPLOYEE_TEMPLATE_PROFILE", "employee-template").strip()
        if not PROFILE_NAME_PATTERN.fullmatch(template):
            raise ValueError("HERMES_EMPLOYEE_TEMPLATE_PROFILE is invalid")

        raw_cidrs = os.environ.get("HERMES_WECHAT_PROVISIONER_ALLOWED_CIDRS", DEFAULT_ALLOWED_CIDRS)
        networks = tuple(ip_network(part.strip(), strict=False) for part in raw_cidrs.split(",") if part.strip())
        if not networks:
            raise ValueError("At least one provisioner client CIDR is required")

        timeout = float(os.environ.get("HERMES_WECHAT_PROVISIONER_RESTART_TIMEOUT_SECONDS", "20"))
        if timeout < 5 or timeout > 120:
            raise ValueError("Provisioner restart timeout must be between 5 and 120 seconds")
        return cls(
            token=token,
            hermes_home=home,
            template_profile=template,
            allowed_networks=networks,
            restart_timeout_seconds=timeout,
        )

    def allows_client(self, host: str | None) -> bool:
        if not host:
            return False
        try:
            address = ip_address(host)
        except ValueError:
            return False
        return any(address.version == network.version and address in network for network in self.allowed_networks)
