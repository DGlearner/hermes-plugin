from __future__ import annotations

import asyncio
import threading
import time
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
async def test_status_timeouts_keep_the_same_qr_until_confirmation(monkeypatch) -> None:
    weixin = SimpleNamespace(EP_GET_QR_STATUS="ilink/bot/get_qrcode_status")
    backend = InstalledWeixinQrBackend()
    control = FakeControl()
    clock = FakeClock()
    service = WechatQrLoginService(control, backend, clock=clock)
    attempts = 0

    async def start():
        return WeixinQrChallenge("private-qr-token", "data:image/png;base64,AAAA", "https://ilinkai.weixin.qq.com")

    async def poll_get(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise asyncio.TimeoutError()
        return {"status": "confirmed", "ilink_bot_id": "bot-account", "bot_token": "secret"}

    weixin.WEIXIN_CDN_BASE_URL = "https://cdn.example"
    monkeypatch.setattr(backend, "start", start)
    monkeypatch.setattr(backend, "_weixin_module", lambda: weixin)
    monkeypatch.setattr(backend, "_get", poll_get)
    started = await service.start(request())
    for _ in range(3):
        clock.value += 10
        waiting = await service.poll(started.session_id, request())
        assert waiting.status == "waiting"
        assert waiting.session_id == started.session_id
        assert waiting.qr_image == started.qr_image
        assert waiting.expires_in == 480 - int(clock.value - 1_000)
        assert control.installs == []
    assert (await service.poll(started.session_id, request())).status == "connected"
    assert len(control.installs) == 1


@pytest.mark.asyncio
async def test_waiting_poll_does_not_reset_scanned_status() -> None:
    backend = FakeBackend()
    backend.results.insert(1, WeixinQrPoll(status="waiting", base_url="https://redirected.example"))
    service = WechatQrLoginService(FakeControl(), backend)
    started = await service.start(request())
    assert (await service.poll(started.session_id, request())).status == "scanned"
    waiting = await service.poll(started.session_id, request())
    assert waiting.status == "scanned"
    assert waiting.qr_image == started.qr_image
    assert (await service.poll(started.session_id, request())).status == "connected"


@pytest.mark.asyncio
async def test_weixin_qr_failure_logs_no_upstream_payload(monkeypatch, caplog) -> None:
    weixin = SimpleNamespace(
        ILINK_BASE_URL="https://ilinkai.weixin.qq.com",
        EP_GET_BOT_QR="ilink/bot/get_bot_qrcode",
    )

    async def rejected(_weixin, _base_url, _endpoint, *, params):
        assert params == {"bot_type": "3"}
        return {"ret": "do-not-log-upstream-body", "qrcode": "private-qr-token"}

    monkeypatch.setattr(InstalledWeixinQrBackend, "_weixin_module", staticmethod(lambda: weixin))
    monkeypatch.setattr(InstalledWeixinQrBackend, "_get", staticmethod(rejected))
    with pytest.raises(ProvisioningError) as unavailable:
        await InstalledWeixinQrBackend().start()

    assert unavailable.value.code == "wechat_qr_unavailable"
    assert "do-not-log-upstream-body" not in caplog.text
    assert "private-qr-token" not in caplog.text


@pytest.mark.asyncio
async def test_qr_login_is_employee_scoped_and_installs_only_after_confirmation() -> None:
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

    other = await service.start(request(OTHER_EMPLOYEE_ID))
    assert other.session_id != started.session_id
    assert backend.starts == 2

    with pytest.raises(ProvisioningError) as hidden:
        await service.poll(started.session_id, request(OTHER_EMPLOYEE_ID))
    assert hidden.value.code == "wechat_qr_session_not_found"

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
async def test_slow_qr_creation_does_not_block_other_employees(monkeypatch) -> None:
    from wechat_provisioner import qr_login

    monkeypatch.setattr(qr_login, "MAX_ACTIVE_QR_SESSIONS", 2)

    class SlowBackend:
        def __init__(self) -> None:
            self.starts = 0
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def start(self) -> WeixinQrChallenge:
            self.starts += 1
            number = self.starts
            if number == 1:
                self.first_started.set()
                await self.release_first.wait()
            return WeixinQrChallenge(
                qr_token=f"qr-token-{number}",
                qr_image=f"data:image/png;base64,QR{number}=",
                base_url="https://initial.example",
            )

    backend = SlowBackend()
    service = WechatQrLoginService(FakeControl(), backend)
    first = asyncio.create_task(service.start(request()))
    try:
        await asyncio.wait_for(backend.first_started.wait(), timeout=2)
        second = await asyncio.wait_for(service.start(request(OTHER_EMPLOYEE_ID)), timeout=2)
        assert second.status == "waiting"
        with pytest.raises(ProvisioningError) as full:
            await service.start(request(UUID(int=3)))
        assert full.value.code == "wechat_qr_capacity"
        repeated = asyncio.create_task(service.start(request()))
        await asyncio.sleep(0)
        assert not repeated.done()
    finally:
        backend.release_first.set()
    started, reused = await asyncio.gather(first, repeated)
    assert started.session_id == reused.session_id
    assert started.session_id != second.session_id
    assert started.qr_image != second.qr_image
    assert backend.starts == 2


@pytest.mark.asyncio
async def test_cancelled_qr_creation_releases_pending_capacity(monkeypatch) -> None:
    from wechat_provisioner import qr_login

    monkeypatch.setattr(qr_login, "MAX_ACTIVE_QR_SESSIONS", 1)

    class CancelBackend:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.starts = 0

        async def start(self) -> WeixinQrChallenge:
            self.starts += 1
            if self.starts == 1:
                self.started.set()
                await asyncio.Event().wait()
            return WeixinQrChallenge(
                qr_token="new-qr-token",
                qr_image="data:image/png;base64,AAAA",
                base_url="https://initial.example",
            )

    backend = CancelBackend()
    service = WechatQrLoginService(FakeControl(), backend)
    creating = asyncio.create_task(service.start(request()))
    await asyncio.wait_for(backend.started.wait(), timeout=2)
    creating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creating

    assert (await service.start(request(OTHER_EMPLOYEE_ID))).status == "waiting"
    assert backend.starts == 2


@pytest.mark.asyncio
async def test_slow_employee_poll_does_not_block_another_employee_confirmation() -> None:
    first_started = asyncio.Event()
    first_release = asyncio.Event()

    class IndependentBackend(FakeBackend):
        async def start(self) -> WeixinQrChallenge:
            self.starts += 1
            return WeixinQrChallenge(
                qr_token=f"employee-qr-{self.starts}",
                qr_image="data:image/png;base64,AAAA",
                base_url="https://initial.example",
            )

        async def poll(self, *, qr_token: str, base_url: str) -> WeixinQrPoll:
            self.polls.append((qr_token, base_url))
            if qr_token == "employee-qr-1":
                first_started.set()
                await first_release.wait()
            return WeixinQrPoll(
                status="confirmed",
                base_url="https://confirmed.example",
                credentials=WeixinQrCredentials(
                    account_id=qr_token,
                    token=f"credential-{qr_token}",
                    base_url="https://confirmed.example",
                    cdn_base_url="https://cdn.example",
                ),
            )

    control = FakeControl()
    service = WechatQrLoginService(control, IndependentBackend())
    first = await service.start(request())
    second = await service.start(request(OTHER_EMPLOYEE_ID))
    first_poll = asyncio.create_task(service.poll(first.session_id, request()))
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second_result = await asyncio.wait_for(
            service.poll(second.session_id, request(OTHER_EMPLOYEE_ID)), timeout=1
        )
        assert second_result.status == "connected"
    finally:
        first_release.set()
        await first_poll

    assert first_poll.result().status == "connected"
    assert {(profile, credentials.account_id) for profile, credentials in control.installs} == {
        (request().profile_name, "employee-qr-1"),
        (request(OTHER_EMPLOYEE_ID).profile_name, "employee-qr-2"),
    }


@pytest.mark.asyncio
async def test_simultaneous_confirmations_serialize_gateway_activation() -> None:
    class SlowInstallControl(FakeControl):
        def __init__(self) -> None:
            super().__init__()
            self.guard = threading.Lock()
            self.active = 0
            self.max_active = 0

        def install_profile_weixin_credentials(
            self,
            *,
            profile_name: str,
            credentials: WeixinQrCredentials,
        ) -> None:
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.05)
                super().install_profile_weixin_credentials(
                    profile_name=profile_name, credentials=credentials
                )
            finally:
                with self.guard:
                    self.active -= 1

    backend = FakeBackend()
    backend.results = [
        WeixinQrPoll(
            status="confirmed",
            base_url="https://confirmed.example",
            credentials=WeixinQrCredentials(
                account_id=f"account-{employee}",
                token=f"credential-{employee}",
                base_url="https://confirmed.example",
                cdn_base_url="https://cdn.example",
            ),
        )
        for employee in (1, 2)
    ]
    control = SlowInstallControl()
    service = WechatQrLoginService(control, backend)
    first = await service.start(request())
    second = await service.start(request(OTHER_EMPLOYEE_ID))

    results = await asyncio.gather(
        service.poll(first.session_id, request()),
        service.poll(second.session_id, request(OTHER_EMPLOYEE_ID)),
    )
    assert [result.status for result in results] == ["connected", "connected"]
    assert control.max_active == 1
    assert len(control.installs) == 2


@pytest.mark.asyncio
async def test_qr_login_capacity_applies_only_at_limit_and_expires() -> None:
    clock = FakeClock()
    backend = FakeBackend()
    service = WechatQrLoginService(FakeControl(), backend, ttl_seconds=60, clock=clock)
    for employee in range(1, 65):
        await service.start(request(UUID(int=employee)))

    with pytest.raises(ProvisioningError) as full:
        await service.start(request(UUID(int=65)))
    assert full.value.code == "wechat_qr_capacity"

    clock.value += 61
    assert (await service.start(request(UUID(int=65)))).status == "waiting"


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
