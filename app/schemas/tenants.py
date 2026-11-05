from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

TenantId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z]+$")]


class TenantCreateRequest(BaseModel):
    """미소속 관리자의 기업 생성 입력."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: TenantId = Field(description="직접 지정하는 고유 기업 ID. 영문 소문자 a~z만 1~64자 사용하며 숫자·공백·하이픈은 허용하지 않습니다.", examples=["syncnote"])
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] = Field(description="기업 표시 이름. 앞뒤 공백을 제거한 결과가 1~100자여야 합니다.", examples=["싱크노트"])


class TenantJoinRequest(BaseModel):
    """인증된 본인의 기업 연결 입력. 다른 사용자의 ID는 받지 않습니다."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: TenantId = Field(description="연결할 기존 기업 ID. 현재는 기업 ID를 아는 인증 사용자라면 초대·승인 없이 연결할 수 있습니다.", examples=["syncnote"])


class TenantResponse(BaseModel):
    """생성·연결·조회한 기업의 공개 정보."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: str = Field(description="기업 식별 문자열. 사용자 숫자 ID와 구분합니다.", examples=["syncnote"])
    name: str = Field(description="앞뒤 공백이 정리된 기업 표시 이름", examples=["싱크노트"])
