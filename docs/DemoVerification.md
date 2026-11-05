# 10단계 통합 검증과 재현

## 검증 환경과 현재 범위

LLM 연결 선행 수정은 [PR #12](https://github.com/MOA-teams/moa-backend/pull/12), main `a786261`에 병합·배포했다. 배포 후 분석 worker 활성화와 서비스 인증을 다시 확인했다. 실제 Bedrock 호출·분석 결과 저장은 [LLMOperations.md](LLMOperations.md)에 기록했다.

공용 `syncnote`에는 다른 작업의 문서와 AI 기록이 있어 사용자의 승인 후 **별도 기업 `syncnotedemo`**를 만들었다. AI의 `MOA_ALLOWED_TENANTS`에 이 기업을 추가했고 문서 systemd worker를 설치·활성화했다. 기존 기업·문서를 삭제하거나 덮어쓰지 않았다. 검증 계정·진행 기록은 서버 `/home/ubuntu/.moa-step10`의 비공개 파일에 보존한다. 비밀번호·토큰을 이 문서에 기록하지 않는다.

AI 서버 `.env` 변경은 백엔드 GitHub Secret과 별개다. AI 재배포 후에도 검증 기업을 유지하려면 AI 배포용 ENV_FILE의 `MOA_ALLOWED_TENANTS`에도 `syncnote,syncnotedemo`를 유지해야 한다. 백엔드에 AWS 모델 인증 키를 복제하지 않는다.

## 문서 시드 등록

사용한 원본은 [moa-seed](https://github.com/MOA-teams/moa-seed/tree/31e938fbb76cd94c2cc68bfba904241aaedf25d8), commit `31e938fbb76cd94c2cc68bfba904241aaedf25d8`이다.

```bash
# 네트워크·DB 변경 없는 사전 점검
.venv/bin/python scripts/seed_documents.py \
  --seed-dir /path/to/moa-seed/seed --tenant syncnotedemo

# 같은 기업 agent/admin의 access token을 MOA_ADMIN_TOKEN 환경변수에 준비
# 토큰을 명령 인자·기록 파일·저장소에 넣지 않는다.
.venv/bin/python scripts/seed_documents.py \
  --seed-dir /path/to/moa-seed/seed --tenant syncnotedemo \
  --state-file /private/path/documents.json --apply --wait-seconds 180
```

원격 실행에는 `--base-url https://...`를 추가한다. HTTP는 loopback만 허용한다. 원본 전체를 먼저 검증한 뒤 K01을 호출하고, K02에서 인덱싱 완료·검색 가능 여부를 확인한다. DB 직접 삽입이나 ID counter 수정은 하지 않는다.

- 최초 등록은 문서가 없는 기업에서만 허용한다. 같은 기업의 기존 문서에 자동 병합하지 않는다.
- 같은 진행 파일로 재실행하면 동일 계정·기업·서버·원본 hash를 확인한다. 첫 HTTP 요청 전에 import ID를 저장하고, 응답 유실 시 같은 멱등키·본문을 재전송한다.
- 진행 파일을 삭제해 재시도하지 않는다. 동일 파일의 동시 실행은 잠금으로 거절한다. 서로 다른 진행 파일로 같은 기업에 동시 등록하지 않는다.
- `seed/docs/*.md`만 문서로 등록한다. `demo/`와 `labels/`를 초기 자료로 넣지 않는다. `company.json`에서는 기본 기업 ID만 읽는다.
- 과거 문의 172건·답변 156건·우수 답변 50건은 **이 도구의 import 대상이 아니다**. 현재 공개 API로 과거 ID·시각·승인 이력을 그대로 복원할 수 없으며 별도 저장 계약이 필요하다. 과거 문의를 신규 API로 보내 급증 데이터처럼 만들지 않는다.

K01은 새 문서를 버전 1부터 연속 발급한다. 시드의 시작 버전과 다른 경우 원본을 조작하지 않고 진행 파일에 대응표를 남긴다. 공개/AI ID도 실제 응답을 기준으로 연결한다.

| 시드 원본 | 최초 검증 기업의 API 버전 |
|---|---|
| DOC-001 v1 | DOC-001 v1 |
| DOC-002 v2, 폐기 | DOC-002 v1, 폐기 |
| DOC-002 v3, 유효 | DOC-002 v2, 유효 |
| DOC-004 v2 | DOC-004 v1 |

원본 12개를 등록했고 최신 11개 모두 실제 인덱싱 완료·검색 가능을 확인했다. 이후 변경 검증으로 버전이 증가하면 최초 manifest와 최신 상태가 다르므로 등록 도구는 `DOCUMENT_CHANGED_OR_MISSING`으로 멈춘다. 이때 manifest를 임의로 수정해 새 시드 등록처럼 처리하지 않는다.

## 실제 초안·발송·종료·평가 재현 도구

```bash
.venv/bin/python scripts/verify_demo_flow.py --tenant syncnotedemo

# 같은 기업 고객·담당자 access token을 각각
# MOA_CUSTOMER_TOKEN, MOA_ADMIN_TOKEN 환경변수로 준비한다.
.venv/bin/python scripts/verify_demo_flow.py --tenant syncnotedemo \
  --state-file /private/path/flow-01.json --apply
```

도구는 가상 요금제 문의를 만들고 실제 분석 완료를 기다린다. 문서 근거가 있고 별도 검수가 통과한 초안에 안내 문장만 추가한 뒤 A05로 발송한다. 고객 메시지와 최종 본문 일치·내부 정보 비노출을 검사하고 종료·5점/해결 평가·관리자 승인 이력을 확인한다. 자동 응대 정책을 켜는 도구가 아니다.

각 변경 요청의 본문·revision·멱등키를 호출 전에 보존한다. 같은 진행 파일은 중단 지점부터 재개하며 완료된 작업은 다시 생성하지 않는다. 새로운 반복 검증은 새 진행 파일로 실행한다. 계정·기업·서버를 바꾸면 재사용을 거절한다. 검수 실패·근거 부족·시간 초과·최신성 충돌은 성공으로 바꾸지 않고 중단한다. 담당자 수동 승인 API는 검수 실패 초안도 허용할 수 있지만, 이 검증 도구가 자동으로 그 결정을 대신하지 않는다.

## 발견된 AI 검색 품질 문제

2026-11-05 `Q-1019`는 실제 분석이 완료됐으나 `직접 확인 필요`, 검수 `확인 불가`였다. 요금 문서가 검색 가능해도 “관련 공개 사실 근거를 찾지 못했습니다”로 초안이 생성되지 않아 발송·종료·평가 종단 검증은 **미완료**다.

실제 분석의 검색어를 재현하고, 운영 Chroma를 변경하지 않는 비공개 복사본에서 실제 임베딩 점수를 측정했다.

| 검색어 | 요금 문서 DOC-001 점수 | 관찰 |
|---|---:|---|
| Pro 요금제 월간 요금 연간 요금 저장 공간 | 0.1117 | DOC-006이 0.2151로 더 높음 |
| 싱크노트 Pro 요금제 월간 요금 연간 결제 저장 공간 | 0.4793 | 정답 문서가 1위지만 기본 임계값 0.5 미달 |
| 싱크노트 요금제는 Free, Pro, Team 세 가지입니다. | 0.8820 | 원문과 같은 문장에서는 검색됨 |

단순 접속 장애나 문서 미등록 문제가 아니다. 임계값만 낮추면 관련성이 낮은 다른 문서도 포함될 수 있어 운영 임계값과 AI 코드는 변경하지 않았다. AI 담당자가 서비스명 보존·질의 생성·한국어 검색 점수·검색 순위를 함께 검증해야 한다. 원문을 질문에 복사해 성공시키는 방식으로 완료 처리하지 않는다. 미리보기 API는 진단에만 사용했고 발송 경로에는 사용하지 않았다.

사용자 결정에 따라 AI 담당자가 수정하며, [전달용 재현 자료](AISearchReproduction.md)에 정확한 run·메시지·검색 점수·수정 후 재개 절차를 정리했다. 실제 배포 서버의 네 프론트 Origin × 답변·종료·평가 API preflight도 **12/12 통과**했다.

## 핵심 데모와 복구 기준

| 검증 | 현황 / 남은 조건 |
|---|---|
| 문서 근거 → 실제 AI 초안 → 수정 발송 → 종료 → 평가 | 재현 도구 준비. AI 검색 품질 문제 해결 후 동일 문의를 A03으로 재분석하고 재개 |
| 요금 변경 → 이전 근거 제외 → 오래된 승인 차단 → 원래 요금 복구 | 별도 검증 기업에서 K01/I05로 수행. 아래 실행 기록 참고 |
| 현재 문의 급증 → 그룹·알림 → Issue 1회 생성 | 9단계 병합과 실제 GitHub App 설치·권한 설정 필요 |

문서 복구는 원문 9,900원을 **새 버전**으로 등록하고 변경된 12,900원 버전의 검색 제외를 확인하는 방식이다. DB/AI 이력·ID를 삭제하거나 과거 버전으로 되돌리지 않는다. 따라서 내용의 초기 상태 복구와 DB 전체 초기화는 다르다. 전체 DB·AI 저장소 reset은 다른 팀 자료를 삭제할 위험이 있어 제공하지 않는다.

2026-11-05 실제 실행: `syncnotedemo / DOC-001` v1→v2(12,900원) 등록 후 v1의 `폐기/searchable=false`, v2의 검색 가능을 확인했다. 변경 전 문서 변경번호로 Q-1019에 답변을 승인하면 `409 STALE_KNOWLEDGE`로 거절됐다. 원문 9,900원을 v3로 등록해 검색 가능 상태로 복구하고 v2 검색 제외까지 확인했다. 기록은 비공개 `version-cycle-01.json`에 보존한다.

`moa-seed/scripts/burst_replay.py`는 현재 백엔드에 그대로 실행할 수 없다. 인증·Idempotency-Key가 없고 현재 C01이 거절하는 tenant_id/customer_id/created_at을 보낸다. 9단계 통합 때 현재 인증 계약에 맞춘 어댑터를 준비하고 정답 labels는 평가에만 사용한다. 기존 스크립트를 공용 서버에 실행하지 않는다.

## GitHub App이 필요한 이유

`MOA-demo/syncnote-app`은 가상 서비스의 소스 저장소다. **GitHub App은 MOA 백엔드가 그 저장소를 읽고 변경 알림을 받는 연동 계정**이다. 모바일 앱이나 moa-client 설치를 뜻하지 않는다. 저장소 생성과 App 등록·설치는 별개다.

현재 서버에는 GitHub App 설정 7개가 모두 없다. 저장소 관리자가 App을 등록해 `MOA-demo/syncnote-app`에 설치해야 실제 webhook 데모를 할 수 있다. 8단계는 Contents/Metadata 읽기를 사용하며, Issue 생성은 9단계의 Issues 쓰기 권한과 연동 계약을 함께 확인해야 한다. 설치 인증·callback·환경변수는 [GitHubOperations.md](GitHubOperations.md)를 따른다. 실제 설치 없이 가짜 installation ID나 사용자 토큰으로 검증을 우회하지 않는다.

백엔드 검증 도구·문서는 `feat/demo-verification`에서 관리하며, 9단계 서비스·모델·마이그레이션을 수정하지 않는다.

## 자동 검증

격리 PostgreSQL 전체 pytest **600개 통과**, skip 없음. 신규 17개는 원본 버전 대응·labels/demo 제외·응답 유실 재시도·원본/계정 불일치·동시 실행·검수 실패 차단·발송 본문·고객 정보 분리·종료/평가 재개를 검증한다. 실제 AI 초안의 검색 품질 실패와 테스트 대역의 성공 흐름을 구분한다.
