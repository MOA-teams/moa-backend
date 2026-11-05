# MOA 백엔드 구현 로드맵

2026-11-05 합의 기준. 단계 순서를 유지하며 각 단계의 외부 의존성 확인을 별도로 기록한다.
현재 계약은 5·6단계의 문자열 ID·한글 enum이며 APISpec·Swagger를 따른다. 3·4단계의 숫자 ID·영문 enum 설명은 당시 구현 기록이다.

## 6단계 구현 기준

- `feat/ai-integration`의 `053bd33`에서 `feat/agent-answers`로 분기한다. 5단계의 공개 문자열 ID·한글 enum 계약을 따른다. 아래 숫자 ID·영문 enum 설명은 3·4단계 당시 기록이며 현재 계약은 APISpec·Swagger가 기준이다.
- A05 담당자 답변, A06 답변 후 종료, C06 최초 평가를 구현한다. 최종 답변·메시지·문의 revision·멱등 응답을 원자적으로 저장한다.
- 원본 초안·검수와 최종 승인 이력을 분리하고, 기업별 백엔드 문서 변경번호·I05로 최신성을 검사한다. 이전 분석의 기준값이 없으면 재분석한다.
- `0010_answers`는 `0009_analysis` 뒤에 연결한다. 운영 마이그레이션·배포와 실제 AI·프론트 연결 확인은 별도 단계다.
- 프론트 연결·권한·중복 처리·오류·검증은 [답변 운영 안내](AnswerOperations.md)를 따른다.
- 2026-11-05: 독립 PostgreSQL 전체 pytest 432개 통과(skip 없음). 답변 전용 52개와 기존 인증·문의·문서·AI 회귀, 마이그레이션 일치·왕복·데이터 보존을 검증했다. 실제 AI·프론트·운영 배포 검증과 구분한다.

## 4단계 구현

- `feat/auth`의 `22ee549`에서 별도 worktree의 `feat/knowledge-documents`로 분기한다. 인증·문의 브랜치를 재작성하지 않는다.
- K01~K03, 문서 원본·불변 버전, tenant별 AI ID 매핑, PostgreSQL lease worker, PDF·한글 OCR, moa-ai I03~I05 연동을 구현한다.
- 공개 ID는 숫자, doc_type은 `policy/faq/feature_spec/release_note`, 상태는 `valid/retired`, 인덱싱은 `pending/running/completed/failed`다. AI 경계에서 `DOC-001`~`DOC-999`와 한글 값으로 변환한다.
- agent/admin 모두 같은 기업 문서를 관리한다. 공용 첨부는 `purpose=inquiry|document`로 분리하며 기본은 inquiry다.
- 마이그레이션은 인증 후속 변경인 `0005_add_source_customer_id` 뒤에 `0006_create_documents`, `0007_document_attachments`를 연결한다.
- 이전 버전 검색 차단 미확인 시 성공으로 반환하지 않는다. 원본·멱등 응답·event·revision을 보존해 응답 유실과 재시작을 복구한다.
- 실행·재시도·운영 검증 기준은 [문서 운영 안내](DocumentOperations.md)를 따른다. 실제 AI/S3 연결과 운영 배포는 자동 대역 테스트와 별도로 확인한다.
- 2026-11-05: 독립 PostgreSQL에서 전체 pytest **345개 통과**. 실제 Tesseract kor+eng로 한글 PNG/JPEG·스캔 PDF·혼합 PDF 검증도 통과했으며 skip은 없다. 마이그레이션 일치·왕복·기존 첨부 보존, 동시 ID/버전·멱등 요청·권한 격리·응답 유실·worker lease/재시도·CORS를 포함한다.
- 후속 배포 설정: GitHub Secret `ENV_FILE` 하나로 DB·JWT·S3·AI 설정을 전달한다. 검증·마이그레이션 성공 후 비공개 환경파일을 적용하며 API와 실행 중인 worker를 재시작한다. Secret 등록 방법은 문서 운영 안내를 따른다.

## 확정한 단계

| 단계 | 구현할 내용 | 관련 API | 완료 기준 |
|---|---|---|---|
| 1. 공통 실행·DB 기반 | 환경설정, DB 연결·세션·마이그레이션, 공통 오류, Enum·시각·ID 정책 | 공통 | 설정으로 서버·DB 연결. 신규 리소스 ID는 숫자 자동 증가 |
| 2. 인증·권한 | 로그인·토큰 갱신·고객 인증, 역할·기업 소속 확인 | AUTH01~03 및 기업 API | 역할 구분과 DB 소속 확인. 문의·첨부 소유권은 3단계에서 검증 |
| 3. 문의·채팅·첨부 | 문의·첫 메시지·접수 안내, 추가 메시지, cursor 조회, S3 첨부, 담당자 목록·상세 | C01~05, A01~02 | AI 없이 저장·접수 안내·이력·첨부와 접근 격리 확인 |
| 4. 지식 원본·버전 관리 | 문서 등록·조회·상태 변경, 추출, 인덱싱 요청·상태 동기화 | K01~03, I03~05 호출 | 새 버전 등록 시 이전 버전 검색 제외, 인덱싱 상태 표시 |
| 5. AI 요청·결과 반영 | 대화 스냅샷·분석 요청·콜백·결과 반영, 수동 분류, 유실 결과 복구 | A03~04, B01, I01~02 호출 | 결과 반영, 중복·오래된 결과 차단 |
| 6. 담당자 답변·상담 종료·평가 | 초안 수정·승인·발송, 수동 작성, 종료, 별점·해결 여부 | A05~06, C06 | 문의 → 답변 → 종료 → 평가 연결 |
| 7. 자동 질문·자동 답변 | 추가 질문, 자동 응대 정책, 수동·자동 공통 발송 | B01 결과 후 내부 처리 | 허용된 메시지만 발송, 실패·충돌은 검토 대기로 전환 |
| 8. GitHub 연결·변경 감지 | App 검증, 수집·보안 필터, webhook 검증·중복 처리, 소스 갱신 | H01~03, I03~05 호출 | 변경된 근거 검색 제외 및 최신 자료 동기화 |
| 9. 그룹·급증·알림·Issue | 그룹 확정, 중복 방지, 시간 구간 집계, 알림·읽음, Issue | G01~04, H04 | 현재 문의 급증 감지, 동일 장애 Issue 중복 방지 |
| 10. 통합 검증·데모 준비 | 실제 시드·LLM·GitHub 연결, 실패 복구·재현 | 전체 | 핵심 데모 3개 반복 실행 및 초기 상태 복구 |

## 작업 분리

- 인증 작업 폴더 `/Users/ehdus/Inter-highschool_hackathon`과 `feat/auth`는 유지한다.
- 3단계는 별도 worktree의 `feat/inquiries`에서 작업한다. 기준 커밋은 인증·CORS를 포함한 `cb9c583`이다.
- 인증 PR이 열려 있으면 문의 PR의 base를 `feat/auth`로 두고, 인증 merge 후 `main`으로 변경한다.
- 문의 브랜치에는 3단계 변경만 커밋한다. 인증 후속 변경을 자동으로 가져오거나 push하지 않는다.
- 로컬 설정·가상환경은 별도로 구성하며 운영 DB 마이그레이션은 이 작업에서 실행하지 않는다.

## ID·Enum 계약

- `user_id`, `inquiry_id`, `message_id`, `attachment_id`는 PostgreSQL BIGINT IDENTITY이며 Python int, JSON number이다.
- `Q-1001`, `MSG-`, `ATT-` 등 접두어는 사용하지 않는다. 숫자 범위는 1~9223372036854775807이다.
- `tenant_id`는 기존 소문자 영문 문자열을 유지한다.
- Python Enum 멤버는 대문자, API와 PostgreSQL Enum 값은 영어 snake_case이다. 한글 표시는 프론트에서 매핑한다.
- 한글 Enum 요청은 400이다. 추후 시드 import에서 원본 문자열 ID·한글 Enum을 변환하는 작업은 10단계 연동 범위에서 처리한다.

| 구분 | 영어 값 |
|---|---|
| UserRole | customer / agent / admin |
| InquiryCategory | usage / account / billing / bug / feature_request / other |
| InquiryUrgency | high / normal / low |
| InquiryStatus | received / in_review / needs_info / answered / closed |
| MessageSender | customer / agent / ai_auto |
| MessageType | customer_message / acknowledgement / information_request / final_answer |

후속 단계의 합의 값(이번에는 Enum을 미리 추가하지 않음):

| 구분 | 영어 값 |
|---|---|
| 문서 유형 | policy / faq / feature_spec / release_note |
| 문서 상태 | valid / retired |
| 답변 재사용 상태 | valid / review_required / inactive |
| 초안 결과 | draft_ready / manual_review_required / additional_question_required |
| 파이프라인 상태 | pending / running / completed / failed |
| 검증 상태 | passed / failed / unknown / not_run |
| 발송 방식 | automatic / agent |

## 3단계 API·동작

| API | 권한·책임 |
|---|---|
| POST /api/v1/inquiries | 기업 소속 customer. 문의·첫 메시지·접수 안내·첨부 연결을 원자적으로 저장 |
| POST /api/v1/inquiries/{inquiry_id}/messages | 소유 customer. 메시지·첨부·revision 증가를 함께 저장 |
| GET /api/v1/inquiries/{inquiry_id} | 소유 customer. 고객 공개 메시지만 cursor 조회 |
| POST /api/v1/attachments | 기업 소속 customer. multipart file 업로드, 숫자 첨부 ID와 한시적 URL 반환 |
| GET /api/v1/attachments/{attachment_id} | 소유 customer 또는 같은 기업의 연결된 문의를 보는 agent/admin. URL 재발급 |
| GET /api/v1/admin/inquiries | 같은 기업 agent/admin. 최신 생성 순 목록, status/category/urgency 필터 |
| GET /api/v1/admin/inquiries/{inquiry_id} | 같은 기업 agent/admin. 메타데이터·대화·최신 고객 메시지 ID |

- 기업·고객·시각은 서버가 결정한다. tenant_id/customer_id/created_at을 생성 본문에 지정하면 400이다.
- 문의와 메시지 저장은 Idempotency-Key(1~200자, 공백 없는 ASCII)를 요구한다. 동일 사용자·경로·키·정규화 본문은 같은 201 응답, 다른 본문은 409이다.
- 키는 논리 요청별 UUID 등을 사용하고 네트워크 재시도에만 재사용한다. 키와 요청 본문은 SHA-256으로 기록하며 자동 만료는 아직 없다.
- 텍스트는 1~10000자이며 공백만 있는 본문은 거절한다. 본문의 공백은 보존한다.
- 메시지당 첨부는 최대 10개다. 중복 ID·타인 첨부는 허용하지 않으며 이미 연결된 첨부 재사용은 409이다.
- 문의 생성 revision=1, 첫 메시지 순번=1, 접수 안내 순번=2이다. 접수 안내는 고정 문구이며 AI를 호출하지 않는다.
- 추가 메시지는 문의 행 잠금을 commit까지 유지해 순번·revision을 증가시키고 received로 변경한다. closed 상태는 409이다.
- 기본 limit=20, 최대 100이다. 메시지는 문의별 순번 오름차순이다. 서명 cursor는 사용자·기업·문의에 결합된다.
- 메시지 next_cursor는 빈 응답에도 유지한다. has_more=true이면 즉시 다음 페이지, false이면 3초 뒤 같은 cursor로 폴링한다.
- 목록은 created_at·inquiry_id 내림차순이며 cursor는 사용자·기업·필터에 결합된다. 마지막 next_cursor는 null이고 새 문의 확인은 처음부터 조회한다.
- category는 AI 분류 필터다. 미분석 category·urgency·그룹·실행·초안·검수 정보는 null이다. 고객 조회에 내부 필드는 포함하지 않는다.
- 다른 기업·고객의 대상은 404, 역할 부족·미소속은 403이다. 오류는 error.code/message/request_id와 X-Request-ID를 사용한다.
- 실제 AI 호출은 5단계, 담당자 답변·종료·평가는 6단계다.

## 검증 상태·남은 확인

- Python 3.11 환경에서 전체 pytest 242개 통과: 기존 인증·CORS, 모델 일치·마이그레이션 왕복·기존 데이터 보존, 중복 요청·동시 저장·cursor, 기업·고객 접근 격리, 첨부 연결·파일 검증·S3 실패 보상 포함.
- S3는 테스트 대역 및 botocore Stubber로 검증했다. 실제 SDK SigV4 URL 생성과 요청 파라미터는 AWS 네트워크 없이 확인했다.
- 실제 버킷·IAM Role·다운로드·만료·브라우저 CORS 확인은 인프라 준비 후 남아 있다. 따라서 외부 S3 통합 검증은 완료로 표시하지 않는다.
- 로컬 업무 스키마·운영 서버·실제 S3 객체는 변경하지 않았다.
- S3 실패 보상 삭제도 실패하거나 프로세스가 중단되면 고아 객체가 남을 수 있다. 자동 정리·보관 기간·전체 파일 보안 검사는 별도 작업이다.
- 인프라 전달·개발 실행·배포 확인은 [S3Setup.md](S3Setup.md)를 따른다.

## 싱크노트 고객 진입 연동 결정

고객 로그인을 유지한다. 싱크노트 cid는 문의의 nullable source_customer_id(최대 128자)로만 기록하고 실제 고객·기업·권한은 기존 JWT와 DB 정보로 판단한다.
외부 ID는 담당자 상세에만 제공한다. 프론트는 로그인·회원가입·기업 연결 전후 전체 /chat query를 유지하고 cid와 env.os/app_version/error_code를 생성 요청에 매핑한다.
백엔드 구현과 295개 테스트 검증을 완료했으며 프론트 코드는 별도 담당자 작업이다. 구체적인 수정 위치·요청 계약·화면 검증은 [SyncnoteIntegration.md](SyncnoteIntegration.md)를 따른다.
