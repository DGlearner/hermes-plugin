from __future__ import annotations

import asyncio
import base64
import io
import math
import time
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID, uuid4

from .contracts import WechatQrRequest, WechatQrResponse
from .errors import ProvisioningError

QR_SESSION_TTL_SECONDS = 480
QR_POLL_TIMEOUT_MS = 1_500


@dataclass(frozen=True)
class WeixinQrChallenge:
    qr_token: str
    qr_image: str
    base_url: str


@dataclass(frozen=True)
class WeixinQrCredentials:
    account_id: str
    token: str
    base_url: str
    cdn_base_url: str


@dataclass(frozen=True)
class WeixinQrPoll:
    status: Literal["waiting", "scanned", "confirmed", "expired"]
    base_url: str
    credentials: WeixinQrCredentials | None = None


class WeixinQrBackend(Protocol):
    async def start(self) -> WeixinQrChallenge: ...

    async def poll(self, *, qr_token: str, base_url: str) -> WeixinQrPoll: ...


class WeixinQrControl(Protocol):
    def ensure_employee_profile(self, request: WechatQrRequest) -> bool: ...

    def install_profile_weixin_credentials(
        self,
        *,
        profile_name: str,
        credentials: WeixinQrCredentials,
    ) -> None: ...


@dataclass
class _QrSession:
    session_id: UUID
    employee_id: UUID
    profile_name: str
    qr_token: str
    qr_image: str | None
    base_url: str
    status: Literal["waiting", "scanned", "connecting", "connected", "expired", "failed"]
    created_at: float
    expires_at: float


class InstalledWeixinQrBackend:
    """Use the QR protocol bundled in the pinned Hermes Weixin adapter."""

    async def start(self) -> WeixinQrChallenge:
        weixin = self._weixin_module()
        try:
            data = await self._get(
                weixin,
                f"{weixin.ILINK_BASE_URL}{weixin.EP_GET_BOT_QR}",
                params={"bot_type": "3"},
            )
            if data.get("ret") != 0 or not data.get("qrcode") or not data.get("qrcode_img_content"):
                raise ValueError("invalid QR response")
            qr_token = str(data["qrcode"]).strip()
            qr_content = str(data["qrcode_img_content"]).strip()
            if not qr_token or not qr_content:
                raise ValueError("empty QR response")
            return WeixinQrChallenge(
                qr_token=qr_token,
                qr_image=self._qr_data_url(qr_content),
                base_url=str(weixin.ILINK_BASE_URL),
            )
        except ProvisioningError:
            raise
        except Exception as exc:
            raise ProvisioningError(
                "wechat_qr_unavailable",
                "WeChat could not create a login QR code. Try again shortly.",
                status_code=503,
                retryable=True,
            ) from exc

    async def poll(self, *, qr_token: str, base_url: str) -> WeixinQrPoll:
        weixin = self._weixin_module()
        try:
            data = await self._get(
                weixin,
                f"{base_url}{weixin.EP_GET_QR_STATUS}",
                params={"qrcode": qr_token, "timeout": str(QR_POLL_TIMEOUT_MS)},
            )
            status = str(data.get("status") or "")
            if status == "wait":
                return WeixinQrPoll(status="waiting", base_url=base_url)
            if status == "scaned":
                return WeixinQrPoll(status="scanned", base_url=base_url)
            if status == "scaned_but_redirect":
                redirect_id = str(data.get("ilink_bot_id") or "").strip()
                redirected = f"https://{redirect_id}.ilinkai.weixin.qq.com" if redirect_id else base_url
                return WeixinQrPoll(status="scanned", base_url=redirected)
            if status == "expired":
                return WeixinQrPoll(status="expired", base_url=base_url)
            if status != "confirmed":
                raise ValueError("invalid QR status")

            account_id = str(data.get("ilink_bot_id") or "").strip()
            token = str(data.get("ilink_bot_token") or "").strip()
            confirmed_base_url = str(data.get("baseurl") or base_url).strip()
            cdn_base_url = str(data.get("cdn_baseurl") or weixin.WEIXIN_CDN_BASE_URL).strip()
            if not account_id or not token or not confirmed_base_url or not cdn_base_url:
                raise ValueError("incomplete QR credentials")
            return WeixinQrPoll(
                status="confirmed",
                base_url=confirmed_base_url,
                credentials=WeixinQrCredentials(
                    account_id=account_id,
                    token=token,
                    base_url=confirmed_base_url,
                    cdn_base_url=cdn_base_url,
                ),
            )
        except ProvisioningError:
            raise
        except Exception as exc:
            raise ProvisioningError(
                "wechat_qr_poll_failed",
                "WeChat login status could not be checked. Try again shortly.",
                status_code=503,
                retryable=True,
            ) from exc

    @staticmethod
    async def _get(weixin, url: str, *, params: dict[str, str]) -> dict:
        connector = weixin._make_ssl_connector()
        async with weixin.aiohttp.ClientSession(connector=connector) as session:
            return await weixin._api_get(session, url, params=params, use_token=False)

    @staticmethod
    def _weixin_module():
        try:
            from gateway.platforms import weixin
        except ImportError as exc:
            raise ProvisioningError(
                "wechat_qr_unavailable",
                "The Hermes WeChat runtime is unavailable.",
                status_code=503,
            ) from exc
        if not getattr(weixin, "AIOHTTP_AVAILABLE", False):
            raise ProvisioningError(
                "wechat_qr_unavailable",
                "The Hermes WeChat runtime is unavailable.",
                status_code=503,
            )
        return weixin

    @staticmethod
    def _qr_data_url(content: str) -> str:
        try:
            import qrcode

            image = qrcode.make(content)
            output = io.BytesIO()
            image.save(output, format="PNG")
        except Exception as exc:
            raise ProvisioningError(
                "wechat_qr_render_failed",
                "The WeChat QR code could not be rendered.",
                status_code=503,
            ) from exc
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        result = f"data:image/png;base64,{encoded}"
        if len(result) > 200_000:
            raise ProvisioningError(
                "wechat_qr_render_failed",
                "The WeChat QR code could not be rendered.",
                status_code=503,
            )
        return result


class WechatQrLoginService:
    def __init__(
        self,
        control: WeixinQrControl,
        backend: WeixinQrBackend | None = None,
        *,
        ttl_seconds: int = QR_SESSION_TTL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self._control = control
        self._backend = backend or InstalledWeixinQrBackend()
        self._ttl_seconds = max(60, min(QR_SESSION_TTL_SECONDS, int(ttl_seconds)))
        self._clock = clock
        self._lock = asyncio.Lock()
        self._sessions: dict[UUID, _QrSession] = {}
        self._install_tasks: dict[UUID, asyncio.Task[ProvisioningError | None]] = {}

    async def start(self, request: WechatQrRequest) -> WechatQrResponse:
        async with self._lock:
            now = self._clock()
            self._prune(now)
            for session in self._sessions.values():
                if session.employee_id == request.employee_id and session.status in {
                    "waiting",
                    "scanned",
                    "connecting",
                    "connected",
                }:
                    return self._response(session, now)
                if session.status in {"waiting", "scanned", "connecting"}:
                    raise ProvisioningError(
                        "wechat_qr_login_busy",
                        "Another WeChat QR login is in progress. Try again shortly.",
                        status_code=409,
                        retryable=True,
                    )

            await asyncio.to_thread(self._control.ensure_employee_profile, request)
            challenge = await self._backend.start()
            session_id = uuid4()
            session = _QrSession(
                session_id=session_id,
                employee_id=request.employee_id,
                profile_name=request.profile_name,
                qr_token=challenge.qr_token,
                qr_image=challenge.qr_image,
                base_url=challenge.base_url,
                status="waiting",
                created_at=now,
                expires_at=now + self._ttl_seconds,
            )
            self._sessions[session_id] = session
            return self._response(session, now)

    async def poll(self, session_id: UUID, request: WechatQrRequest) -> WechatQrResponse:
        credentials: WeixinQrCredentials | None = None
        install_task: asyncio.Task[ProvisioningError | None] | None = None
        async with self._lock:
            now = self._clock()
            self._prune(now)
            session = self._sessions.get(session_id)
            if session is None or session.employee_id != request.employee_id:
                raise ProvisioningError(
                    "wechat_qr_session_not_found",
                    "The WeChat QR login session was not found or has expired.",
                    status_code=404,
                )
            if session.status in {"connected", "expired", "failed", "connecting"}:
                return self._response(session, now)
            if now >= session.expires_at:
                session.status = "expired"
                session.qr_image = None
                session.qr_token = ""
                return self._response(session, now)

            result = await self._backend.poll(qr_token=session.qr_token, base_url=session.base_url)
            session.base_url = result.base_url
            if result.status == "expired":
                session.status = "expired"
                session.qr_image = None
                session.qr_token = ""
                return self._response(session, now)
            if result.status in {"waiting", "scanned"}:
                session.status = result.status
                return self._response(session, now)
            credentials = result.credentials
            if credentials is None:
                session.status = "failed"
                raise ProvisioningError(
                    "wechat_qr_invalid_confirmation",
                    "WeChat returned an incomplete login confirmation.",
                    status_code=503,
                )
            session.status = "connecting"
            session.qr_image = None
            session.qr_token = ""
            install_task = asyncio.create_task(self._finish_install(session, credentials))
            self._install_tasks[session.session_id] = install_task
            install_task.add_done_callback(
                lambda completed, key=session.session_id: self._forget_install_task(key, completed)
            )

        # The activation task owns the final state transition. Shielding it means an
        # HTTP disconnect or client cancellation cannot strand the in-memory QR
        # session in `connecting` while the blocking Gateway restart continues.
        error = await asyncio.shield(install_task)
        if error is not None:
            raise error
        async with self._lock:
            return self._response(session, self._clock())

    async def _finish_install(
        self,
        session: _QrSession,
        credentials: WeixinQrCredentials,
    ) -> ProvisioningError | None:
        error: ProvisioningError | None = None
        try:
            await asyncio.to_thread(
                self._control.install_profile_weixin_credentials,
                profile_name=session.profile_name,
                credentials=credentials,
            )
        except ProvisioningError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - normalize arbitrary runtime activation failures
            error = ProvisioningError(
                "wechat_channel_install_failed",
                "Hermes could not activate the WeChat channel. Try again shortly.",
                status_code=503,
                retryable=True,
            )
            error.__cause__ = exc

        async with self._lock:
            current = self._sessions.get(session.session_id)
            if current is session:
                session.status = "failed" if error is not None else "connected"
        return error

    def _forget_install_task(
        self,
        session_id: UUID,
        completed: asyncio.Task[ProvisioningError | None],
    ) -> None:
        if self._install_tasks.get(session_id) is completed:
            self._install_tasks.pop(session_id, None)

    def _prune(self, now: float) -> None:
        for session in self._sessions.values():
            if now >= session.expires_at and session.status in {"waiting", "scanned"}:
                session.status = "expired"
                session.qr_image = None
                session.qr_token = ""
        expired_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if now >= session.expires_at + 60 and session.status in {"expired", "failed", "connected"}
        ]
        for session_id in expired_ids:
            self._sessions.pop(session_id, None)

    @staticmethod
    def _response(session: _QrSession, now: float) -> WechatQrResponse:
        return WechatQrResponse(
            status=session.status,
            session_id=session.session_id,
            qr_image=session.qr_image if session.status in {"waiting", "scanned"} else None,
            expires_in=max(0, min(QR_SESSION_TTL_SECONDS, math.ceil(session.expires_at - now))),
        )
