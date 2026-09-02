from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Protocol

from .contracts import BindWechatRequest, UnbindWechatRequest, WechatBindingResponse
from .state import BindingStateStore, StoredBinding, utc_now

logger = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class WechatIdentity:
    user_id: str
    user_name: str


class HermesControl(Protocol):
    def check_ready(self) -> None: ...

    def approve_default_pairing(self, pairing_code: str) -> WechatIdentity: ...

    def ensure_employee_profile(self, request: BindWechatRequest) -> bool: ...

    def grant_profile_user(self, profile_name: str, identity: WechatIdentity) -> None: ...

    def revoke_profile_user(self, profile_name: str, external_user_id: str) -> None: ...

    def grant_default_user(self, identity: WechatIdentity) -> None: ...

    def revoke_default_user(self, external_user_id: str) -> None: ...

    def set_profile_pat(self, profile_name: str, personal_token: str) -> None: ...

    def remove_profile_pat(self, profile_name: str) -> None: ...

    def request_profile_attachment_cleanup(self, profile_name: str) -> None: ...

    def upsert_route(self, binding_id, profile_name: str, external_user_id: str) -> bool: ...

    def remove_route(self, binding_id, profile_name: str, external_user_id: str) -> bool: ...

class WechatProvisioningService:
    def __init__(self, control: HermesControl, state: BindingStateStore) -> None:
        self._control = control
        self._state = state
        self._mutation_lock = asyncio.Lock()

    async def ready(self) -> None:
        await asyncio.to_thread(self._control.check_ready)

    async def bind(self, request: BindWechatRequest) -> WechatBindingResponse:
        async with self._mutation_lock:
            return await asyncio.to_thread(self._bind_sync, request)

    async def unbind(self, binding_id, request: UnbindWechatRequest) -> None:
        async with self._mutation_lock:
            await asyncio.to_thread(self._unbind_sync, binding_id, request)

    def _bind_sync(self, request: BindWechatRequest) -> WechatBindingResponse:
        existing = self._state.get(request.binding_id)
        if existing and (existing.employee_id != request.employee_id or existing.profile_name != request.profile_name):
            raise ProvisioningError("binding_conflict", "Binding identity does not match.", status_code=409)

        if existing and existing.status in {"provisioning", "active", "revoking"}:
            identity = WechatIdentity(existing.external_user_id, existing.user_name)
        else:
            identity = self._control.approve_default_pairing(request.pairing_code.get_secret_value())
            conflict = self._state.find_active_user(identity.user_id)
            if conflict and conflict.binding_id != request.binding_id:
                self._control.revoke_default_user(identity.user_id)
                raise ProvisioningError(
                    "wechat_account_already_bound",
                    "This WeChat account is already bound to another employee.",
                    status_code=409,
                )

        now = utc_now()
        provisioning = StoredBinding(
            binding_id=request.binding_id,
            employee_id=request.employee_id,
            profile_name=request.profile_name,
            external_user_id=identity.user_id,
            user_name=identity.user_name,
            status="provisioning",
            bound_at=existing.bound_at if existing else None,
            updated_at=now,
        )
        self._state.put(provisioning)

        try:
            self._control.ensure_employee_profile(request)
            self._control.set_profile_pat(request.profile_name, request.personal_token.get_secret_value())
            self._control.grant_profile_user(request.profile_name, identity)
            # Hermes authorizes busy-session messages at the ingress adapter
            # before profile routing, so a bound user needs both grants.
            self._control.grant_default_user(identity)
            self._control.upsert_route(request.binding_id, request.profile_name, identity.user_id)
        except ProvisioningError:
            if not existing or existing.status != "active":
                self._best_effort_revoke_default(identity.user_id)
            raise
        except Exception as exc:
            if not existing or existing.status != "active":
                self._best_effort_revoke_default(identity.user_id)
            raise ProvisioningError(
                "hermes_provisioning_failed",
                "Hermes could not finish the binding. Retry with a new pairing code.",
                status_code=503,
                retryable=True,
            ) from exc

        bound_at = provisioning.bound_at or utc_now()
        self._state.put(replace(provisioning, status="active", bound_at=bound_at, updated_at=utc_now()))
        return WechatBindingResponse(
            external_user_id=identity.user_id,
            profile_name=request.profile_name,
            bound_at=bound_at,
        )

    def _unbind_sync(self, binding_id, request: UnbindWechatRequest) -> None:
        existing = self._state.get(binding_id)
        if existing and (existing.employee_id != request.employee_id or existing.profile_name != request.profile_name):
            raise ProvisioningError("binding_conflict", "Binding identity does not match.", status_code=409)
        if existing and existing.external_user_id != request.external_user_id:
            raise ProvisioningError("binding_conflict", "WeChat identity does not match.", status_code=409)

        if existing:
            self._state.put(replace(existing, status="revoking", updated_at=utc_now()))
        self._control.remove_route(binding_id, request.profile_name, request.external_user_id)
        self._control.revoke_profile_user(request.profile_name, request.external_user_id)
        self._control.revoke_default_user(request.external_user_id)
        self._control.remove_profile_pat(request.profile_name)
        request_cleanup = getattr(self._control, "request_profile_attachment_cleanup", None)
        if callable(request_cleanup):
            request_cleanup(request.profile_name)
        if existing:
            self._state.put(replace(existing, status="revoked", updated_at=utc_now()))

    def _best_effort_revoke_default(self, external_user_id: str) -> None:
        try:
            self._control.revoke_default_user(external_user_id)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Could not clean up the temporary default-profile pairing grant: %s", type(exc).__name__)
