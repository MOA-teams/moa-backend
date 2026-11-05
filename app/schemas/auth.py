from typing import Annotated

from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.core.enums import UserRole


class SignupRequest(BaseModel):
    """회원가입 입력. 사용자 ID와 기업 소속은 여기서 지정하지 않습니다."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(description="로그인 이메일. 소문자로 정규화되며 모든 기업에서 중복될 수 없습니다.", examples=["customer@example.com"])
    password: Annotated[SecretStr, Field(min_length=8, max_length=128, description="비밀번호 8~128자. 앞뒤 공백도 비밀번호의 일부로 유지됩니다.", examples=["example-password"])]
    role: UserRole = Field(description="customer: 고객, agent: 담당자, admin: 관리자. 현재 정책에서는 가입 시 세 역할을 자유롭게 선택합니다.", examples=["customer"])

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class SignupResponse(BaseModel):
    """가입한 계정의 공개 정보. 토큰은 로그인 API에서 발급받습니다."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str = Field(validation_alias=AliasChoices("public_id", "user_id"), description="외부 문자열 ID", examples=["U-2001"])
    email: EmailStr = Field(description="소문자로 정규화된 이메일", examples=["customer@example.com"])
    role: UserRole = Field(description="계정에 저장된 역할", examples=["customer"])


class LoginRequest(BaseModel):
    """세 역할이 공통으로 사용하는 JSON 로그인 입력."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(description="가입한 이메일. 대소문자를 구분하지 않습니다.", examples=["customer@example.com"])
    password: Annotated[SecretStr, Field(min_length=1, max_length=128, description="가입 시 설정한 비밀번호. 앞뒤 공백을 제거하지 말고 전달합니다.", examples=["example-password"])]

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class RefreshRequest(BaseModel):
    """Authorization 헤더 대신 본문으로 전달하는 refresh token."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: Annotated[SecretStr, Field(min_length=1, max_length=512, description="로그인 또는 직전 갱신 응답의 refresh_token 원문. 성공한 갱신에 사용한 이전 값은 재사용할 수 없습니다.", examples=["refresh-token-from-login-response"])]


class TokenResponse(BaseModel):
    """로그인·갱신 응답. 다른 필드 없이 두 토큰만 반환합니다."""

    model_config = ConfigDict(from_attributes=True)

    access_token: str = Field(repr=False, description="보호 API의 Authorization: Bearer 헤더에 사용할 JWT. Swagger Authorize에는 이 값만 입력합니다.", examples=["eyJhbGciOiJIUzI1NiJ9.example.signature"])
    refresh_token: str = Field(repr=False, description="토큰 갱신 API 본문에 사용할 난수 토큰. 새 응답을 받으면 이전 값과 교체합니다.", examples=["refresh-token-example-replace-with-real-value"])


class AuthenticatedUser(BaseModel):
    internal_user_id: int = Field(exclude=True, repr=False)
    """현재 인증 사용자. 비밀번호·해시·토큰은 포함하지 않습니다."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str = Field(validation_alias=AliasChoices("public_id", "user_id"), description="외부 문자열 ID", examples=["U-2001"])
    email: EmailStr = Field(description="현재 계정의 이메일", examples=["customer@example.com"])
    role: UserRole = Field(description="DB에서 확인한 현재 역할. 화면 분기와 접근 조건 확인에 사용합니다.", examples=["customer"])
    tenant_id: str | None = Field(default=None, description="현재 소속 기업 ID. 미소속이면 null이며 기업 연결 후 기존 토큰으로 재조회할 수 있습니다.", examples=[None, "syncnote"])
