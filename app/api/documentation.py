"""Swagger 설명과 가상 요청·응답 예제. 실제 계정이나 토큰을 포함하지 않는다."""
from typing import Any

from app.schemas.common import ErrorResponse

API_DESCRIPTION = """
MOA 프론트엔드 연동을 위한 인증·기업 소속 API입니다.

## 처음 연결하는 순서
1. **회원가입**: `POST /api/v1/auth/signup`. 역할은 `customer`(고객), `agent`(담당자), `admin`(관리자)입니다.
2. **로그인**: `POST /api/v1/auth/login`. 성공 시 `access_token`, `refresh_token` 두 값만 반환합니다.
3. 오른쪽 위 **Authorize**에 로그인 응답의 **access_token 값만** 입력합니다. `Bearer ` 접두어는 Swagger가 붙입니다.
4. **내 정보**: `GET /api/v1/auth/me`로 사용자 ID·이메일·역할·기업 소속을 확인합니다. 미소속이면 `tenant_id`는 `null`입니다.
5. **기업 연결**: 미소속 관리자는 기업 생성 API를, 나머지 사용자는 기업 ID로 소속 연결 API를 호출합니다. 연결 후 기존 access token으로 내 정보를 다시 조회할 수 있습니다.

## 토큰 처리
- 인증이 필요한 API는 `Authorization: Bearer <access_token>`을 사용합니다.
- 회원가입·로그인·토큰 갱신·헬스체크에는 access token이 필요하지 않습니다.
- 갱신할 때는 `POST /api/v1/auth/refresh` 본문에 **refresh_token**을 보냅니다. 성공하면 **두 토큰을 함께 교체**합니다.
- 이전 refresh token은 재사용할 수 없습니다. 같은 토큰의 동시 갱신은 한 요청만 성공합니다.
- 기본 만료 설정은 access token 1시간, refresh token 7일이며 서버 설정에 따라 달라질 수 있습니다. 갱신 전 access token은 자체 만료까지 유효합니다.
- 로그인·갱신 응답에는 사용자 정보나 `expires_in`, `token_type` 필드가 없습니다. 사용자 정보는 내 정보 API로 조회합니다.

## 응답·오류 처리
- JSON 필드는 `snake_case`입니다. 사용자 ID는 숫자, 기업 ID는 문자열입니다.
- 입력 검증 실패는 **422가 아닌 400**으로 반환합니다.
- `401`: 로그인 정보 또는 토큰 확인이 필요합니다. `403`: 역할·기업 소속 조건을 확인합니다.
- `404`: 기업을 찾을 수 없습니다. `409`: 이메일·기업 ID 중복 또는 이미 정해진 소속과 충돌합니다.
- 오류는 `error.code`, `error.message`, `error.request_id` 구조입니다. 요청 추적 ID는 `X-Request-ID` 헤더에도 반환합니다.

**모든 예제는 가상 데이터입니다.** 예시 계정·기업이 서버에 미리 생성되어 있지 않으며, 예시 토큰은 인증에 사용할 수 없습니다. 실제 로그인 응답의 토큰을 사용하세요.
"""

OPENAPI_TAGS = [
    {"name": "auth", "description": "회원가입·로그인·토큰 갱신·내 정보 조회. 고객·담당자·관리자 모두 같은 인증 흐름을 사용합니다."},
    {"name": "tenants", "description": "기업 생성·본인 소속 연결·내 기업 조회. 사용자 한 명은 한 기업에만 소속됩니다."},
    {"name": "inquiries", "description": "customer 전용 문의 접수·추가 메시지·cursor 폴링. 문의·메시지 저장에 Idempotency-Key가 필요합니다. AI 호출 없이 동작합니다."},
    {"name": "attachments", "description": "기업·업로더 권한을 확인하는 비공개 S3 첨부. 업로드 후 숫자 ID를 문의에 연결하고, 다운로드 URL은 만료 시 재발급합니다."},
    {"name": "system", "description": "서버 응답 상태 확인. 헬스체크는 DB 연결 상태를 검사하지 않습니다."},
]


SIGNUP_EXAMPLES = {
    role: {"summary": label, "value": {"email": f"{role}@example.com", "password": "example-password", "role": role}}
    for role, label in [("customer", "고객 회원가입"), ("agent", "담당자 회원가입"), ("admin", "관리자 회원가입")]
}
LOGIN_EXAMPLES = {
    role: {"summary": label, "value": {"email": f"{role}@example.com", "password": "example-password"}}
    for role, label in [("customer", "고객 로그인"), ("admin", "관리자 로그인")]
}
REFRESH_EXAMPLES = {"refresh": {"summary": "실제 refresh_token으로 교체", "value": {"refresh_token": "refresh-token-from-login-response"}}}
TENANT_CREATE_EXAMPLES = {"syncnote": {"summary": "관리자의 기업 생성", "value": {"tenant_id": "syncnote", "name": "싱크노트"}}}
TENANT_JOIN_EXAMPLES = {"syncnote": {"summary": "기존 기업에 본인 소속 연결", "value": {"tenant_id": "syncnote"}}}
TOKEN_EXAMPLE = {"access_token": "eyJhbGciOiJIUzI1NiJ9.example.signature", "refresh_token": "refresh-token-example-replace-with-real-value"}
TENANT_EXAMPLE = {"tenant_id": "syncnote", "name": "싱크노트"}

ERROR_DOCUMENTATION = {
    "IDEMPOTENCY_CONFLICT": (409, "같은 Idempotency-Key로 다른 요청을 보낼 수 없습니다.", "중복 키의 요청 본문 불일치"),
    "ATTACHMENT_ALREADY_LINKED": (409, "이미 메시지에 연결된 첨부입니다.", "이미 사용 중인 첨부"),
    "INQUIRY_NOT_FOUND": (404, "문의를 찾을 수 없습니다.", "문의 없음 또는 다른 기업·고객의 문의"),
    "INQUIRY_CLOSED": (409, "종료된 문의에는 메시지를 추가할 수 없습니다. 새 문의를 생성해 주세요.", "종료된 상담에 메시지 추가"),
    "INVALID_CURSOR": (400, "유효한 조회 cursor가 필요합니다.", "cursor 변조·조회 대상·필터 불일치"),
    "INVALID_FILE": (400, "파일 이름 또는 내용이 올바르지 않습니다.", "빈 파일·잘못된 파일명"),
    "FILE_TOO_LARGE": (413, "파일은 10MiB 이하여야 합니다.", "파일 용량 초과"),
    "UNSUPPORTED_FILE_TYPE": (415, "PNG, JPEG, PDF 파일만 업로드할 수 있습니다.", "확장자·MIME·파일 시그니처 불일치"),
    "STORAGE_UNAVAILABLE": (503, "첨부 저장소를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.", "S3 설정·자격 증명·저장소 오류"),
    "ATTACHMENT_NOT_FOUND": (404, "첨부를 찾을 수 없습니다.", "첨부 없음 또는 타인의 첨부"),
    "VALIDATION_ERROR": (400, "요청 값이 올바르지 않습니다.", "필수값 누락·형식·길이·역할·추가 필드 오류"),
    "EMAIL_ALREADY_EXISTS": (409, "이미 등록된 이메일입니다.", "대소문자를 포함해 동일한 이메일이 이미 존재함"),
    "INVALID_CREDENTIALS": (401, "이메일 또는 비밀번호가 올바르지 않습니다.", "미등록 이메일 또는 잘못된 비밀번호"),
    "INVALID_REFRESH_TOKEN": (401, "유효한 갱신 토큰이 필요합니다.", "만료·알 수 없음·이미 사용한 refresh token"),
    "INVALID_ACCESS_TOKEN": (401, "유효한 인증 정보가 필요합니다.", "access token 누락·만료·서명 또는 사용자 확인 실패"),
    "FORBIDDEN": (403, "접근 권한이 없습니다.", "현재 역할로 허용되지 않는 작업"),
    "TENANT_ALREADY_EXISTS": (409, "이미 등록된 기업 ID입니다.", "기업 ID 중복"),
    "TENANT_ALREADY_ASSIGNED": (409, "이미 기업에 소속되어 있습니다.", "기존 소속과 충돌하는 생성·연결 요청"),
    "TENANT_NOT_FOUND": (404, "기업을 찾을 수 없습니다.", "요청한 기업 또는 연결된 기업을 찾을 수 없음"),
    "TENANT_REQUIRED": (403, "기업 소속이 필요합니다.", "기업 미소속 상태에서 내 기업 조회"),
}


def request_id_header() -> dict[str, Any]:
    return {"description": "서버가 생성한 요청 추적 ID", "schema": {"type": "string"}}


def documented_errors(*codes: str, bearer_auth: bool = False) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {
        "4XX": {
            "model": ErrorResponse,
            "description": "공통 클라이언트 오류 구조. 구체적인 상태·코드는 아래 예제를 참고하세요. 입력 검증 오류는 400으로 처리합니다.",
        },
    }
    for code in codes:
        status_code, message, summary = ERROR_DOCUMENTATION[code]
        if status_code not in responses:
            responses[status_code] = {
                "model": ErrorResponse,
                "description": f"{status_code} 오류. error.code로 원인을 구분합니다.",
                "headers": {"X-Request-ID": request_id_header()},
                "content": {"application/json": {"examples": {}}},
            }
            if bearer_auth and status_code == 401:
                responses[status_code]["headers"]["WWW-Authenticate"] = {
                    "description": "Bearer 인증 필요", "schema": {"type": "string", "example": "Bearer"},
                }
        responses[status_code]["content"]["application/json"]["examples"][code] = {
            "summary": summary,
            "value": {"error": {"code": code, "message": message, "request_id": "REQ-00000000000000000000000000000001"}},
        }
    return responses


def documented_success(description: str, examples: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "description": description,
        "headers": {"X-Request-ID": request_id_header()},
        "content": {"application/json": {"examples": examples}},
    }
