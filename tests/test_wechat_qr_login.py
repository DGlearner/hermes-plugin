from __future__ import annotations

import asyncio
import threading
from ipaddress import ip_network
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from wechat_provisioner.app import create_app
from wechat_provisioner.contracts import WechatQrRequest
from wechat_provisioner.errors import ProvisioningError
from wechat_provisioner.qr_login import (
    InstalledWeixinQrBackend,
    WechatQrLoginService,
    WeixinQrChallenge,
    WeixinQrCredentials,
    WeixinQrPoll,
)
from wechat_provisioner.service import WechatProvisioningService
from wechat_provisioner.settings import ProvisionerSettings
from wechat_provisioner.state import BindingStateStore

EMPLOYEE_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_EMPLOYEE_ID = UUID("00000000-0000-0000-0000-000000000002")
TOKEN = "provisioner-secret-value-at-least-32-characters"


def request(employee_id: UUID = EMPLOYEE_ID) -> WechatQrRequest:
    return WechatQrRequest(
        employee_id=employee_id,
        username=f"employee-{employee_id.int}",
        display_name="Employee",
    )


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


class FakeBackend:
    def __init__(self) -> None:
        self.starts = 0
        self.polls: list[tuple[str, str]] = []
        self.results = [
            WeixinQrPoll(status="scanned", base_url="https://redirected.example"),
            WeixinQrPoll(
                status="confirmed",
                base_url="https://confirmed.example",
                credentials=WeixinQrCredentials(
                    account_id="bot-account",
                    token="wechat-channel-secret",
                    base_url="https://confirmed.example",
                    cdn_base_url="https://cdn.example",
                ),
            ),
        ]

    async def start(self) -> WeixinQrChallenge:
        self.starts += 1
        return WeixinQrChallenge(
            qr_token="temporary-qr-token",
            qr_image="data:image/png;base64,AAAA",
            base_url="https://initial.example",
        )

    async def poll(self, *, qr_token: str, base_url: str) -> WeixinQrPoll:
        self.polls.append((qr_token, base_url))
        return self.results.pop(0)


class FakeControl:
    def __init__(self) -> None:
        self.profiles: list[str] = []
        self.installs: list[tuple[str, WeixinQrCredentials]] = []

    def ensure_employee_profile(self, body: WechatQrRequest) -> bool:
        self.profiles.append(body.profile_name)
        return True

    def install_profile_weixin_credentials(
        self,
        *,
        profile_name: str,
        credentials: WeixinQrCredentials,
    ) -> None:
        self.installs.append((profile_name, credentials))

    def check_ready(self) -> None:
        return None


@pytest.mark.asyncio
async def test_installed_weixin_backend_uses_current_ilink_api_contract() -> None:
    calls: list[dict] = []

    class Session:
        def __init__(self, **options):
            assert options == {"connector": None, "trust_env": True}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def api_get(session, **kwargs):
        assert isinstance(session, Session)
        calls.append(kwargs)
        return {"ret": 0}

    weixin = SimpleNamespace(
        aiohttp=SimpleNamespace(ClientSession=Session),
        _make_ssl_connector=lambda: None,
        _api_get=api_get,
    )
    await InstalledWeixinQrBackend._get(
        weixin,
        "https://ilinkai.weixin.qq.com",
        "ilink/bot/get_bot_qrcode",
        params={"bot_type": "3"},
    )
    assert calls == [{
        "base_url": "https://ilinkai.weixin.qq.com",
        "endpoint": "ilink/bot/get_bot_qrcode?bot_type=3",
        "timeout_ms": 8_000,
    }]


@pytest.mark.asyncio
async def test_installed_weixin_backend_reads_current_confirmation_fields(monkeypatch) -> None:
    weixin = SimpleNamespace(
        ILINK_BASE_URL="https://ilinkai.weixin.qq.com",
        EP_GET_BOT_QR="ilink/bot/get_bot_qrcode",
        EP_GET_QR_STATUS="ilink/bot/get_qrcode_status",
        WEIXIN_CDN_BASE_URL="https://novac2c.cdn.weixin.qq.com/c2c",
    )
    statuses = [
        {"status": "scaned_but_redirect", "redirect_host": "sh.ilinkai.weixin.qq.com"},
        {"status": "confirmed", "ilink_bot_id": "bot-account", "bot_token": "secret-token"},
    ]

    async def fake_get(_weixin, _base_url, endpoint, *, params):
        if endpoint == weixin.EP_GET_BOT_QR:
            assert params == {"bot_type": "3"}
            return {"ret": 0, "qrcode": "qr-token", "qrcode_img_content": "scan-url"}
        assert params == {"qrcode": "qr-token"}
        return statuses.pop(0)

    monkeypatch.setattr(InstalledWeixinQrBackend, "_weixin_module", staticmethod(lambda: weixin))
    monkeypatch.setattr(InstalledWeixinQrBackend, "_get", staticmethod(fake_get))
    monkeypatch.setattr(InstalledWeixinQrBackend, "_qr_data_url", staticmethod(lambda _data: "data:image/png;base64,AAAA"))

    backend = InstalledWeixinQrBackend()
    challenge = await backend.start()
    redirected = await backend.poll(qr_token=challenge.qr_token, base_url=challenge.base_url)
    confirmed = await backend.poll(qr_token=challenge.qr_token, base_url=redirected.base_url)

    assert redirected.base_url == "https://sh.ilinkai.weixin.qq.com"
    assert confirmed.status == "confirmed"
    assert confirmed.credentials is not None
    assert confirmed.credentials.token == "secret-token"


@pytest.mark.asyncio
async def test_qr_login_is_employee_scoped_singleton_and_installs_only_after_confirmation() -> None:
    clock = FakeClock()
    backend = FakeBackend()
    control = FakeControl()
    service = WechatQrLoginService(control, backend, clock=clock)

    started = await service.start(request())
    repeated = await service.start(request())
    assert started.status == "waiting"
    assert repeated.session_id == started.session_id
    assert backend.starts == 1
    assert control.installs == []
    assert "temporary-qr-token" not in started.model_dump_json()

    with pytest.raises(ProvisioningError) as busy:
        await service.start(request(OTHER_EMPLOYEE_ID))
    assert busy.value.code == "wechat_qr_login_busy"

    scanned = await service.poll(started.session_id, request())
    assert scanned.status == "scanned"
    assert control.installs == []
    connected = await service.poll(started.session_id, request())
    assert connected.status == "connected"
    assert connected.qr_image is None
    assert len(control.installs) == 1
    assert control.installs[0][0] == request().profile_name
    serialized = connected.model_dump_json()
    assert "wechat-channel-secret" not in serialized
    assert "bot-account" not in serialized
    assert backend.polls == [
        ("temporary-qr-token", "https://initial.example"),
        ("temporary-qr-token", "https://redirected.example"),
    ]


@pytest.mark.asyncio
async def test_qr_session_expires_and_cannot_be_polled_by_another_employee() -> None:
    clock = FakeClock()
    backend = FakeBackend()
    service = WechatQrLoginService(FakeControl(), backend, ttl_seconds=60, clock=clock)
    started = await service.start(request())

    with pytest.raises(ProvisioningError) as hidden:
        await service.poll(started.session_id, request(OTHER_EMPLOYEE_ID))
    assert hidden.value.code == "wechat_qr_session_not_found"

    clock.value += 61
    expired = await service.poll(started.session_id, request())
    assert expired.status == "expired"
    assert expired.qr_image is None
    assert backend.polls == []


@pytest.mark.asyncio
async def test_qr_activation_survives_a_cancelled_poll_request() -> None:
    install_started = threading.Event()
    allow_install = threading.Event()

    class SlowControl(FakeControl):
        def install_profile_weixin_credentials(
            self,
            *,
            profile_name: str,
            credentials: WeixinQrCredentials,
        ) -> None:
            install_started.set()
            assert allow_install.wait(timeout=2)
            super().install_profile_weixin_credentials(
                profile_name=profile_name,
                credentials=credentials,
            )

    control = SlowControl()
    backend = FakeBackend()
    service = WechatQrLoginService(control, backend)
    started = await service.start(request())
    assert (await service.poll(started.session_id, request())).status == "scanned"

    confirming = asyncio.create_task(service.poll(started.session_id, request()))
    assert await asyncio.to_thread(install_started.wait, 1)
    confirming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await confirming

    assert (await service.poll(started.session_id, request())).status == "connecting"
    allow_install.set()
    for _ in range(100):
        result = await service.poll(started.session_id, request())
        if result.status == "connected":
            break
        await asyncio.sleep(0.01)

    assert result.status == "connected"
    assert len(control.installs) == 1


def settings(tmp_path: Path) -> ProvisionerSettings:
    return ProvisionerSettings(
        token=TOKEN,
        hermes_home=tmp_path,
        template_profile="employee-template",
        allowed_networks=(ip_network("100.64.0.0/10"),),
        restart_timeout_seconds=10,
    )


@pytest.mark.asyncio
async def test_qr_http_api_requires_token_and_returns_only_browser_contract(tmp_path: Path) -> None:
    control = FakeControl()
    qr_service = WechatQrLoginService(control, FakeBackend())
    app = create_app(
        settings(tmp_path),
        WechatProvisioningService(control, BindingStateStore(tmp_path / "bindings.json")),
        qr_service,
    )
    transport = httpx.ASGITransport(app=app, client=("100.100.1.2", 1234))
    payload = request().model_dump(mode="json")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.post("/v1/wechat-qr", json=payload)
        started = await client.post(
            "/v1/wechat-qr",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=payload,
        )

    assert unauthorized.status_code == 401
    assert started.status_code == 200
    assert set(started.json()) == {"status", "session_id", "qr_image", "expires_in"}
    assert "temporary-qr-token" not in started.text
    assert TOKEN not in started.text
