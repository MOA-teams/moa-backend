from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

TenantId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z]+$")]


class TenantCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: TenantId
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class TenantJoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: TenantId


class TenantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    name: str
