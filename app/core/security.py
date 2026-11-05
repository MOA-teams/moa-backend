import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from uuid import uuid4

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.core.config import get_settings
from app.core.enums import UserRole

password_hasher = PasswordHash.recommended()
# 미등록 이메일도 비밀번호 검증을 거쳐 실패 경로의 연산량을 맞춘다.
DUMMY_PASSWORD_HASH = password_hasher.hash("not-an-account-password")


class InvalidAccessTokenError(Exception):
    pass


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: str
    role: UserRole


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return password_hasher.verify(password, hashed_password)
    except UnknownHashError:
        return False


def create_access_token(user_id: str, role: UserRole) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id), "role": role.value, "token_type": "access",
            "iat": now,
            "exp": now + timedelta(seconds=settings.access_token_expire_seconds),
            "jti": uuid4().hex,
        },
        settings.jwt_secret_key.get_secret_value(), algorithm="HS256",
    )


def decode_access_token(token: str) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token, get_settings().jwt_secret_key.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["sub", "role", "token_type", "iat", "exp", "jti"]},
        )
        if payload["token_type"] != "access":
            raise InvalidAccessTokenError
        subject = payload["sub"]
        if not isinstance(subject, str) or not re.fullmatch(r"(?:U-[0-9]{4}|agent_[0-9]{2}|admin_[0-9]{2})", subject):
            raise InvalidAccessTokenError
        user_id = subject
        return AccessTokenClaims(user_id=user_id, role=UserRole(payload["role"]))
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise InvalidAccessTokenError from None


def create_refresh_token() -> str:
    return token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()
