# 싱크노트 문의 진입·로그인 연동 안내

2026-11-05 합의: **기존 고객 로그인을 유지한다.** 싱크노트의 cid는 외부 참고 정보이며 인증 수단으로 쓰지 않는다.
백엔드 구현과 프론트 담당자 전달용 문서만 반영했다. moa-client의 화면·리다이렉트·API 어댑터는 이 작업에서 수정하지 않았다.

## 1. 확정 흐름

```text
싱크노트 문의하기
→ /chat?cid=U-2001&error_code=E-502&os=iOS%2018&app_version=2.4.0
→ 미로그인이면 /login?next=<query를 포함한 /chat 주소 전체를 인코딩>
→ 필요 시 회원가입, 같은 next 보존
→ 로그인 후 원래 /chat 주소로 복귀
→ 기존 위젯 기업 연결
→ 전달된 환경·오류 정보를 포함해 문의 생성
```

- 이미 로그인한 고객도 같은 query 파싱을 거친다.
- MOA의 숫자 user_id는 JWT와 DB 사용자 조회로 결정한다. 기업도 로그인 사용자의 DB 소속으로 결정한다.
- cid를 user_id/customer_id로 변환하거나 cid로 계정을 찾고 토큰을 발급하지 않는다.
- 기존 다른 기업 소속 계정은 현재 프론트의 기업 불일치 안내를 유지한다. query로 기업을 이동시키지 않는다.
- 고객용 자동 로그인 API와 로그인·토큰 갱신·회원가입 형식 변경은 없다.

## 2. 백엔드 API 계약

문의 생성: `POST /api/v1/inquiries`

```http
Authorization: Bearer <로그인한 고객의 access_token>
Content-Type: application/json
Idempotency-Key: <이번 문의 생성 요청의 UUID>
```

```json
{
  "message": "저장할 때 오류가 발생해요.",
  "source_customer_id": "U-2001",
  "customer_category": "bug",
  "env": {
    "os": "iOS 18",
    "app_version": "2.4.0",
    "error_code": "E-502"
  },
  "attachments": []
}
```

| 항목 | 처리 |
|---|---|
| source_customer_id | cid를 문자열 그대로 전달. 선택·null 허용, 최대 128자. 숫자 변환·앞자리 0 제거·임의 자르기 금지 |
| customer_id / tenant_id | 요청 본문으로 보내지 않음. 인증 사용자로 결정 |
| env.os | 전달된 os 우선, 없으면 브라우저에서 감지한 OS |
| env.app_version | 전달된 app_version 우선. 알 수 없으면 null |
| env.error_code | 전달된 error_code 우선. 알 수 없으면 null. 브라우저에서 오류 코드를 만들어 채우지 않음 |
| customer_category | 영어 Enum. 미선택이면 other 또는 생략. null은 보내지 않음 |
| attachments | 업로드 API 응답의 숫자 attachment_id 배열. 프론트 파일 객체·URL·문자열 ID를 보내지 않음 |

- 메시지는 1~10000자이며 공백만 있는 본문은 거절한다. 첨부는 최대 10개다.
- env의 허용 필드는 os(최대 200자), app_version(최대 100자), error_code(최대 100자)다.
- 현재 프론트 타입의 browser는 UI 표시에 사용할 수 있지만 env에 그대로 추가하면 400이다. 타입 객체 전체를 펼쳐 전송하지 말고 위 필드만 명시적으로 매핑한다.
- 같은 논리 요청 재시도는 원래 Idempotency-Key와 본문을 유지한다. cid를 포함한 본문을 변경하면 새 키를 사용한다.
- 선택 필드가 없던 기존 요청과 source_customer_id=null 요청은 이전 배포의 재시도 기록과 호환된다.
- 성공 응답은 기존대로 숫자 inquiry_id, status, auto_message다. source_customer_id를 고객 응답에는 추가하지 않는다.
- 외부 ID는 같은 기업의 agent/admin이 `GET /api/v1/admin/inquiries/{inquiry_id}`로 상세 조회할 때만 제공한다. 기존 문의는 null이다.
- 외부 ID와 환경은 문의 생성 시 기록한다. 추가 메시지 API에는 이 필드를 보내지 않는다.

## 3. 프론트 담당 수정 위치

확인한 저장소: MOA-teams/moa-client의 main. 다른 담당자가 수정할 수 있으므로 작업 전 최신 코드를 확인한다.

| 파일 | 수정할 책임 |
|---|---|
| apps/client/src/components/auth/customer-auth.tsx | RequireCustomer의 고정 /login?next=/chat 이동을 전체 query 보존으로 변경. 로그인·회원가입·역할 오류 후 재로그인에서 같은 next 유지. 기업 연결 중에도 진입 정보 유지. 브랜드·기업 불일치 안내의 플로노트 문구 변경 |
| apps/client/src/components/chat/customer-chat.tsx | 로그인 여부와 무관하게 진입 query 파싱. 전달된 환경 우선 반영. 싱크노트 연결·오류 정보 안내. 헤더·첫 인사·로그아웃 이동 수정 |
| packages/shared/src/auth.ts | 고객용 next 검증에 /chat 허용 조건 적용. 공용 safeNext를 일괄 제한해 관리자 온보딩 이동을 깨뜨리지 않도록 고객용 검증 함수를 분리 |
| packages/shared/src/api.ts 및 types.ts | 고객 문의 API를 실제 백엔드 경로·snake_case 본문·숫자 ID·응답 DTO에 맞게 매핑. 인증 요청과 같은 토큰 갱신 흐름·Bearer 처리 사용 |

현재 확인한 customerApi.start는 customerName/customerCategory/text/environment/첨부 객체를 보내고 id/messages 형태의 응답을 기대한다.
이는 백엔드의 생성 API와 다르므로 **query만 읽게 고쳐도 실제 백엔드 연동이 끝나는 것은 아니다.**
인증 API는 이미 /backend 프록시 경로를 사용하므로, 문의도 같은 백엔드 연결 경로와 토큰 처리 흐름을 사용한다.

### next 보존·검증

- 이동 전 현재 pathname과 searchParams 전체를 합쳐 `/chat?...`을 만든다.
- next 값 전체를 encodeURIComponent로 한 번 인코딩해 로그인 URL을 구성한다. query의 일부만 따로 붙이지 않는다.
- 로그인과 회원가입은 URLSearchParams 또는 useSearchParams로 얻은 next를 사용한다. 이미 디코딩된 값에 decodeURIComponent를 반복 적용하지 않는다.
- 고객 이동은 정확히 /chat 경로만 허용한다. 외부 절대 URL, //host, 역슬래시·제어 문자, 다른 내부 경로는 /chat으로 대체한다.
- 아래는 고객용 검증 로직 예시다. 고정 기준 URL은 검증에만 쓰며 실제 이동 대상은 반환된 내부 경로다.

```ts
function safeChatNext(next: string | null | undefined): string {
  if (!next || !/^\/chat(?:\?|$)/.test(next) || /[\\\u0000-\u001f\u007f]/.test(next)) {
    return "/chat";
  }
  try {
    const base = "https://moa-client.invalid";
    const url = new URL(next, base);
    if (url.origin !== base || url.pathname !== "/chat") return "/chat";
    return url.pathname + url.search;
  } catch {
    return "/chat";
  }
}
```

### 진입 정보와 기존 상담 복원

- cid/error_code/os/app_version을 읽고 빈 값은 미전달로 취급한다. cid를 인증 사용자 이름·이메일로 덮어쓰지 않는다.
- 화면 상단에 ‘싱크노트에서 연결됨’을 표시한다. 오류 코드가 있으면 ‘E-502 오류 정보가 함께 전달돼요’처럼 React 텍스트로 표시하고 HTML로 삽입하지 않는다.
- 브랜드·첫 인사는 ‘싱크노트 고객센터’, ‘안녕하세요, 싱크노트입니다’로 통일한다.
- 외부에서 새 오류 query와 함께 들어오면 localStorage의 이전 inquiryId로 자동 이어 보내지 않고 새 문의 생성 흐름을 사용한다. 기존 문의의 env는 추가 메시지로 변경되지 않기 때문이다.
- 첫 문의가 성공한 뒤 새 inquiry_id를 기존 사용자별 저장 키에 기록하고 query를 제거한다. 실패 시에는 진입 정보·원래 요청 키·본문을 보존해 재시도한다.
- query 없는 일반 재접속은 기존 사용자별 상담 복원을 유지한다. 고객 ID만으로 다른 사용자의 저장 키를 읽지 않는다.

### 실제 API 어댑터

- 현재 UI의 text → message, customerCategory → customer_category, environment → env로 변환한다.
- UI의 appVersion/errorCode → app_version/error_code로 변환하며 source_customer_id에는 진입 cid를 넣는다.
- UI의 feature 분류는 백엔드 feature_request와 매핑한다. 전체 허용 값은 Swagger를 기준으로 한다.
- 생성 응답의 inquiry_id로 저장·조회하고 GET /api/v1/inquiries/{id}로 첫 메시지와 접수 안내를 읽는다. POST가 전체 대화 DTO를 반환한다고 가정하지 않는다.
- 조회는 cursor/limit을 사용한다. has_more=true이면 즉시 다음 페이지, false이면 3초 후 같은 next_cursor로 폴링한다. message_id로 중복 제거한다.
- 추가 메시지 요청은 text와 숫자 첨부 ID 배열만 보낸다. 종료된 문의의 409는 새 문의 생성 안내로 처리한다.
- 오류는 error.code/message/request_id를 읽는다. 로그인·토큰 갱신 요청 형식은 변경하지 않는다.

## 4. 프론트 담당 인수 확인

| 시나리오 | 기대 결과 |
|---|---|
| 미로그인 상태에서 cid·E-502·OS·버전으로 진입 | 로그인 후 네 값이 모두 남아 있고 문의에 저장됨 |
| 회원가입 경유 | 로그인 링크 왕복·가입 후 자동 로그인에서도 동일 next 유지 |
| 이미 로그인한 고객 | 로그인 화면 없이 같은 정보가 채워지고 정상 저장 |
| 기업 미소속 고객 | 기존 위젯 기업 연결 완료 후에도 query 유지 |
| 다른 기업·agent/admin 계정 | 기존 접근 안내 유지. cid로 소속·역할 변경 없음 |
| 외부 next, //host, 역슬래시, /admin 등 입력 | /chat으로 대체되고 외부 이동하지 않음 |
| 일부 환경 정보 누락 | 해당 값만 감지값 또는 null로 보완 |
| 같은 cid를 가진 다른 MOA 계정 | 다른 고객 문의에 접근할 수 없음 |
| 새 오류 query + 기존 저장 문의 | 새 문의에 새 환경 저장, 성공 후 query 제거 |
| 생성 실패 후 재시도 | 원래 키·본문으로 재시도되고 중복 문의 없음 |
| query 없는 일반 접속 | 기존 로그인·상담 복원 유지, source_customer_id는 생략 가능 |

## 5. 반영·검증 상태

- 백엔드: nullable String(128) 컬럼과 Alembic 0005_add_source_customer_id 추가. 기존 계정·문의 소유권은 유지한다.
- 로컬 테스트: 전체 295개 통과. 임시 PostgreSQL 스키마에서 마이그레이션 왕복·기존 데이터 보존·인증 격리·이전 버전 요청 재시도를 검증했다.
- 실제 프론트 화면 테스트는 미진행이다. 프론트 담당자가 위 변경·인수 확인을 수행해야 한다.
- 업무 DB·운영 서버에는 이 작업에서 마이그레이션을 실행하지 않았다. main 병합 후 기존 자동 배포에서 새 revision을 적용한다.
- AI 요청·그룹화·급증 알림은 기존 5·9단계 범위이며 이번 작업에서 실행하거나 완료로 표시하지 않는다.
