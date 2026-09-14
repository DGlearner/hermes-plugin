from __future__ import annotations


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
