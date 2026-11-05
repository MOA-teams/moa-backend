from secrets import compare_digest
from typing import Annotated
from fastapi import Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.config import get_settings
from app.core.errors import APIError

service_bearer=HTTPBearer(auto_error=False,scheme_name='PipelineServiceToken',description='AI 서버 전용 서비스 토큰. 고객 JWT가 아닙니다.')

async def pipeline_tenant(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(service_bearer)],
                          tenant: Annotated[str, Header(alias='X-Tenant-ID',pattern=r'^[a-z]{1,64}$')]):
    expected=get_settings().ai_callback_token
    if expected is None or not expected.get_secret_value():
        raise APIError(503,'CALLBACK_NOT_CONFIGURED','서비스 인증 설정이 필요합니다.')
    if credentials is None or not compare_digest(credentials.credentials.encode(),expected.get_secret_value().encode()):
        raise APIError(401,'INVALID_SERVICE_TOKEN','유효한 서비스 인증이 필요합니다.',headers={'WWW-Authenticate':'Bearer'})
    return tenant
