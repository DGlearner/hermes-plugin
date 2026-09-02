from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .contracts import (
    BindWechatRequest,
    ProvisionerHealth,
    UnbindWechatRequest,
    WechatBindingResponse,
)
from .hermes_control import InstalledHermesControl
from .service import ProvisioningError, WechatProvisioningService
from .settings import ProvisionerSettings
from .state import BindingStateStore


def create_app(
    settings: ProvisionerSettings | None = None,
    service: WechatProvisioningService | None = None,
) -> FastAPI:
    resolved = settings or ProvisionerSettings.from_env()
    provisioning = service or WechatProvisioningService(
        InstalledHermesControl(resolved),
        BindingStateStore(resolved.hermes_home / "provisioner" / "wechat_bindings.json"),
    )
    app = FastAPI(title="Hermes WeChat Provisioner", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def restrict_clients(request: Request, call_next):
        if not resolved.allows_client(request.client.host if request.client else None):
            return JSONResponse(status_code=403, content={"error_code": "forbidden", "message": "Access denied."})
        return await call_next(request)

    async def require_token(request: Request) -> None:
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {resolved.token}"
        if not secrets.compare_digest(header, expected):
            raise HTTPException(status_code=401, detail="Access denied.")

    @app.exception_handler(ProvisioningError)
    async def provisioning_error(_request: Request, exc: ProvisioningError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": exc.code, "message": str(exc), "retryable": exc.retryable},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error_code": "invalid_request", "message": "Invalid request."},
        )

    @app.get("/livez", response_model=ProvisionerHealth)
    async def livez() -> ProvisionerHealth:
        return ProvisionerHealth(status="ok")

    @app.get("/readyz", response_model=ProvisionerHealth, dependencies=[Depends(require_token)])
    async def readyz() -> ProvisionerHealth:
        await provisioning.ready()
        return ProvisionerHealth(status="ready")

    @app.post(
        "/v1/wechat-bindings",
        response_model=WechatBindingResponse,
        dependencies=[Depends(require_token)],
    )
    async def bind_wechat(body: BindWechatRequest) -> WechatBindingResponse:
        return await provisioning.bind(body)

    @app.delete(
        "/v1/wechat-bindings/{binding_id}",
        status_code=204,
        dependencies=[Depends(require_token)],
    )
    async def unbind_wechat(binding_id: UUID, body: UnbindWechatRequest) -> None:
        await provisioning.unbind(binding_id, body)

    return app
