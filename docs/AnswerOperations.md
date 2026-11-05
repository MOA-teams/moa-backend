# 담당자 답변·종료·평가 연동

6단계는 `feat/ai-integration`의 문자열 ID·한글 enum 계약을 따른다. AI 자동 발송, 우수 답변 선정, GitHub 코드 근거 검증은 포함하지 않는다.

## 프론트 연결 순서

1. 담당자는 `GET /api/v1/admin/inquiries/{inquiry_id}`로 `revision`, `latest_customer_message_id`, `backend_knowledge_revision`, 현재 `draft`를 읽는다.
2. 답변을 확인·수정한 뒤 아래 A05를 호출한다. 직접 작성이면 `draft_id`와 `draft_revision`을 모두 null로 보낸다. 임시 편집 내용은 프론트에서 관리하며 저장 API는 없다.
3. 201 응답은 답변과 채팅 메시지의 DB 커밋 완료다. 고객이 읽었다는 뜻은 아니다. 고객 화면은 기존 cursor·3초 폴링으로 새 메시지를 읽는다.
4. 담당자는 A05 응답의 `inquiry_revision` 또는 새 상세 조회의 `revision`으로 A06을 호출한다.
5. 고객 채팅 조회의 `feedback_requested=true`, `feedback_submitted=false`이면 평가 UI를 표시한다. C06 제출 후 반환된 평가를 표시한다.

모든 변경 요청에 `Authorization: Bearer ...`와 `Idempotency-Key`가 필요하다. 같은 사용자·경로·키·본문 재시도는 기존 결과, 같은 키·다른 본문은 409다. 새 논리 요청에는 새 키를 사용한다. 성공 응답을 잃었다면 최신 revision으로 본문을 고쳐 보내지 말고 원래 키·본문을 재전송한다.

## A05: 최종 답변 승인·발송

`POST /api/v1/admin/inquiries/Q-1001/answers`

```json
{
  "draft_id": "DRAFT-000001",
  "draft_revision": 1,
  "expected_inquiry_revision": 1,
  "expected_last_customer_message_id": "MSG-000001",
  "expected_backend_knowledge_revision": 7,
  "answer_text": "담당자가 확인한 안내입니다.",
  "evidence": {"docs": [{"doc_id": "DOC-001", "version": 1}], "code": []}
}
```

성공 201:

```json
{
  "answer_id": "A-1001",
  "message_id": "MSG-000003",
  "inquiry_id": "Q-1001",
  "inquiry_revision": 2,
  "status": "답변 완료",
  "delivery_mode": "담당자",
  "agent_id": "agent_01",
  "answered_at": "2026-11-05T13:00:00+09:00"
}
```

- 같은 기업 agent/admin이 승인한다. 본문 1~10,000자, 공백만 있는 본문은 거절한다.
- 직접 작성은 AI 없이 가능하며 문서 근거는 생략할 수 있다. 문서를 지정하면 동일한 유효성 검사를 받는다.
- 초안 기반 근거는 저장된 초안의 문서 근거 중에서만 선택한다. 코드 근거는 현 단계에서 409다.
- AI 검수 실패도 사람이 수정·승인할 수 있으나, 오래된 문의·초안·문서로 발송할 수는 없다. 담당자 승인은 수정된 본문에 대한 AI 검수 통과를 의미하지 않는다.
- 답변과 메시지 본문은 함께 저장한다. 같은 초안·기준 고객 메시지 또는 같은 문의 revision의 중복 승인은 유일성 제약으로 차단한다.
- 직접 작성으로 후속 답변을 추가하려면 최신 상세를 읽어 새 revision과 새 멱등키를 사용한다. 이미 발송한 초안 자체는 다시 발송할 수 없다.
- `agent_edit_ratio`는 원본·최종 본문에 대한 `1 - SequenceMatcher(..., autojunk=False).ratio()`를 소수 네 자리로 저장한다. 직접 작성은 null이며 우수 답변 후보 선정은 하지 않는다.

## A06: 상담 종료

`POST /api/v1/admin/inquiries/Q-1001/close`

```json
{"expected_inquiry_revision": 2, "resolution_status": "미확인"}
```

성공 200에는 `inquiry_id`, `inquiry_revision`, `status=상담 종료`, `resolution_status=미확인`, `closed_at`, `feedback_requested=true`가 포함된다.

최종 답변이 있어야 종료할 수 있다. 종료 시 revision이 증가하고 이후 고객 메시지·담당자 답변·분류 변경을 거절한다. 종료는 해결 확인이 아니다. 재개 API는 제공하지 않으며 새 문의를 생성한다. 같은 키 재시도만 종료 결과를 재응답한다.

## C06: 최초 평가

`POST /api/v1/inquiries/Q-1001/feedback`

```json
{"rating": null, "resolved": false, "comment": "아직 같은 문제가 있어요."}
```

성공 201에는 `inquiry_id`, 종료 당시 마지막 답변의 `answer_id`, `rating`, `resolved`, `comment`, `resolution_status`, `recorded_at`이 포함된다.

- 종료된 본인 문의에 한 번만 제출한다. 다른 키로 두 번째 평가를 보내면 같은 내용이어도 409다.
- 별점은 정수 1~5, 해결 여부는 boolean, 댓글은 10,000자 이하이며 공백만 있으면 거절한다. 셋 중 하나 이상 필요하다.
- `resolved=false`도 유효하다. true는 `해결 확인`, false는 `미해결`, 미제공은 `미확인`이다. 별점과 해결 여부는 독립적이다.
- 평가는 수정하지 않으며, 미해결 평가가 문의를 자동 재개하지 않는다. 무응답은 평가 레코드가 없고 해결 상태가 미확인이다.

## 조회와 정보 경계

고객 메시지는 최종 답변일 때 `answer_id`를 포함하고 그 외에는 null이다. 고객 채팅은 `closed_at`, `resolution_status`, `feedback_requested`, `feedback_submitted`, 본인 `feedback`을 제공한다. 내부 초안·근거·검수는 제공하지 않는다.

담당자 상세의 `answers`는 현재 메시지 페이지에 해당하는 답변만 반환한다. 최종 본문·근거·원본 초안·원본 검수·승인자·승인 시각·수정 여부·수정률·run을 확인할 수 있다. AI 초안은 수정하거나 덮어쓰지 않는다. 이후 분석 스냅샷의 최종 답변 메시지에는 `answer_id`가 전달된다.

## 최신성·실패 처리

`backend_knowledge_revision`은 백엔드 관리 문서의 변경번호이며 AI 결과의 `knowledge_revision`과 다른 값이다. DB 트리거가 문서 버전 등록 또는 상태·재검토·검색 가능 여부·source revision·텍스트 해시 변경 시 기업별 값을 증가시킨다. 조회 동기화나 worker의 상태 변경도 포함하며 동일 상태 쓰기는 증가시키지 않는다.

분석 요청은 접수 당시 변경번호를 내부 필드에 보존한다. 기존 분석의 null 값은 최신값으로 보정하지 않으며 다시 분석해야 한다. 변경번호가 같더라도 인용한 문서를 I05로 재확인한다. AI 조회 전체 제한은 10초다. 백엔드를 거치지 않은 AI 소스 변경까지 분산 트랜잭션으로 보장하지는 않는다.

잠금 순서는 문의 → 분석 요청·run·초안 → 기업 변경번호 → 문서 순이다. 기업 행에는 외래키 KEY SHARE와 호환되는 FOR NO KEY UPDATE를 사용한다. 문서 작업도 기업을 먼저 잠그고, 여러 문서 근거는 문서 ID 순으로 확인한다. OCR은 기존대로 잠금 밖에서 실행한다. 기업 단위 잠금으로 해당 기업의 문서 처리와 승인 요청이 기다릴 수 있으므로 AI 조회 지연을 관찰한다.

| 오류 | 처리 |
|---|---|
| 400 `VALIDATION_ERROR` | 필드 형식·길이·필수 헤더 확인 |
| 403 `FORBIDDEN` / `TENANT_REQUIRED` | 역할·기업 소속 확인 |
| 404 `INQUIRY_NOT_FOUND` / `DOCUMENT_NOT_FOUND` | 존재·접근 가능한 대상인지 확인 |
| 409 `STALE_INQUIRY_REVISION` / `STALE_CUSTOMER_MESSAGE` / `STALE_DRAFT` | 상세 재조회 후 필요한 경우 A03 재분석 |
| 409 `STALE_KNOWLEDGE` / `STALE_EVIDENCE` | 현재 문서 상태 확인 후 초안 재분석 |
| 409 `EVIDENCE_NOT_RETRIEVED` / `CODE_EVIDENCE_UNVERIFIED` | 허용된 문서 근거를 선택하거나 담당자가 직접 작성 |
| 409 `ANSWER_REQUIRED` / `INQUIRY_CLOSED` / `INQUIRY_NOT_CLOSED` | 답변·종료 상태 확인 |
| 409 `FEEDBACK_ALREADY_SUBMITTED` / `ANSWER_ALREADY_SENT` | 조회로 이미 처리된 결과 확인 |
| 503 `EVIDENCE_CHECK_UNAVAILABLE` | 저장·발송 없음. 기존 키·본문으로 재시도 |

## 마이그레이션·검증

`0010_answers`는 `0009_analysis` 다음이다. 답변·평가 테이블, 종료 필드, 기업 변경번호와 분석 기준값, 답변 ID 시퀀스, 문서 변경 감지 트리거를 추가한다. 기존 자료는 보존한다. downgrade는 새 답변·평가 이력을 삭제하므로 운영 롤백 전에 백업·데이터 보존 절차가 필요하다.

독립 PostgreSQL에서 `tests/test_answers.py` 및 전체 테스트로 검증한다. 테스트 AI/RAG 대역은 실제 AI·브라우저 연동과 구분한다. 운영 DB 마이그레이션·배포·실제 프론트 연결 확인은 별도 실행 단계다.

### 2026-11-05 검증 결과

독립 PostgreSQL에서 전체 pytest **432개 통과**, skip 없음. 답변 전용 52개 사례에는 AI 검수 실패 초안의 담당자 승인, 직접 작성, 고객 채팅 전달, 종료·최초 평가, 문서 상태·버전·검색 가능 여부 변경, 멱등 재시도, 동시 승인·고객 메시지·종료·평가, 조회 일관성, 기업 외래키 잠금 호환성, ID 소진 롤백과 네 Origin의 preflight를 포함한다. ORM·마이그레이션 일치 및 `0009 ↔ 0010` 데이터 보존, 기존 인증·문의·문서·AI 회귀 테스트도 통과했다. 실제 AI 네트워크 호출 및 운영 배포는 수행하지 않았다.
