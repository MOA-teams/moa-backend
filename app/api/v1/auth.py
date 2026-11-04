from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.deps.db import get_db
from app.schemas.auth import SignupRequest, SignupResponse
from app.services.auth import EmailAlreadyExistsError, signup

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    response_model=SignupResponse,
    responses={
        400: {"description": "입력 검증 실패"},
        409: {"description": "이미 등록된 이메일"},
        "4XX": {"description": "공통 오류 응답"},
    },
)
async def signup_user(
    payload: SignupRequest,
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
