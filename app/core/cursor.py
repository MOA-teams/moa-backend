"""인증 토큰과 용도를 분리한 서명 cursor. 조회 대상·필터를 함께 서명한다."""
import base64
import binascii
import hashlib
import hmac
import json

from app.core.config import get_settings
from app.core.errors import APIError


def signing_key() -> bytes:
    secret = get_settings().jwt_secret_key.get_secret_value().encode()
    return hmac.digest(secret, b"moa-pagination-cursor-v2", "sha256")


def encode_cursor(scope: dict, position: dict) -> str:
    payload = json.dumps({"v": 2, "scope": scope, "position": position}, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.digest(signing_key(), payload, "sha256")
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def decode_cursor(value: str | None, scope: dict) -> dict | None:
    if value is None:
        return None
    try:
        if len(value) > 2048:
            raise ValueError
        encoded, signed = value.split(".")
        payload = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        signature = base64.b64decode(signed + "=" * (-len(signed) % 4), altchars=b"-_", validate=True)
        if not hmac.compare_digest(signature, hmac.digest(signing_key(), payload, "sha256")):
            raise ValueError
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or decoded.get("v") != 2 or decoded.get("scope") != scope or not isinstance(decoded.get("position"), dict):
            raise ValueError
        return decoded["position"]
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        raise APIError(400, "INVALID_CURSOR", "유효한 조회 cursor가 필요합니다.") from None
