# AI 담당자 전달: 실제 요금제 문의의 RAG 근거 누락

## 현상과 기대 결과

실제 LLM 연결과 문서 인덱싱은 성공했지만, 요금 문서가 있는 기업에서 Pro 가격·저장 공간 문의가 `직접 확인 필요`로 끝난다. AI 코드는 변경하지 않았으며, 사용자 결정에 따라 AI 담당자가 수정한다.

기대 결과는 문서 `DOC-001`의 Pro 월 9,900원·연 95,040원·50GB를 근거로 초안을 생성하고 별도 검수하는 것이다. 가격을 질문에 넣거나 정답 라벨을 인덱싱해서 통과시키면 안 된다.

## 재현 환경

| 항목 | 값 |
|---|---|
| 확인 일자 | 2026-11-05 |
| AI 배포 커밋 | `c7acb48` |
| 백엔드 배포 커밋 | `a786261` — LLM 연결 PR #12 |
| 기업 | `syncnotedemo` — 공용 syncnote와 분리 |
| 문의 | `Q-1019` |
| run | `RUN-c5667b438ff84f81af0f00379d8b8365` |
| 기준 메시지 / 문의 revision | `MSG-000047` / `1` |
| 분석 당시 백엔드 문서 변경번호 | `35` |
| 생성 모델 | `global.anthropic.claude-haiku-4-5-20251001-v1:0` |
| 임베딩 | `amazon.titan-embed-text-v2:0`, 1024차원, EC2 IAM Role |
| AI / backend 내부 주소 | `127.0.0.1:8001` / `127.0.0.1:8000` |
| 검색 최소 점수 | `0.5` |
| 원본 | moa-seed `31e938fbb76cd94c2cc68bfba904241aaedf25d8`, seed/docs |

가상 고객 입력:

> [통합 검증용 가상 문의] 싱크노트 Pro 요금제를 검토하고 있습니다. 월간 요금과 연간 결제 요금, 포함된 저장 공간을 알려 주세요.

실제 결과: 분석 `완료`, 분류 `결제`, 긴급도 `낮음`, 누락 필드 없음. 초안 결과 `직접 확인 필요`, draft 없음, 검수 `확인 불가`, 이유 `관련 공개 사실 근거를 찾지 못했습니다.` 백엔드는 이 결과를 그대로 보존했고 고객에게 최종 답변을 보내지 않았다.

## 검색 측정

분석 미리보기에서 얻은 검색어는 **`Pro 요금제 월간 요금 연간 요금 저장 공간`**이었다. 운영 Chroma를 수정하지 않는 비공개 복사본에서 점수 필터만 진단용으로 해제해 실제 Bedrock 임베딩 검색을 수행했다.

| 문서 | 실제 검색어 점수 |
|---|---:|
| DOC-006 | 0.2151 |
| DOC-002 | 0.1311 |
| DOC-005 | 0.1289 |
| DOC-010 | 0.1213 |
| DOC-001 — 필요한 요금 문서 | 0.1117 |

같은 복사본에서 `싱크노트 Pro 요금제 월간 요금 연간 결제 저장 공간`은 DOC-001이 0.4793으로 1위였고, 문서 원문의 `싱크노트 요금제는 Free, Pro, Team 세 가지입니다.`는 0.8820이었다. 서비스명이 검색어에서 빠지는 영향과 한국어 질의의 검색 순위·임계값을 함께 확인할 필요가 있다. 세 질의만으로 일반적인 해결책을 확정한 것은 아니다.

**임계값만 낮추는 수정은 아직 적용하지 않았다.** 실제 검색어는 정답 문서보다 다른 문서의 점수가 높다. 검색 점수는 정답 확률이 아니며, 관련 없는 문서까지 근거로 채택하지 않는지 검증해야 한다.

관련 코드: [검색어 전달·검색 임계값](https://github.com/MOA-teams/moa-ai/blob/c7acb48/app/services/pipeline/orchestrator.py), [cosine 점수·필터](https://github.com/MOA-teams/moa-ai/blob/c7acb48/app/services/rag/chroma_store.py), [분석 미리보기](https://github.com/MOA-teams/moa-ai/blob/c7acb48/app/api/internal/v1/preview.py).

## 현재 자료 상태와 안전한 재검증

- 최초 요금 문서는 API DOC-001 v1이었다. 별도 버전 전환 검증을 수행해 현재 **v3에 동일한 9,900원 원문**을 복구했다. v1·v2는 폐기·검색 제외이며 v3는 완료·검색 가능이다.
- 환불 시드의 v2/v3는 API v1/v2로 대응한다. 검증 기업에는 원본 문서 12개, 최신 검색 가능 문서 11개가 있다. 과거 문의·우수 답변·labels는 넣지 않았다.
- 재검증 전 I05의 `tenant=syncnotedemo, source_type=문서, source_id=DOC-001, version=3`을 확인한다. 조회 권한에는 기존 서비스 토큰과 `X-Tenant-ID`를 사용한다. 비밀값을 전달 문서나 로그에 기록하지 않는다.
- 검색 진단을 위해 운영 저장소를 다른 Chroma 프로세스로 열어 변경하지 않는다. 서버 `/home/ubuntu/.moa-step10/rag-diagnostic-*`는 당시의 비공개 복사본이다. 원본 전체 삭제·재인덱싱은 하지 않았다.
- 미리보기는 진단 전용이다. 최종 발송 검증은 I01→I02→백엔드 저장→A05 경로로 다시 수행한다.

AI 수정 배포 후 백엔드 담당자는 `GET /api/v1/admin/inquiries/Q-1019`에서 현재 revision·latest_customer_message_id를 읽고 다음 요청을 새 Idempotency-Key로 보낸다.

```http
POST /api/v1/admin/inquiries/Q-1019/analysis
Authorization: Bearer <같은 기업 담당자 access token>
Idempotency-Key: <새 요청 키>
Content-Type: application/json

{
  "expected_inquiry_revision": 1,
  "based_on_message_id": "MSG-000047",
  "review_only": true
}
```

위 ID·revision은 기록 시점 값이며 재조회 결과와 다르면 최신 값으로 교체한다. 분석 완료 후 기존 `flow-01.json`으로 `scripts/verify_demo_flow.py`를 재실행하면 문서 근거·검수 통과 확인부터 담당자 수정 발송·종료·평가를 이어간다. 실패했던 run을 성공으로 덮어쓰지 않는다.

수정 확인에는 요금·환불·계정처럼 실제 근거가 있는 문의와 학생 할인·공개 API처럼 근거 없는 문의를 함께 포함한다. labels는 기대 결과 평가에만 사용하고 프롬프트·검색 인덱스에는 넣지 않는다. 최소한 정답 문서 검색, 폐기 버전 제외, 근거 없는 초안 차단, 실제 고객 메시지 비노출까지 확인해야 한다.
