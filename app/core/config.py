from urllib.parse import urlsplit
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.core.auto_response import AutoResponsePolicy, TenantID

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    jwt_secret_key: SecretStr
    access_token_expire_seconds: int = Field(default=3600, gt=0)
    refresh_token_expire_days: int = Field(default=7, gt=0)
    ai_base_url: str | None = None
    ai_service_token: SecretStr | None = None
    ai_callback_token: SecretStr | None = None
    ai_pipeline_worker_enabled: bool = False
    auto_response_policies: dict[TenantID, AutoResponsePolicy] = Field(default_factory=dict)
    aws_region: str | None = None
    s3_bucket_name: str | None = None
    s3_presigned_url_expire_seconds: int = Field(default=300, gt=0, le=3600)

    @field_validator("ai_base_url")
    @classmethod
    def validate_ai_url(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("AI 서버의 http(s) 기본 주소가 필요합니다.")
        return value

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET_KEY는 32바이트 이상이어야 합니다.")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
