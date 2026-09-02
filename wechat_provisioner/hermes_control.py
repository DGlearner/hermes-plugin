from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from .contracts import BindWechatRequest
from .service import ProvisioningError, WechatIdentity
from .settings import ProvisionerSettings

PAT_ENV = "MCP_COMPANY_MCP_API_KEY"
SHARED_MODEL_API_KEY_ENV = "HERMES_SHARED_MODEL_API_KEY"
FORBIDDEN_TEMPLATE_SECRETS = {
    PAT_ENV,
    "WEIXIN_ACCOUNT_ID",
    "WEIXIN_ALLOWED_USERS",
    "WEIXIN_TOKEN",
}
MANAGED_ROUTE_PREFIX = "rag-mcp-wechat-"
EMPLOYEE_WEIXIN_TOOLSETS = (
    "web",
    "vision",
    "session_search",
    "clarify",
    "profile_rag_mcp",
)
MANAGED_PROFILE_CONFIG_KEYS = (
    "model",
    "providers",
    "plugins",
)
SESSION_RESET_POLICY = {
    "mode": "daily",
    "idle_minutes": 1440,
    "at_hour": 4,
    "notify": False,
}
ATTACHMENT_CLEANUP_REQUEST_FILE = ".profile-rag-mcp-attachment-cleanup"


class InstalledHermesControl:
    """Adapter around the APIs shipped in the running Hermes image."""

    def __init__(self, settings: ProvisionerSettings) -> None:
        self._settings = settings
        self._home = settings.hermes_home
        self._template = settings.template_profile

    def check_ready(self) -> None:
        template_home = self._profile_home(self._template)
        if not self._home.is_dir() or not os.access(self._home, os.W_OK):
            raise ProvisioningError("hermes_home_unavailable", "Hermes data directory is unavailable.", status_code=503)
        if not template_home.is_dir():
            raise ProvisioningError("template_missing", "Hermes employee template profile is missing.", status_code=503)
        with self._profile_scope(template_home):
            from agent.secret_scope import build_profile_secret_scope

            template_secrets = build_profile_secret_scope(template_home)
        forbidden = sorted(key for key in FORBIDDEN_TEMPLATE_SECRETS if template_secrets.get(key))
        if forbidden:
            raise ProvisioningError(
                "template_contains_employee_secrets",
                "Hermes employee template contains user-specific credentials.",
                status_code=503,
            )
        if not os.environ.get(SHARED_MODEL_API_KEY_ENV, "").strip():
            raise ProvisioningError(
                "shared_model_credential_missing",
                "Hermes shared model credential is unavailable.",
                status_code=503,
            )
        template_config = self._read_profile_config(template_home)
        if self._contains_inline_model_credential(template_config):
            raise ProvisioningError(
                "template_contains_model_secret",
                "Hermes employee template contains an inline model credential.",
                status_code=503,
            )
        self._validate_employee_profile_config(template_config)
        config = self._read_gateway_config()
        gateway = config.get("gateway") or {}
        if not isinstance(gateway, dict) or gateway.get("multiplex_profiles") is not True:
            raise ProvisioningError("multiplex_disabled", "Hermes profile multiplexing is not enabled.", status_code=503)

    def create_employee_template(self, source_profile: str) -> Path:
        template_home = self._profile_home(self._template)
        if template_home.exists():
            raise ProvisioningError("template_exists", "Hermes employee template already exists.", status_code=409)
        source_home = self._profile_home(source_profile)
        if not source_home.is_dir():
            raise ProvisioningError("source_profile_missing", "Hermes source profile does not exist.")
        with self._profile_scope(self._home):
            from hermes_cli.profiles import create_profile, validate_profile_name

            validate_profile_name(source_profile)
            create_profile(
                self._template,
                clone_from=source_profile,
                clone_config=True,
                no_alias=True,
                description="Credential-free template for employee WeChat profiles",
            )
        with self._profile_scope(template_home):
            from hermes_cli.config import remove_env_value

            for env_key in FORBIDDEN_TEMPLATE_SECRETS:
                self._call_env_writer(env_key, remove_env_value, env_key)
        source_config = self._read_profile_config(source_home)
        self._converge_profile_config(template_home, source_config)
        self._atomic_json(
            template_home / ".rag-mcp-template.json",
            {"profile_name": self._template, "source_profile": source_profile},
        )
        self.check_ready()
        return template_home

    def sync_employee_profiles(
        self,
        source_profile: str,
        *,
        include_profiles: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Converge the template and managed employee Profiles without touching Session databases."""
        source_home = self._profile_home(source_profile)
        if not source_home.is_dir() or not (source_home / "config.yaml").is_file():
            raise ProvisioningError("source_profile_missing", "Hermes model source profile does not exist.")
        source_config = self._read_profile_config(source_home)
        template_home = self._profile_home(self._template)
        template_home.mkdir(parents=True, exist_ok=True)
        self._converge_profile_config(template_home, source_config)

        profile_names = {
            path.parent.name
            for path in (self._home / "profiles").glob("*/.rag-mcp-employee.json")
        }
        profile_names.update(include_profiles)
        synchronized: list[str] = []
        for profile_name in sorted(profile_names):
            profile_home = self._profile_home(profile_name)
            if not profile_home.is_dir():
                raise ProvisioningError(
                    "profile_missing",
                    f"Hermes profile '{profile_name}' does not exist.",
                )
            self._converge_profile_config(profile_home, self._read_profile_config(template_home))
            synchronized.append(profile_name)
        return tuple(synchronized)

    def approve_default_pairing(self, pairing_code: str) -> WechatIdentity:
        with self._profile_scope(self._home):
            from gateway.pairing import PairingStore

            store = PairingStore(profile="default")
            result = store.approve_code("weixin", pairing_code)
            if not result:
                locked = bool(store._is_locked_out("weixin"))
                raise ProvisioningError(
                    "pairing_locked" if locked else "invalid_pairing_code",
                    "Pairing is temporarily locked." if locked else "The pairing code is invalid or expired.",
                    status_code=429 if locked else 400,
                    retryable=locked,
                )
        return WechatIdentity(str(result["user_id"]), str(result.get("user_name") or ""))

    def ensure_employee_profile(self, request: BindWechatRequest) -> bool:
        profile_home = self._profile_home(request.profile_name)
        marker = profile_home / ".rag-mcp-employee.json"
        created = False
        if not profile_home.exists():
            with self._profile_scope(self._home):
                from hermes_cli.profiles import create_profile

                create_profile(
                    request.profile_name,
                    clone_from=self._template,
                    clone_config=True,
                    no_alias=True,
                    description=f"WeChat employee profile for {request.display_name}",
                )
            created = True

        self._converge_profile_config(
            profile_home,
            self._read_profile_config(self._profile_home(self._template)),
        )

        if marker.exists():
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            if str(metadata.get("employee_id")) != str(request.employee_id):
                raise ProvisioningError("profile_conflict", "Hermes profile belongs to another employee.", status_code=409)
        else:
            self._atomic_json(
                marker,
                {
                    "employee_id": str(request.employee_id),
                    "profile_name": request.profile_name,
                    "username": request.username,
                },
            )
        return created

    def set_profile_pat(self, profile_name: str, personal_token: str) -> None:
        with self._profile_scope(self._profile_home(profile_name)):
            from hermes_cli.config import save_env_value

            self._call_env_writer(PAT_ENV, save_env_value, PAT_ENV, personal_token)

    def remove_profile_pat(self, profile_name: str) -> None:
        profile_home = self._profile_home(profile_name)
        if not profile_home.exists():
            return
        with self._profile_scope(profile_home):
            from hermes_cli.config import remove_env_value

            self._call_env_writer(PAT_ENV, remove_env_value, PAT_ENV)

    def request_profile_attachment_cleanup(self, profile_name: str) -> None:
        profile_home = self._profile_home(profile_name)
        if not profile_home.is_dir():
            return
        path = profile_home / ATTACHMENT_CLEANUP_REQUEST_FILE
        fd, temporary = tempfile.mkstemp(
            prefix=f"{path.name}-",
            suffix=".tmp",
            dir=profile_home,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"requested_at={time.time()}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def grant_profile_user(self, profile_name: str, identity: WechatIdentity) -> None:
        with self._profile_scope(self._profile_home(profile_name)):
            from gateway.pairing import PairingStore

            store = PairingStore(profile=profile_name)
            with store._lock:
                store._approve_user("weixin", identity.user_id, identity.user_name)

    def revoke_profile_user(self, profile_name: str, external_user_id: str) -> None:
        profile_home = self._profile_home(profile_name)
        if not profile_home.exists():
            return
        with self._profile_scope(profile_home):
            from gateway.pairing import PairingStore

            PairingStore(profile=profile_name).revoke("weixin", external_user_id)

    def grant_default_user(self, identity: WechatIdentity) -> None:
        with self._profile_scope(self._home):
            from gateway.pairing import PairingStore

            store = PairingStore(profile="default")
            with store._lock:
                store._approve_user("weixin", identity.user_id, identity.user_name)

    def revoke_default_user(self, external_user_id: str) -> None:
        with self._profile_scope(self._home):
            from gateway.pairing import PairingStore

            PairingStore(profile="default").revoke("weixin", external_user_id)

    def upsert_route(self, binding_id: UUID, profile_name: str, external_user_id: str) -> bool:
        config = self._read_gateway_config()
        gateway = config.setdefault("gateway", {})
        if not isinstance(gateway, dict):
            raise ProvisioningError("invalid_gateway_config", "Hermes gateway configuration is invalid.", status_code=503)
        routes = gateway.get("profile_routes") or []
        if not isinstance(routes, list):
            raise ProvisioningError("invalid_gateway_config", "Hermes profile routes are invalid.", status_code=503)

        route_name = self._route_name(binding_id)
        retained = [
            route
            for route in routes
            if not self._route_conflicts(route, route_name, profile_name, external_user_id)
        ]
        retained.append(
            {
                "name": route_name,
                "platform": "weixin",
                "chat_id": external_user_id,
                "profile": profile_name,
            }
        )
        if retained == routes:
            return False
        gateway["profile_routes"] = retained
        self._write_gateway_config(config)
        return True

    def remove_route(self, binding_id: UUID, profile_name: str, external_user_id: str) -> bool:
        config = self._read_gateway_config()
        gateway = config.get("gateway") or {}
        routes = gateway.get("profile_routes") or [] if isinstance(gateway, dict) else []
        if not isinstance(routes, list):
            raise ProvisioningError("invalid_gateway_config", "Hermes profile routes are invalid.", status_code=503)
        route_name = self._route_name(binding_id)
        retained = [
            route
            for route in routes
            if not (
                isinstance(route, dict)
                and route.get("name") == route_name
                and route.get("profile") == profile_name
                and route.get("chat_id") == external_user_id
            )
        ]
        if retained == routes:
            return False
        gateway["profile_routes"] = retained
        self._write_gateway_config(config)
        return True

    def restart_gateway(self, required_profile: str) -> None:
        before_pid = self._gateway_pid()
        try:
            result = subprocess.run(
                ["hermes", "gateway", "restart"],
                cwd=self._home,
                capture_output=True,
                text=True,
                timeout=self._settings.restart_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProvisioningError(
                "gateway_restart_failed",
                "Hermes gateway could not be restarted.",
                status_code=503,
                retryable=True,
            ) from exc
        if result.returncode != 0:
            raise ProvisioningError(
                "gateway_restart_failed",
                "Hermes gateway could not be restarted.",
                status_code=503,
                retryable=True,
            )

        deadline = time.monotonic() + self._settings.restart_timeout_seconds
        while time.monotonic() < deadline:
            status = self._gateway_status()
            served = status.get("served_profiles") or []
            pid = status.get("pid")
            restarted = before_pid is None or (pid is not None and pid != before_pid)
            if restarted and required_profile in served:
                return
            time.sleep(0.25)
        raise ProvisioningError(
            "gateway_restart_timeout",
            "Hermes gateway did not become ready after restart.",
            status_code=503,
            retryable=True,
        )

    @contextmanager
    def _profile_scope(self, profile_home: Path):
        from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home_token = set_hermes_home_override(profile_home)
        secret_token = set_secret_scope(build_profile_secret_scope(profile_home))
        try:
            yield
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)

    def _profile_home(self, profile_name: str) -> Path:
        if profile_name == "default":
            return self._home
        return self._home / "profiles" / profile_name

    def _read_gateway_config(self) -> dict:
        path = self._home / "config.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ProvisioningError("invalid_gateway_config", "Hermes gateway configuration is invalid.", status_code=503)
        return payload

    @staticmethod
    def _read_profile_config(profile_home: Path) -> dict[str, Any]:
        path = profile_home / "config.yaml"
        if not path.is_file():
            return {}
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ProvisioningError("invalid_profile_config", "Hermes profile configuration is invalid.", status_code=503)
        return payload

    def _converge_profile_config(self, profile_home: Path, source_config: dict[str, Any]) -> None:
        target = self._read_profile_config(profile_home)
        for key in MANAGED_PROFILE_CONFIG_KEYS:
            if key in source_config:
                target[key] = deepcopy(source_config[key])
            else:
                target.pop(key, None)
        target = self._without_inline_model_credentials(target)

        platform_toolsets = self._mapping_copy(source_config.get("platform_toolsets"))
        platform_toolsets["weixin"] = list(EMPLOYEE_WEIXIN_TOOLSETS)
        target["platform_toolsets"] = platform_toolsets

        builtin_toolsets, plugin_toolsets = self._toolset_catalog()
        known_builtin = self._mapping_copy(source_config.get("known_builtin_toolsets"))
        known_builtin["weixin"] = sorted(builtin_toolsets)
        target["known_builtin_toolsets"] = known_builtin
        known_plugins = self._mapping_copy(source_config.get("known_plugin_toolsets"))
        known_plugins["weixin"] = sorted(plugin_toolsets)
        target["known_plugin_toolsets"] = known_plugins

        target["session_reset"] = dict(SESSION_RESET_POLICY)
        approvals = self._mapping_copy(source_config.get("approvals"))
        deny = approvals.get("deny")
        deny_values = [str(value) for value in deny] if isinstance(deny, list) else []
        approvals["deny"] = list(dict.fromkeys([*deny_values, "*"]))
        target["approvals"] = approvals

        self._validate_employee_profile_config(target)
        self._write_profile_config(profile_home, target)

    @staticmethod
    def _mapping_copy(value: Any) -> dict[str, Any]:
        return deepcopy(value) if isinstance(value, dict) else {}

    @classmethod
    def _without_inline_model_credentials(cls, config: dict[str, Any]) -> dict[str, Any]:
        sanitized = deepcopy(config)
        for key in ("model", "providers"):
            if key in sanitized:
                sanitized[key] = cls._replace_model_credentials(sanitized[key])
        return sanitized

    @classmethod
    def _replace_model_credentials(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._replace_model_credentials(item) for item in value]
        if not isinstance(value, dict):
            return value
        result: dict[str, Any] = {}
        credential_reference = False
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized = key.lower()
            if normalized == "api_key":
                credential_reference = credential_reference or bool(str(raw_value or "").strip())
                continue
            if normalized in {"key_env", "api_key_env"}:
                credential_reference = True
                continue
            result[key] = cls._replace_model_credentials(raw_value)
        if credential_reference:
            result["api_key_env"] = SHARED_MODEL_API_KEY_ENV
        return result

    @classmethod
    def _contains_inline_model_credential(cls, config: dict[str, Any]) -> bool:
        def contains(value: Any) -> bool:
            if isinstance(value, list):
                return any(contains(item) for item in value)
            if not isinstance(value, dict):
                return False
            return any(
                (str(key).lower() == "api_key" and bool(str(item or "").strip())) or contains(item)
                for key, item in value.items()
            )

        return any(contains(config.get(key)) for key in ("model", "providers"))

    @classmethod
    def _uses_shared_model_credential(cls, config: dict[str, Any]) -> bool:
        def uses_shared(value: Any) -> bool:
            if isinstance(value, list):
                return any(uses_shared(item) for item in value)
            if not isinstance(value, dict):
                return False
            return any(
                (
                    str(key).lower() in {"key_env", "api_key_env"}
                    and str(item).strip() == SHARED_MODEL_API_KEY_ENV
                )
                or uses_shared(item)
                for key, item in value.items()
            )

        return any(uses_shared(config.get(key)) for key in ("model", "providers"))

    @staticmethod
    def _toolset_catalog() -> tuple[set[str], set[str]]:
        try:
            from hermes_cli.tools_config import (
                CONFIGURABLE_TOOLSETS,
                _get_effective_configurable_toolsets,
            )

            builtin = {str(item[0]) for item in CONFIGURABLE_TOOLSETS}
            effective = {str(item[0]) for item in _get_effective_configurable_toolsets()}
        except (ImportError, RuntimeError, TypeError, ValueError) as exc:
            raise ProvisioningError(
                "toolset_catalog_unavailable",
                "Hermes toolset catalog is unavailable.",
                status_code=503,
            ) from exc
        plugins = effective - builtin
        if "profile_rag_mcp" not in plugins:
            raise ProvisioningError(
                "rag_plugin_unavailable",
                "Hermes RAG plugin is unavailable.",
                status_code=503,
            )
        return builtin, plugins

    @staticmethod
    def _validate_employee_profile_config(config: dict[str, Any]) -> None:
        if not config.get("model") or not config.get("providers"):
            raise ProvisioningError(
                "employee_model_config_missing",
                "Hermes employee model configuration is incomplete.",
                status_code=503,
            )
        if not InstalledHermesControl._uses_shared_model_credential(config):
            raise ProvisioningError(
                "employee_model_credential_invalid",
                "Hermes employee model configuration does not use the shared credential.",
                status_code=503,
            )
        if (config.get("platform_toolsets") or {}).get("weixin") != list(EMPLOYEE_WEIXIN_TOOLSETS):
            raise ProvisioningError(
                "employee_tool_policy_invalid",
                "Hermes employee tool policy is invalid.",
                status_code=503,
            )
        if config.get("session_reset") != SESSION_RESET_POLICY:
            raise ProvisioningError(
                "employee_session_policy_invalid",
                "Hermes employee Session policy is invalid.",
                status_code=503,
            )
        if "*" not in ((config.get("approvals") or {}).get("deny") or []):
            raise ProvisioningError(
                "employee_approval_policy_invalid",
                "Hermes employee approval policy is invalid.",
                status_code=503,
            )

    @staticmethod
    def _write_profile_config(profile_home: Path, config: dict[str, Any]) -> None:
        profile_home.mkdir(parents=True, exist_ok=True)
        path = profile_home / "config.yaml"
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        serialized = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        fd, temporary = tempfile.mkstemp(prefix="config-", suffix=".yaml.tmp", dir=profile_home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _write_gateway_config(self, config: dict) -> None:
        path = self._home / "config.yaml"
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        serialized = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        fd, temporary = tempfile.mkstemp(prefix="config-", suffix=".yaml.tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _route_name(binding_id: UUID) -> str:
        return f"{MANAGED_ROUTE_PREFIX}{binding_id.hex}"

    @staticmethod
    def _route_conflicts(route, route_name: str, profile_name: str, external_user_id: str) -> bool:
        if not isinstance(route, dict):
            return False
        if route.get("name") == route_name:
            return True
        if route.get("platform") == "weixin" and route.get("chat_id") == external_user_id:
            return True
        return str(route.get("name") or "").startswith(MANAGED_ROUTE_PREFIX) and route.get("profile") == profile_name

    @staticmethod
    def _call_env_writer(env_key: str, writer, *args) -> None:
        sentinel = object()
        previous = os.environ.get(env_key, sentinel)
        try:
            writer(*args)
        finally:
            if previous is sentinel:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = previous

    @staticmethod
    def _atomic_json(path: Path, payload: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f"{path.name}-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _gateway_status() -> dict:
        try:
            from gateway.status import read_runtime_status

            return read_runtime_status() or {}
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            return {}

    def _gateway_pid(self):
        return self._gateway_status().get("pid")
