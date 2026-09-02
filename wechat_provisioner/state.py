from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

BindingState = Literal["provisioning", "active", "revoking", "revoked"]


@dataclass(frozen=True)
class StoredBinding:
    binding_id: UUID
    employee_id: UUID
    profile_name: str
    external_user_id: str
    user_name: str
    status: BindingState
    bound_at: datetime | None
    updated_at: datetime

    def to_json(self) -> dict[str, str | None]:
        payload = asdict(self)
        payload["binding_id"] = str(self.binding_id)
        payload["employee_id"] = str(self.employee_id)
        payload["bound_at"] = self.bound_at.isoformat() if self.bound_at else None
        payload["updated_at"] = self.updated_at.isoformat()
        return payload

    @classmethod
    def from_json(cls, payload: dict) -> StoredBinding:
        status = str(payload["status"])
        if status not in {"provisioning", "active", "revoking", "revoked"}:
            raise ValueError("Invalid stored binding status")
        bound_at = payload.get("bound_at")
        return cls(
            binding_id=UUID(str(payload["binding_id"])),
            employee_id=UUID(str(payload["employee_id"])),
            profile_name=str(payload["profile_name"]),
            external_user_id=str(payload["external_user_id"]),
            user_name=str(payload.get("user_name") or ""),
            status=status,  # type: ignore[arg-type]
            bound_at=datetime.fromisoformat(str(bound_at)) if bound_at else None,
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
        )


class BindingStateStore:
    """Small durable journal; pairing codes and PAT values are never persisted."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, binding_id: UUID) -> StoredBinding | None:
        return self._load().get(str(binding_id))

    def find_active_user(self, external_user_id: str) -> StoredBinding | None:
        for record in self._load().values():
            if record.external_user_id == external_user_id and record.status in {"provisioning", "active", "revoking"}:
                return record
        return None

    def put(self, record: StoredBinding) -> None:
        records = self._load()
        records[str(record.binding_id)] = record
        self._write(records)

    def _load(self) -> dict[str, StoredBinding]:
        if not self._path.exists():
            return {}
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("bindings"), dict):
            raise ValueError("Invalid Hermes provisioner state file")
        return {key: StoredBinding.from_json(value) for key, value in payload["bindings"].items()}

    def _write(self, records: dict[str, StoredBinding]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "bindings": {key: record.to_json() for key, record in sorted(records.items())},
        }
        fd, temporary = tempfile.mkstemp(prefix="wechat-bindings-", suffix=".tmp", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def utc_now() -> datetime:
    return datetime.now(UTC)
