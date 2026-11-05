from typing import Annotated

from fastapi import APIRouter, Body, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documentation import (
    LOGIN_EXAMPLES, REFRESH_EXAMPLES, SIGNUP_EXAMPLES, TOKEN_EXAMPLE,
    documented_errors, documented_success,
)
from app.core.errors import APIError
from app.deps.auth import get_user
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser, LoginRequest, RefreshRequest, SignupRequest, SignupResponse, TokenResponse
from app.services.auth import (
    EmailAlreadyExistsError, InvalidCredentialsError, InvalidRefreshTokenError,
    login, refresh_tokens, signup,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/signup", status_code=status.HTTP_201_CREATED, response_model=SignupResponse,
    summary="회원가입",
    description="""
고객·담당자·관리자 계정을 생성합니다. 인증 헤더는 필요하지 않습니다.

- `role`: `customer`(고객), `agent`(담당자), `admin`(관리자). 현재 정책에서는 자유 선택입니다.
- 이메일은 소문자로 정규화되며 기업에 관계없이 중복을 허용하지 않습니다.
- 비밀번호는 8~128자이며 앞뒤 공백도 유지합니다.
- `user_id`는 서버가 발급합니다. `tenant_id`나 다른 추가 필드는 보내지 않습니다.
- 가입 응답에는 토큰이 없습니다. 가입 후 로그인 API를 호출하세요.
- 기업 연결은 로그인 후 기업 생성 또는 소속 연결 API에서 별도로 진행합니다.
""",
    response_description="생성된 계정의 숫자 ID·이메일·역할",
    responses={
        **documented_errors("VALIDATION_ERROR", "EMAIL_ALREADY_EXISTS"),
        201: documented_success("계정 생성 완료. 자동 로그인은 수행하지 않습니다.", {
            "created": {"summary": "고객 계정 생성", "value": {"user_id": 1, "email": "customer@example.com", "role": "customer"}},
        }),
    },
)
async def signup_user(
    payload: Annotated[SignupRequest, Body(openapi_examples=SIGNUP_EXAMPLES)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> SignupResponse:
    try:
        user = await signup(
            session, email=payload.email,
            password=payload.password.get_secret_value(), role=payload.role,
        )
    except EmailAlreadyExistsError:
        raise APIError(409, "EMAIL_ALREADY_EXISTS", "이미 등록된 이메일입니다.") from None
    return SignupResponse.model_validate(user)


@router.post(
    "/login", response_model=TokenResponse,
    summary="로그인 및 토큰 발급",
    description="""
이메일·비밀번호로 로그인하고 access/refresh token을 발급합니다. 인증 헤더는 필요하지 않습니다.

- 세 역할 모두 같은 API를 사용합니다. 요청에 `role`을 보내지 않습니다.
- 이메일 대소문자는 구분하지 않습니다. 비밀번호 공백은 그대로 전달합니다.
- 성공 응답은 `access_token`, `refresh_token` 두 필드뿐입니다.
- Swagger의 **Authorize**에는 반환된 **access_token 값만** 입력합니다.
- 사용자 역할·기업 소속이 필요하면 `GET /api/v1/auth/me`를 호출합니다.
- 미등록 이메일과 잘못된 비밀번호는 동일한 `401 INVALID_CREDENTIALS`로 응답합니다.
""",
    response_description="발급된 access/refresh token",
    responses={
        **documented_errors("VALIDATION_ERROR", "INVALID_CREDENTIALS"),
        200: documented_success("로그인 성공. 예시가 아닌 실제 응답 토큰을 사용하세요.", {
            "tokens": {"summary": "두 토큰 발급", "value": TOKEN_EXAMPLE},
        }),
    },
)
async def login_user(
    payload: Annotated[LoginRequest, Body(openapi_examples=LOGIN_EXAMPLES)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    try:
        pair = await login(session, email=payload.email, password=payload.password.get_secret_value())
    except InvalidCredentialsError:
        raise APIError(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.") from None
    return TokenResponse.model_validate(pair)


@router.post(
    "/refresh", response_model=TokenResponse,
    summary="토큰 갱신",
    description="""
유효한 refresh token으로 새 access/refresh token을 발급합니다. Authorization 헤더는 필요하지 않습니다.

- 로그인 또는 직전 갱신 응답의 `refresh_token`을 JSON 본문으로 보냅니다.
- 성공하면 저장해 둔 **두 토큰을 모두 새 응답 값으로 교체**합니다.
- 이전 refresh token은 즉시 재사용할 수 없으며, 같은 토큰의 동시 갱신은 한 요청만 성공합니다.
- 다른 로그인 세션의 refresh token은 유지됩니다. 기존 access token도 자체 만료까지 유효합니다.
- `401 INVALID_REFRESH_TOKEN`이면 만료·재사용·잘못된 토큰 여부를 확인하고 다시 로그인합니다.
""",
    response_description="교체된 두 토큰. 이전 refresh token은 재사용 불가",
    responses={
        **documented_errors("VALIDATION_ERROR", "INVALID_REFRESH_TOKEN"),
        200: documented_success("갱신 성공. 기존 두 토큰을 함께 교체하세요.", {
            "tokens": {"summary": "새 토큰 쌍", "value": TOKEN_EXAMPLE},
        }),
    },
)
async def refresh_user_tokens(
    payload: Annotated[RefreshRequest, Body(openapi_examples=REFRESH_EXAMPLES)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    try:
        pair = await refresh_tokens(session, refresh_token=payload.refresh_token.get_secret_value())
    except InvalidRefreshTokenError:
        raise APIError(401, "INVALID_REFRESH_TOKEN", "유효한 갱신 토큰이 필요합니다.") from None
    return TokenResponse.model_validate(pair)


@router.get(
    "/me", response_model=AuthenticatedUser,
    summary="내 정보 조회",
    description="""
현재 access token의 사용자를 조회합니다. **customer·agent·admin 모두 인증이 필요**합니다.

- `Authorization: Bearer <access_token>` 헤더를 사용합니다.
- 사용자 정보와 기업 소속은 서버 DB에서 확인합니다. 사용자·기업 ID를 요청으로 보내지 않습니다.
- 미소속이면 `tenant_id`는 `null`입니다.
- 기업 연결 후에도 기존 access token으로 재조회하면 현재 소속을 확인할 수 있습니다.
- 비밀번호·해시·토큰은 응답에 포함하지 않습니다.
""",
    response_description="인증 사용자의 공개 정보와 nullable 기업 ID",
    responses={
        **documented_errors("INVALID_ACCESS_TOKEN", bearer_auth=True),
        200: documented_success("현재 계정 정보", {
            "unassigned": {"summary": "기업 미소속", "value": {"user_id": 1, "email": "customer@example.com", "role": "customer", "tenant_id": None}},
            "assigned": {"summary": "기업 소속", "value": {"user_id": 1, "email": "customer@example.com", "role": "customer", "tenant_id": "syncnote"}},
        }),
    },
)
async def get_my_info(
    user: Annotated[AuthenticatedUser, Depends(get_user)],
) -> AuthenticatedUser:
    return user
