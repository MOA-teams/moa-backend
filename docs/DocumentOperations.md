# 문서 원본·버전과 RAG 운영

## 실행 준비

같은 기업의 agent/admin이 문서 API를 사용한다. 공개 ID는 숫자, enum은 영어다. AI에는 기업별 `DOC-001`~`DOC-999`와 한글 값을 전달한다. 999개는 버전이 아닌 문서 수 제한이며 폐기해도 ID를 재사용하지 않는다. 이번 API로 등록하는 문서는 `고객 공개` RAG 근거다.

API와 worker에 동일한 설정을 제공한다. Secret 값은 저장소·채팅·로그에 남기지 않는다.

### GitHub Actions의 ENV_FILE

[백엔드 저장소 Actions Secrets](https://github.com/MOA-teams/moa-backend/settings/secrets/actions)에서 **New repository secret**을 선택하고 이름은 `ENV_FILE`, 값은 **운영용 backend .env 파일의 내용 전체**를 붙여 넣는다. 줄바꿈을 그대로 유지한다. 파일 경로나 base64 문자열을 넣는 방식이 아니다.

다음 키가 모두 필요하다. DB 접속 정보와 JWT 키는 현재 운영 값을 유지한다.

```dotenv
DATABASE_URL=postgresql+asyncpg://<계정>:<비밀번호>@<DB호스트>:5432/<DB이름>
JWT_SECRET_KEY=<현재 운영 서명키>
AWS_REGION=<기존 S3 버킷 리전>
S3_BUCKET_NAME=<기존 비공개 버킷 이름>
S3_PRESIGNED_URL_EXPIRE_SECONDS=300
AI_BASE_URL=http://127.0.0.1:8001
AI_SERVICE_TOKEN=<AI 서버의 MOA_INTERNAL_TOKEN과 동일한 값>
```

`SSH_HOST`, `SSH_USER`, `SSH_KEY` Secret은 그대로 필요하다. 새 workflow는 개별 S3 Secrets 대신 ENV_FILE에서 S3 설정도 읽는다. 기존 main의 workflow가 아직 개별 S3 Secrets를 사용한다면 새 workflow 배포가 완료되기 전에는 기존 Secrets를 제거하지 않는다.

설정 값은 SSH 표준입력으로 전달하며 로그·명령 인자에 넣지 않는다. dotenv 문법·필수 키·DB/AI URL·JWT 길이·S3 값을 검증하고 새 설정으로 Alembic을 실행한다. 검증 또는 마이그레이션 실패 시 기존 환경파일은 보존하고 서비스를 재시작하지 않는다.

마이그레이션 성공 후 `.env`와 systemd용 `.env.systemd`를 각각 원자적으로 교체하고 권한을 `0600`으로 제한한다. 후자는 기본값까지 포함하므로 과거 db.conf/S3 drop-in 값이 새 설정을 덮어쓰지 않는다. API와 worker의 전용 drop-in에 같은 EnvironmentFile을 연결한다. 파일 설치·서비스 재시작 단계 실패 시 배포가 실패로 종료되며, 이미 성공한 DB 마이그레이션을 자동으로 되돌리지는 않는다.

dotenv는 shell로 실행하지 않는다. `${변수}`, `$()`, backtick, `%`는 값 그대로 취급하므로 다른 변수를 참조하는 형식 대신 완성된 값을 넣는다. Secret 업데이트만으로 배포가 실행되지는 않으며 **이 workflow가 포함된 main 배포 시** 반영된다.

| 설정 | 용도 |
|---|---|
| `DATABASE_URL`, `JWT_SECRET_KEY` | 기존 backend 설정 |
| `AI_BASE_URL` | moa-ai 내부 주소, 예: `http://127.0.0.1:8001` |
| `AI_SERVICE_TOKEN` | AI의 `MOA_INTERNAL_TOKEN`과 동일한 토큰 |
| `AWS_REGION`, `S3_BUCKET_NAME` | 기존 비공개 첨부 저장소 |

AI의 `MOA_ALLOWED_TENANTS`에도 backend 기업 ID를 등록한다. 요청은 서비스 Bearer 토큰과 `X-Tenant-ID`를 사용한다. AI 설정 누락은 기존 API 기동을 막지 않지만 문서 인덱싱은 실패한다.

```bash
.venv/bin/pip install -r requirements.txt
# Ubuntu
sudo apt-get install tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng
tesseract --list-langs
.venv/bin/alembic upgrade head
.venv/bin/python -m app.workers.documents
```

macOS에서는 `brew install tesseract tesseract-lang`으로 OCR을 준비한다. S3 `GetObject` 권한으로 DB에 저장된 key의 원본을 읽는다. presigned URL은 사용하지 않는다.

## 별도 worker 프로세스

다음 unit은 Ubuntu 경로 예시다. 설치·활성화는 운영 배포 단계에서 수행한다. 기존 API의 systemd 환경설정은 worker에 자동 상속되지 않으므로 DB·S3·AI 설정을 worker에도 제공한다.

```ini
[Unit]
Description=MOA document indexing worker
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/moa-backend
EnvironmentFile=/home/ubuntu/moa-backend/.env.systemd
ExecStart=/home/ubuntu/moa-backend/.venv/bin/python -m app.workers.documents
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

ENV_FILE 배포는 API와 이미 실행 중인 worker를 재시작한다. 미설치 worker의 unit 생성·활성화는 운영 도입 단계에서 별도로 수행한다. 위 `.env.systemd`는 배포 도구가 생성하는 파일이며 직접 편집하지 않는다.

## 접수와 검색 상태

- 신규 문서는 `doc_id` 생략, `version=1`. 이후 기존 숫자 ID와 다음 버전을 전달한다. 원문을 덮어쓰지 않는다.
- 첨부는 multipart `purpose=document`로 업로드한 본인 숫자 ID를 사용한다. 문의 첨부와 교차 연결·재사용할 수 없다.
- K01 `202`는 원본·작업 보존과 이전 버전 차단 확인이다. 추출·인덱싱 결과는 목록에서 확인한다.
- K01 `503 DOCUMENT_SYNC_FAILED`에도 원본·작업은 보존된다. 같은 키·본문으로 재시도한다. 새 키로 중복 등록하지 않는다.
- K03 동일 상태 재시도는 저장된 미확정 event를 사용한다. 미확정 차단 중 다른 변경은 `409 DOCUMENT_SYNC_PENDING`이다.
- 외부 호출 전에 변경 의도·revision·I03 제출 가능성을 commit한다. I04에서 소스가 없으면 검색 불가 manifest로 fence를 세운 뒤 같은 event를 다시 확인한다.
- 과거 버전은 재활성화할 수 없다. 새 버전을 등록한다. 새 버전 실패 시 과거 자료로 자동 복귀하지 않는다.
- `completed`만으로 검색 가능하다고 판단하지 않는다. 현재 embedding 공간·최신 버전·revision·상태가 일치하고 I05가 허용할 때만 `searchable=true`다.
- 목록 조회 실패는 마지막 인덱싱 상태와 `sync_error`, `searchable=false`로 표시한다. 약 10초의 조회 예산 이후 항목은 `AI_SYNC_TIMEOUT`이다. 개별 호출 제한도 10초이므로 이미 시작한 마지막 호출 때문에 전체 예산을 초과할 수 있다.

## 제한과 복구

파일 10MiB, PDF 20페이지, 이미지/렌더링 페이지 2천만 픽셀, 페이지 OCR 30초, 전체 추출 300초 제한이다. 내장 텍스트가 없는 PDF 페이지와 이미지는 `kor+eng` OCR한다. 별도 자식 프로세스에서 순차 실행하고 제한 초과 시 Tesseract 자식까지 종료한다.

손상·암호화·빈 결과·한도 초과는 오류로 보존한다. 일부 내용을 임의로 잘라내지 않는다. AI의 200,000자·64 chunk 제한도 적용한다. OCR 정확도는 원본 품질에 영향을 받으므로 정확한 문구가 중요한 자료는 텍스트 또는 텍스트 PDF를 우선 사용한다.

lease는 600초다. 종료된 worker의 작업은 만료 후 다른 worker가 인수한다. 오래된 revision 결과는 반영하지 않으며, 이미 저장된 추출 결과·hash는 재시도에서도 동일하게 사용한다.

네트워크 오류는 최초 시도 후 5·30·120초 간격으로 3회 재시도한다. AI에서 300초 이상 대기하거나 실패한 작업은 I05 확인 후 최대 3회 I03으로 재접수한다. 설정·원본 오류나 재시도 소진은 `failed`로 보존한다.

```bash
# 한 작업의 한 단계만 실행
.venv/bin/python -m app.workers.documents --once
# 원인을 해결한 뒤 현재 revision의 실패 작업 재시도
.venv/bin/python -m app.workers.documents --retry-job 123
```

운영자는 `document_jobs`의 `job_id/phase/last_error/available_at/lease_until`을 확인한다. 고객용 범용 작업 API는 추가하지 않는다. 오류 로그에는 원문·토큰·S3 URL을 기록하지 않는다.

## 검증과 배포 확인

자동 테스트는 임시 PostgreSQL의 테스트별 schema, AI HTTP 계약/실패 대역, 실제 Tesseract를 사용한다. OCR 통합 테스트는 `kor+eng`나 한글 글꼴이 없으면 skip되므로 결과의 skip 여부도 확인한다.

실제 배포 전 개발 환경에서 S3 업로드·원본 읽기, AI 허용 tenant·토큰, worker 실행을 확인한다. 실제 AI와 문서 등록 → I05 완료·검색 가능 → 버전 2 등록 → 버전 1 검색 제외 → 버전 2 검색 가능을 검증하고 재검토 설정·해제와 프로세스 재시작도 확인한다.

실제 AI/S3 서버 연결 검증은 HTTP 대역 테스트와 별도이며 연결 정보와 접근 권한이 필요하다. 이 작업에서 운영 마이그레이션·배포는 실행하지 않았다. `0005` 이하 downgrade는 문서 기록을 제거하므로 운영 롤백은 백업·데이터 보존 방안을 확인한 뒤 별도로 수행한다.

계약 근거: [moa-ai schema](https://github.com/MOA-teams/moa-ai/blob/main/app/schemas/rag.py), [상태·revision 처리](https://github.com/MOA-teams/moa-ai/blob/main/app/services/rag/chroma_store.py), [Tesseract 설치](https://tesseract-ocr.github.io/tessdoc/Installation.html).
