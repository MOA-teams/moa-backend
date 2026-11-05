"""Swagger 설명과 가상 요청·응답 예제. 실제 계정이나 토큰을 포함하지 않는다."""
from typing import Any

from app.schemas.common import ErrorResponse

API_DESCRIPTION = """
MOA 프론트엔드 연동을 위한 인증·기업 소속·문의·채팅·S3 첨부 API입니다.

## 처음 연결하는 순서
1. **회원가입**: `POST /api/v1/auth/signup`. 역할은 `customer`(고객), `agent`(담당자), `admin`(관리자)입니다.
2. **로그인**: `POST /api/v1/auth/login`. 성공 시 `access_token`, `refresh_token` 두 값만 반환합니다.
3. 오른쪽 위 **Authorize**에 로그인 응답의 **access_token 값만** 입력합니다. `Bearer ` 접두어는 Swagger가 붙입니다.
4. **내 정보**: `GET /api/v1/auth/me`로 사용자 ID·이메일·역할·기업 소속을 확인합니다. 미소속이면 `tenant_id`는 `null`입니다.
5. **기업 연결**: 미소속 관리자는 기업 생성 API를, 나머지 사용자는 기업 ID로 소속 연결 API를 호출합니다. 연결 후 기존 access token으로 내 정보를 다시 조회할 수 있습니다.

## 문의·첨부 연동 순서
1. customer 계정을 기업에 연결합니다.
2. 첨부가 있으면 `POST /api/v1/attachments`에 multipart `file`로 업로드하고 문자열 `attachment_id`를 받습니다.
3. `POST /api/v1/inquiries`에 첫 메시지와 첨부 ID 배열을 보냅니다. **Idempotency-Key** 헤더에 요청별 UUID를 넣으세요.
4. `GET /api/v1/inquiries/{id}`에서 받은 `next_cursor`로 다음 이력을 읽습니다. `has_more=false`이면 3초 뒤 폴링합니다.
5. 추가 메시지에는 새 Idempotency-Key를 사용하고, 네트워크 재시도에는 원래 키와 본문을 유지합니다.
6. 첨부 다운로드 직전에 `GET /api/v1/attachments/{id}`로 기본 300초의 다운로드 URL을 발급받습니다.
7. agent/admin은 `/api/v1/admin/inquiries`에서 본인 기업의 목록과 상세를 조회합니다.

도메인 Enum은 계약의 한글 값이며 문의·메시지·첨부 ID는 접두어를 포함한 문자열입니다. 문의 저장과 함께 AI 분석 요청을 보존합니다. 결과는 담당자 상세에서 확인하며 답변 발송·상담 종료는 후속 단계입니다.

## 지식 문서 연동 순서
1. 같은 기업의 agent/admin이 `/api/v1/admin/documents`를 사용합니다.
2. 파일은 첨부 API에 multipart `purpose=document`로 먼저 업로드합니다. 직접 입력이면 `text`를 사용합니다.
3. 신규 문서는 doc_id를 생략하고 version=1, 이후 기존 DOC-xxx ID와 다음 version을 보냅니다. Idempotency-Key와 타임존이 있는 created_at이 필요합니다.
4. 202 이후 목록의 indexing_status·indexing_error·sync_error·searchable을 확인합니다. 문서 worker가 별도로 실행되어야 합니다.
5. 상태 변경은 PATCH를 사용합니다. 503이면 저장된 작업의 검색 상태 확인이 남아 있으므로 같은 요청으로 재시도합니다.

## 토큰 처리
- 인증이 필요한 API는 `Authorization: Bearer <access_token>`을 사용합니다.
- 회원가입·로그인·토큰 갱신·헬스체크에는 access token이 필요하지 않습니다.
- 갱신할 때는 `POST /api/v1/auth/refresh` 본문에 **refresh_token**을 보냅니다. 성공하면 **두 토큰을 함께 교체**합니다.
- 이전 refresh token은 재사용할 수 없습니다. 같은 토큰의 동시 갱신은 한 요청만 성공합니다.
- 기본 만료 설정은 access token 1시간, refresh token 7일이며 서버 설정에 따라 달라질 수 있습니다. 갱신 전 access token은 자체 만료까지 유효합니다.
- 로그인·갱신 응답에는 사용자 정보나 `expires_in`, `token_type` 필드가 없습니다. 사용자 정보는 내 정보 API로 조회합니다.

## 응답·오류 처리
- JSON 필드는 `snake_case`입니다. 사용자·기업 ID는 문자열입니다.
- 입력 검증 실패는 **422가 아닌 400**으로 반환합니다.
- `401`: 로그인 정보 또는 토큰 확인이 필요합니다. `403`: 역할·기업 소속 조건을 확인합니다.
- `404`: 기업을 찾을 수 없습니다. `409`: 이메일·기업 ID 중복 또는 이미 정해진 소속과 충돌합니다.
- 오류는 `error.code`, `error.message`, `error.request_id` 구조입니다. 요청 추적 ID는 `X-Request-ID` 헤더에도 반환합니다.

**모든 예제는 가상 데이터입니다.** 예시 계정·기업이 서버에 미리 생성되어 있지 않으며, 예시 토큰은 인증에 사용할 수 없습니다. 실제 로그인 응답의 토큰을 사용하세요.
"""

OPENAPI_TAGS = [
    {"name": "documents", "description": "같은 기업 agent/admin의 문서 원본·불변 버전 관리와 RAG 인덱싱 상태 조회."},
    {"name": "auth", "description": "회원가입·로그인·토큰 갱신·내 정보 조회. 고객·담당자·관리자 모두 같은 인증 흐름을 사용합니다."},
    {"name": "tenants", "description": "기업 생성·본인 소속 연결·내 기업 조회. 사용자 한 명은 한 기업에만 소속됩니다."},
    {"name": "admin-inquiries", "description": "같은 기업 agent/admin 전용 목록·상세 조회. 내부 필드는 고객 채팅 응답과 분리합니다."},
    {"name": "inquiries", "description": "customer 전용 문의 접수·추가 메시지·cursor 폴링. 문의·메시지 저장에 Idempotency-Key가 필요합니다. AI 완료를 기다리지 않고 접수하며 분석 요청은 별도로 처리합니다."},
    {"name": "attachments", "description": "기업·업로더 권한을 확인하는 비공개 S3 첨부. 업로드 후 문자열 ID를 문의에 연결하고, 다운로드 URL은 만료 시 재발급합니다."},
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
    "DOCUMENT_NOT_FOUND": (404, "문서 또는 버전을 찾을 수 없습니다.", "문서 없음 또는 다른 기업의 문서"),
    "DOCUMENT_ID_EXHAUSTED": (409, "이 기업의 AI 문서 ID 999개를 모두 사용했습니다.", "AI 문서 ID 한도 소진"),
    "DOCUMENT_VERSION_CONFLICT": (409, "문서 버전이 현재 상태와 충돌합니다.", "중복·건너뛴 버전 또는 이전 버전 활성화"),
    "DOCUMENT_SYNC_PENDING": (409, "이전 상태 변경을 먼저 동기화해야 합니다.", "미확정 검색 차단이 남아 있음"),
    "DOCUMENT_SYNC_FAILED": (503, "검색 상태 반영을 확인하지 못했습니다. 같은 요청으로 재시도하세요.", "원본·작업은 보존되었으나 AI 반영 확인 실패"),
    "IDEMPOTENCY_CONFLICT": (409, "같은 Idempotency-Key로 다른 요청을 보낼 수 없습니다.", "중복 키의 요청 본문 불일치"),
    "ATTACHMENT_ALREADY_LINKED": (409, "이미 메시지에 연결된 첨부입니다.", "이미 사용 중인 첨부"),
    "INQUIRY_NOT_FOUND": (404, "문의를 찾을 수 없습니다.", "문의 없음 또는 다른 기업·고객의 문의"),
    "MESSAGE_NOT_FOUND": (404, "메시지를 찾을 수 없습니다.", "메시지 없음 또는 해당 문의에 속하지 않는 메시지"),
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


ERROR_DOCUMENTATION.update({
    "STALE_INQUIRY_REVISION": (409, "최신 문의를 다시 조회해 주세요.", "문의 revision 또는 기준 메시지 충돌"),
    "ID_SPACE_EXHAUSTED": (503, "발급 가능한 ID 범위를 초과했습니다.", "계약 ID 범위 소진"),
    "LEGACY_IDEMPOTENCY_KEY": (409, "이전 API 계약의 요청입니다. 처리 결과를 확인해 주세요.", "계약 전환 전 요청 키"),
    "INVALID_SERVICE_TOKEN": (401, "유효한 서비스 인증이 필요합니다.", "AI 서비스 토큰 오류"),
    "CALLBACK_NOT_CONFIGURED": (503, "서비스 인증 설정이 필요합니다.", "콜백 수신 토큰 미설정"),
    "RUN_NOT_REGISTERED": (409, "실행 등록 후 재시도해 주세요.", "AI 접수 등록보다 콜백이 먼저 도착함"),
    "PIPELINE_IDENTITY_MISMATCH": (409, "등록된 분석 요청과 일치하지 않습니다.", "문의·revision·기준 메시지 불일치"),
    "PIPELINE_RESULT_CONFLICT": (409, "이미 저장된 최종 결과와 다릅니다.", "같은 run의 다른 최종 결과"),
    "NON_TERMINAL_RESULT": (400, "완료 또는 실패 결과만 수신합니다.", "진행 중 결과를 콜백으로 전송함"),
})


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
