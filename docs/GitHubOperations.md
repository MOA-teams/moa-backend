# 8단계 GitHub 연결·변경 감지

백엔드 H01~H03와 프론트 연결 계약이다. 프론트 구현·온보딩의 서비스 기본 정보/문서 업로드/CSV import/테스트 문의, H04 Issue 생성은 이번 변경에 포함하지 않는다. GitHub App은 웹 서비스가 GitHub 권한을 받기 위한 연동 수단이다. `moa-client`를 설치하는 방식이 아니다.

## 연결 대상과 준비

- 데모 저장소: `MOA-demo/syncnote-app`, 감시 브랜치 `main`. `release/pricing`이 main에 반영되면 push 이벤트로 재수집한다.
- 참조: [AI Integration](https://github.com/MOA-teams/moa-ai/blob/main/docs/Integration.md), [AI RAG 계약](https://github.com/MOA-teams/moa-ai/blob/main/app/schemas/rag.py), [싱크노트 README](https://github.com/MOA-demo/syncnote-app/blob/main/README.md).
- GitHub App은 별도 생성·설치가 필요하다. 설치 대상 저장소를 **선택한 저장소만**으로 제한한다.
- 이번 단계 권한은 **Contents Read / Metadata Read**. Issue 쓰기·코드 쓰기 권한을 요청하지 않는다. Pull request 이벤트는 사용하지 않으며 merge 결과의 push가 기준이다.
- Webhook URL: 외부에서 접근 가능한 HTTPS 백엔드 주소의 `/api/v1/integrations/github/webhook`. 구독은 **Push**, 설치 삭제·중지·저장소 제외 이벤트도 처리한다.
- User authorization callback URL: 프론트가 구현할 고정 주소(예: `https://moa-admin-teal.vercel.app/integrations/github/callback`). GitHub App 설정과 `GITHUB_REDIRECT_URI`를 정확히 일치시킨다. 이 주소는 이번 백엔드 변경으로 생성되지 않는다.
- App 설정에서 설치 시 사용자 인증을 자동 요청하는 옵션은 끄고, 아래 **설치 → 별도 사용자 인증** 순서를 따른다. Setup URL의 `installation_id`만 받아 H01을 호출해서는 안 된다. [GitHub Setup URL 안내](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url).

## 환경설정과 실행

기존 GitHub Secret `ENV_FILE`에 다음 **키와 실제 운영 값**을 추가한다. 비밀값은 프론트·저장소·채팅·로그에 넣지 않는다.

| 키 | 용도 |
|---|---|
| `GITHUB_APP_ID` | 숫자 App ID를 문자열로 지정 |
| `GITHUB_APP_SLUG` | `github.com/apps/{slug}`의 slug |
| `GITHUB_CLIENT_ID` | GitHub App의 OAuth Client ID |
| `GITHUB_CLIENT_SECRET` | GitHub App 사용자 인증용 secret |
| `GITHUB_APP_PRIVATE_KEY_BASE64` | App PEM private key를 줄바꿈 없는 Base64로 인코딩한 값 |
| `GITHUB_WEBHOOK_SECRET` | GitHub App Webhook과 같은 서명 secret |
| `GITHUB_REDIRECT_URI` | 등록한 프론트 콜백 HTTPS URL. localhost 개발만 HTTP 허용 |

모두 선택 설정이다. 미설정이어도 기존 인증·문의 API는 시작된다. 레벨 1·2 연결 시작은 설정 누락 시 503, 레벨 0은 GitHub 인증 없이 처리한다. AI는 기존 `AI_BASE_URL=http://127.0.0.1:8001` 및 `AI_SERVICE_TOKEN`을 사용한다. 이 주소는 백엔드·AI가 같은 EC2에서 실행될 때의 내부 주소이며 브라우저가 직접 호출하지 않는다.

`0011_github`는 `0010_answers` 뒤에 적용한다. 7단계 병합 이후의 최종 head는 `0012_github_automatic`이며, 기존 `0011_github`와 `0011_answers_automatic` 분기를 데이터 변경 없이 합친다. 이미 배포된 revision은 수정하지 않는다. 기존 테이블을 변경하거나 기존 문의·답변·문서를 삭제하지 않는다. GitHub 테이블 5개가 추가된다. 운영에서 downgrade하면 이 단계의 연결·이벤트·작업·원본 이력이 삭제되므로 먼저 worker를 중지하고 별도로 백업한다.

문서 worker와 별도로 아래 프로세스가 필요하다.

```bash
.venv/bin/python -m app.workers.github
# 작업을 한 단계만 처리
.venv/bin/python -m app.workers.github --once
# 원인을 해결한 뒤 현재 세대의 실패 작업 재개
.venv/bin/python -m app.workers.github --retry-job 123
```

Ubuntu systemd unit 예시(`/etc/systemd/system/moa-github-worker.service`):

```ini
[Unit]
Description=MOA GitHub synchronization worker
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/moa-backend
EnvironmentFile=/home/ubuntu/moa-backend/.env.systemd
ExecStart=/home/ubuntu/moa-backend/.venv/bin/python -m app.workers.github
Restart=always
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

운영 도입 시 unit 설치와 `systemctl enable --now moa-github-worker`를 별도로 수행한다. ENV_FILE 배포는 이 worker의 환경파일 drop-in도 설치하고, **이미 실행 중인 worker만** 재시작·상태 검사한다. 자동으로 unit을 생성하거나 최초 활성화하지 않는다.

## 프론트 연결 순서

모든 경로에 `/api/v1`을 붙인다. 관리자 요청에는 `Authorization: Bearer <MOA access token>`이 필요하다. H02는 agent/admin, 설정·인증·선택은 admin 전용이다. 기업 소속은 MOA 인증 사용자에서 결정하며 요청으로 tenant를 받지 않는다.

1. **레벨 0**: H01에 `{"security_level":0}`을 보내고 상태를 조회한다. 기존 연결이 있다면 검색 차단 완료까지 `동기화 중`이다.
2. **레벨 1·2 설치**: `POST /admin/integrations/github/authorization`(본문 없음)으로 `installation_url`을 받는다. 사용자가 GitHub에서 App을 선택 저장소에 설치한다. 설치 완료 여부를 timeout으로 확정하지 않는다.
3. **사용자 인증**: 설치 후 같은 시작 API를 다시 호출하고 반환된 `authorization_url`로 이동한다. 10분 유효한 state와 PKCE challenge가 포함된다. 이전 시작 요청의 state는 사용하지 않는다.
4. **콜백**: 프론트는 GitHub가 보낸 `code`, `state`를 `POST /admin/integrations/github/authorization/complete`에 전달한다. 서버는 state의 관리자·기업·만료·재사용 여부를 검사하고 GitHub 사용자 토큰으로 **해당 App 설치 + 저장소 admin 권한**을 확인한다. 토큰은 이 검증 동안만 사용하며 저장하지 않는다.
5. **저장소 선택**: complete 응답의 `repositories`만 선택 목록으로 사용한다. `authorization_id`는 30분 유효하며 한 번의 H01에만 사용한다. 사용자 GitHub 권한이 부족하면 목록이 비어 있다. 이 경우 GitHub 저장소 관리자 계정으로 다시 인증한다.
6. **브랜치·경로 선택**: selection API를 아래와 같이 호출한다. 경로는 저장소 상대 경로이며 파일 직접 입력도 가능하다. 전체 허용을 명시하려면 `.`을 사용한다. 기본 제외 패턴은 제거할 수 없다.
7. **최초 인덱싱**: H01을 새 `Idempotency-Key`로 호출한다. `202`는 작업 접수이며 완료가 아니다. H02를 3초 간격으로 조회한다. `연결 완료`만 다음 온보딩 단계로 진행한다. 실패 시 `error_code`를 표시하고 설정 수정 또는 운영자 재시도를 안내한다.

시작 응답:

```json
{
  "authorization_url": "https://github.com/login/oauth/authorize?...",
  "installation_url": "https://github.com/apps/your-app/installations/new",
  "expires_in": 600
}
```

인증 완료 요청:

```json
{"state":"GitHub가 반환한 state", "code":"일회용 authorization code"}
```

인증 완료 응답:

```json
{
  "authorization_id": "검증된 state",
  "expires_in": 1800,
  "repositories": [{"installation_id":12345,"repository_id":67890,"repository":"MOA-demo/syncnote-app","default_branch":"main"}]
}
```

`POST /admin/integrations/github/selection`:

```json
{"authorization_id":"검증된 state", "repository_id":67890}
```

응답은 `{"branches":[{"name":"main","sha":"40자리 SHA"}]}`. 같은 본문에 `"branch":"main"`을 추가하면 `{"sha":"40자리 SHA","paths":["billing","sync"]}`를 반환한다. 콜백 후 code/state가 URL에 남지 않게 제거하고 `authorization_id`는 연결 화면의 메모리에서 관리한다. 선택 요청은 POST 본문으로 보내며 인증값을 URL·분석 로그에 넣지 않는다.

H01 `POST /admin/integrations/github`, `Idempotency-Key` 필수:

```json
{
  "authorization_id":"검증된 state",
  "installation_id":12345,
  "repository":"MOA-demo/syncnote-app",
  "watch_branch":"main",
  "security_level":2,
  "include_paths":["billing/","sync/"],
  "exclude_patterns":[".env*","*.pem","*.key","secrets/","config/prod*"],
  "document_bindings":[{"path":"billing/pricing.ts","doc_id":"DOC-001"}]
}
```

`document_bindings`는 선택 사항이며 같은 기업의 실제 문서 ID만 허용한다. 등록된 path가 변경·삭제·수집 제외되면 해당 문서 최신 버전을 **재검토 필요**로 표시하고 검색에서 제외한다. 같은 경로 아래의 변경도 포함한다. 가격·정책 내용을 자동으로 새 값으로 확정하지 않는다. 최초 연결만으로 기존 문서를 재검토 상태로 바꾸지는 않는다.

같은 멱등키·같은 본문은 처음 202 응답을 재사용한다. 같은 키·다른 본문은 409다. 다른 키에 인증 증명을 재사용하면 400이다. 설정을 변경하려면 새 인증을 진행한다. 연결 해제는 레벨 0으로 보내며 별도 GitHub 인증이 필요 없다. 같은 저장소는 한 기업에만 연결할 수 있다.

H02 응답 예:

```json
{
  "integration_id":"github_syncnote", "status":"연결 완료",
  "installation_id":12345, "repository":"MOA-demo/syncnote-app",
  "watch_branch":"main", "security_level":2, "analysis_provider":"external_llm",
  "include_paths":["billing/","sync/"],
  "exclude_patterns":[".env*","*.pem","*.key","secrets/","config/prod*"],
  "document_bindings":[{"path":"billing/pricing.ts","doc_id":"DOC-001"}],
  "last_synced_sha":"40자리 SHA", "last_synced_at":"2026-11-05T03:00:00Z",
  "error_code":null,
  "report":{"indexed_files":["billing/pricing.ts"],"excluded":[],"masked":[],"outside_watch":0}
}
```

상태는 `연동 안 함 / 동기화 중 / 연결 완료 / 동기화 실패`. `report`와 `last_synced_sha`는 마지막 **완료** 스냅샷이다. 진행 중에도 이전 완료 결과가 남을 수 있다. 프론트는 임의의 진행률을 만들지 않고 상태와 마지막 완료 결과를 구분해서 표시한다. `masked`는 경로·줄 번호·종류만 포함한다. 원문·토큰·비밀값은 조회로 반환하지 않는다. provider는 레벨 1·2에서 `external_llm`, 0에서 `none`이다. 레벨 2를 로컬 LLM 처리로 안내하면 안 된다.

## Webhook·검색 일관성

- H03에는 MOA JWT 대신 `X-Hub-Signature-256`을 사용한다. JSON 파싱 전의 원본 bytes를 HMAC-SHA256으로 검증한다. 수신 크기는 2MiB 이하이다.
- `X-GitHub-Delivery`로 중복을 제거한다. 같은 delivery의 같은 이벤트·본문은 같은 결과, 다른 본문은 409. 무관한 브랜치/설치/이벤트는 `202 accepted=false`이며 역시 중복 기록을 보존한다.
- push의 `repository.id`, `installation.id`, `refs/heads/{watch_branch}`를 확인한다. 지연·역순 이벤트의 `after`를 그대로 적용하지 않고 수집 시점의 현재 HEAD에서 immutable tree/blob SHA로 읽는다.
- 설치 삭제·중지·저장소 권한 제외·감시 브랜치 삭제는 연결을 비활성화하고 기존 소스를 차단한다. 복구 후 자동 재활성화하지 않으며 새 인증·H01을 수행한다.
- GitHub 원본은 제외·마스킹 후에만 DB에 저장한다. 보안 레벨 1은 blob API를 호출하지 않는다. 레벨 2는 허용 경로·확장자·일반 파일만 읽고 symlink/submodule·binary를 제외한다. 마스킹은 알려진 패턴을 대상으로 하며 모든 비밀을 탐지한다는 보장은 없다. 민감한 디렉터리는 반드시 경로에서 제외한다.
- 기본 제외 `.env*`, `*.pem`, `*.key`, `secrets/`, `config/prod*`는 하위 디렉터리에도 적용한다. dependency 디렉터리도 제외한다. I03은 항상 `내부 전용`이며 코드 본문을 고객 응답에 노출하지 않는다.
- 소스 ID는 저장소 ID·종류·경로 해시, version은 commit SHA다. 변경하지 않은 blob은 기존 검색 자료를 재사용한다. 변경·삭제·이름 변경·허용 범위 축소는 이전 소스를 I04로 폐기하고 I05로 확인한 **뒤** 새 자료를 I03에 전달한다.
- 문서 연결의 검색 차단도 먼저 확인한다. 연동 변경 접수~완료 사이에는 연결 문서를 이용한 담당자 발송이 `503 GITHUB_SYNC_PENDING`으로 거절된다. 이전 연결 경로를 삭제해도 차단 확인 전까지 이 보호가 유지된다. 7단계 자동 답변도 같은 조건이면 `GITHUB_SYNC_PENDING` 사유로 담당자 검토로 전환한다.
- 기업 지식 변경번호 → 연동 → 작업 → 문서 순서로 잠근다. GitHub 수집은 잠금 밖, AI 상태 변경은 잠금 안에서 실행한다. worker의 generation·lease token으로 오래된 수집 결과를 버린다. 완료와 이벤트 접수 시 기업 변경번호를 증가시켜 이전 초안 승인을 막는다.
- DB에 제출 의도를 먼저 저장한다. I03 응답 유실은 같은 payload, I04 응답 유실은 같은 event ID·본문으로 재시도한다. force-push로 과거 SHA가 돌아오면 더 높은 revision으로 재활성화한다.
- 작업 lease 600초, 수집 전체 300초, 네트워크 요청 10초, AI poll 5초/최대 300초. 일시 오류는 5·30·120초로 최대 3회 재시도하며 GitHub rate-limit 대기시간도 반영한다. 실패를 숨기거나 연결 완료로 처리하지 않는다.
- 수집 상한: tree 10,000 entries, 스냅샷 200 files, 파일 200KB, 새 텍스트 4MiB. truncated tree는 실패 처리한다. 제외 파일은 report에 사유를 표시한다. 인덱싱 실패 원문은 반환하지 않고 분류 코드만 제공한다.
- worker가 장시간 중단되면 이벤트는 저장되지만 AI 차단 반영은 지연된다. `동기화 중/실패`를 완료로 취급하면 안 된다. H02는 매번 외부 GitHub/AI를 조회하지 않는다. App 권한을 우회한 외부 AI 저장소 변경까지 이 worker가 감지하는 것은 아니다.
- 기존 A05는 코드 근거 직접 발송을 계속 거절한다. 이 단계는 코드 수집·갱신을 제공하며, 코드의 고객용 발송 검증 확대는 별도 범위다.

## 검증과 운영 확인

2026-11-05 7·8단계 통합 후 전체 pytest 571개가 통과했으며 skip은 없다. GitHub 전용 33개와 후속 통합 5개는 동기화 중 자동 발송 차단·기존 GitHub 마이그레이션 데이터 보존을 포함한다. 자동 검증은 격리 PostgreSQL 및 GitHub/AI 네트워크 대역을 사용한다. 실제 App 설치·실제 AI 인덱싱과 구분한다. 배포 전 운영자가 다음을 확인한다.

1. App 생성·선택 저장소 설치·고정 콜백 및 Webhook HTTPS URL·서명을 설정한다.
2. 프론트가 모의 설치/진행률 대신 위 API를 호출하도록 연결한다. 4개 허용 Origin의 POST 및 Authorization/Idempotency-Key preflight를 사용한다.
3. 환경설정 적용·마이그레이션·GitHub worker 최초 활성화 후 GitHub 인증→선택→H01→H02 `연결 완료`를 확인한다.
4. 실제 AI의 I05에서 최신 소스가 검색 가능하고 이전 SHA가 검색 불가인지 확인한다.
5. `billing/pricing.ts → DOC-001`을 연결하고 요금 변경을 main에 반영한다. DOC-001 재검토·검색 제외와 최신 코드 동기화를 확인하고, 정책 문서는 담당자가 새 버전으로 확정한다.
6. 잘못된 서명, 같은 delivery 재전송, 다른 브랜치, 삭제 파일, 연결 해제, worker 재시작을 확인한다. 실패 작업은 원인을 해결한 뒤 `--retry-job`으로 재개한다. 이전 세대·완료 작업은 재개할 수 없다.

설치 인증 참고: [GitHub App user token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app), [App JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app).
