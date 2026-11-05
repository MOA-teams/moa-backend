# MOA GitHub App 등록

MOA 백엔드가 `MOA-demo/syncnote-app`의 코드를 읽고 변경 알림을 받으며 Issue를 생성하기 위한 연동 계정이다. 2026-11-05 서버 확인 기준으로 작성했다. App 등록은 GitHub에서 수행하며, 기존 worker 설치와는 별도다.

## 1. 등록 화면

[MOA-teams 조직의 New GitHub App](https://github.com/organizations/MOA-teams/settings/apps/new)을 연다. 조직 소유자 또는 App 관리 권한이 필요하다. 메뉴로 접근하면 조직 Settings → Developer settings → GitHub Apps → New GitHub App이다. [공식 등록 안내](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)

| 항목 | 입력·선택 |
|---|---|
| GitHub App name | `MOA Support Integration` 등 중복되지 않는 이름 |
| Homepage URL | `https://moa-admin-teal.vercel.app` |
| Callback URL 1 | `https://moa-admin-teal.vercel.app/integrations/github/callback` |
| Callback URL 2 | `http://localhost:3001/integrations/github/callback` — Add callback URL로 추가 |
| Expire user authorization tokens | 체크 유지 |
| Request user authorization (OAuth) during installation | 체크 해제. MOA 화면에서 별도로 사용자 인증을 시작함 |
| Enable Device Flow | 체크 해제 |
| Setup URL | 비워 둠 |
| Webhook Active | 체크 |
| Webhook URL | `https://52-196-205-217.nip.io/api/v1/integrations/github/webhook` |
| Webhook secret | 새로 생성한 긴 무작위 값. 아래 ENV_FILE에도 동일 값 사용 |
| SSL verification | 활성화 |
| Where can this GitHub App be installed? | **Any account** |

App 소유자는 MOA-teams이고 설치 대상은 다른 조직 MOA-demo이므로 Any account가 필요하다. 이는 모든 저장소에 자동 접근한다는 뜻이 아니며 설치 시 허용 저장소를 선택한다. [공개·비공개 App의 설치 범위](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private)

macOS에서 webhook secret을 만들어 클립보드에 복사하려면 `openssl rand -hex 32 | tr -d '\n' | pbcopy`를 사용한다. App의 Webhook secret과 ENV_FILE의 `GITHUB_WEBHOOK_SECRET`에 같은 값을 넣는다.

## 2. 권한·이벤트

Repository permissions:

- Contents: **Read-only** — 감시 브랜치·경로 수집
- Metadata: **Read-only** — 저장소 확인
- Issues: **Read and write** — 9단계 H04 Issue 생성

그 외 권한은 No access로 둔다. Subscribe to events에서는 **Push**를 선택한다. 현재 변경 감지는 PR 이벤트가 아니라 merge 후 감시 브랜치의 push를 사용한다. Create GitHub App을 누른다.

## 3. 키 발급과 ENV_FILE

등록 완료 화면에서 App ID·Client ID를 확인하고 **Generate a new client secret**, **Generate a private key**를 각각 실행한다. App slug는 공개 App 주소 `https://github.com/apps/<slug>`의 마지막 부분이다.

GitHub App에서 내려받은 private key PEM을 줄바꿈 없는 Base64로 변환한다. macOS에서 결과를 화면에 출력하지 않고 복사하는 예:

```bash
base64 < /path/to/github-app-private-key.pem | tr -d '\n' | pbcopy
```

[moa-backend Actions Secrets](https://github.com/MOA-teams/moa-backend/settings/secrets/actions)의 기존 **ENV_FILE 전체 내용**에 다음 7개 키를 추가한다. 기존 DB·JWT·S3·AI 값은 보존한다.

```dotenv
GITHUB_APP_ID=<App ID>
GITHUB_APP_SLUG=<App slug>
GITHUB_CLIENT_ID=<Client ID>
GITHUB_CLIENT_SECRET=<발급한 client secret>
GITHUB_APP_PRIVATE_KEY_BASE64=<PEM을 Base64로 변환한 한 줄>
GITHUB_WEBHOOK_SECRET=<App 설정에 입력한 webhook secret>
GITHUB_REDIRECT_URI=https://moa-admin-teal.vercel.app/integrations/github/callback
```

Secret 저장만으로 현재 프로세스에 반영되지는 않는다. ENV_FILE 배포 workflow를 다시 실행해 API·worker에 반영한다. private key·client secret은 소스 파일이나 전달 문서에 붙이지 않는다.

## 4. 실제 저장소에 설치

App 설정의 **Install App**에서 **MOA-demo**를 선택한다. **Only select repositories**로 `syncnote-app`만 선택해 설치한다. 대상 조직 설치 권한이 없으면 MOA-demo 관리자에게 설치를 요청한다.

이후 MOA 관리자 화면에서 GitHub 연결을 시작해 사용자 인증 → 저장소 선택 → 감시 브랜치 `main`·경로·보안 레벨 선택 → 최초 인덱싱 순서로 진행한다. 설치 후 받은 installation_id만 직접 입력하는 방식은 사용하지 않는다. 세부 API는 [GitHubOperations.md](GitHubOperations.md)를 따른다.

정상 확인: authorization 요청이 `200`으로 인증 URL을 반환하고, H01 접수 후 H02가 `연결 완료`로 전환되어야 한다. Webhook의 Recent Deliveries에서 MOA 서버 응답을 확인한다. 레벨 0의 최종 상태는 `연동 안 함`이며 성공적으로 비활성 상태를 반영했다는 의미다.

## 운영·localhost 콜백 선택

GitHub에는 두 callback을 등록할 수 있지만 현재 백엔드는 `GITHUB_REDIRECT_URI` **한 개만** 선택한다. 운영 백엔드는 운영 Vercel URL을 사용한다. 로컬 백엔드 `.env`에서는 `http://localhost:3001/integrations/github/callback`으로 지정한다. [GitHub callback 선택 규칙](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-user-authorization-callback-url)

localhost:3001 프론트가 **운영 백엔드**를 호출하면 인증 후에도 운영 Vercel URL로 돌아간다. CORS 허용은 API 호출을 허용할 뿐 callback을 자동 선택하지 않는다. 한 운영 백엔드에서 두 callback을 요청별로 선택하려면 허용 목록과 OAuth state 결합을 포함한 별도 인증 변경이 필요하며 이번 수정에는 포함하지 않는다.
