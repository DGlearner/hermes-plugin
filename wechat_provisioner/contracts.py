from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

PROFILE_PATTERN = re.compile(r"^employee-[0-9a-f]{32}$")


class BindWechatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    employee_id: UUID
    username: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    profile_name: str = Field(pattern=PROFILE_PATTERN.pattern)
    pairing_code: SecretStr
    personal_token: SecretStr

    @model_validator(mode="after")
    def validate_identity(self) -> BindWechatRequest:
        expected_profile = f"employee-{self.employee_id.hex}"
        if self.profile_name != expected_profile:
            raise ValueError("profile_name does not match employee_id")
        if len(self.pairing_code.get_secret_value().strip()) != 8:
            raise ValueError("pairing_code must contain 8 characters")
        if not self.personal_token.get_secret_value().startswith("ragmcp_"):
            raise ValueError("personal_token has an invalid format")
        return self


class UnbindWechatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: UUID
    profile_name: str = Field(pattern=PROFILE_PATTERN.pattern)
    external_user_id: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_identity(self) -> UnbindWechatRequest:
        if self.profile_name != f"employee-{self.employee_id.hex}":
            raise ValueError("profile_name does not match employee_id")
        return self


class WechatBindingResponse(BaseModel):
    external_user_id: str
    profile_name: str
    bound_at: datetime


class ProvisionerHealth(BaseModel):
    status: str
