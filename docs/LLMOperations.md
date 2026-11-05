# 실제 LLM 연결과 분석 worker 운영

## 연결 구조

백엔드 API(`127.0.0.1:8000`)의 분석 worker가 moa-ai(`127.0.0.1:8001`)에 I01을 요청하고 I02로 결과를 조회한다. 2026-11-05 확인한 AI 제공자는 AWS Bedrock이며 EC2 IAM Role로 Claude Haiku 4.5와 Titan Text Embeddings v2를 호출한다. OpenAI API 키는 사용하지 않는다.

백엔드 `ENV_FILE`에는 기존 DB·JWT·S3 설정과 다음 설정을 함께 유지한다. 비밀값을 저장소나 로그에 출력하지 않는다.

```dotenv
AI_BASE_URL=http://127.0.0.1:8001
AI_SERVICE_TOKEN=<AI의 MOA_INTERNAL_TOKEN과 동일한 값>
AI_PIPELINE_WORKER_ENABLED=true
```

AI의 `MOA_ALLOWED_TENANTS`에도 대상 기업을 허용해야 한다. 현재 검증 기업은 `syncnote`다. AWS 자격 증명·모델·RAG 설정은 AI 서비스 책임이며 백엔드에 모델 키를 복제하지 않는다.

배포 도구는 AI 주소·토큰을 필수로 검증한다. `AI_PIPELINE_WORKER_ENABLED`를 생략하면 배포 환경에서는 `true`를 적용하고, 명시한 `false`/`0`는 보존한다. 로컬 `Settings` 기본값은 계속 `false`이므로 로컬 실행에서는 직접 활성화한다. 자동 발송 정책은 별도 설정이며 worker 활성화만으로 고객에게 자동 답변을 보내지 않는다.

## 콜백과 결과 복구

콜백을 사용할 때는 백엔드 `AI_CALLBACK_TOKEN`과 AI `MOA_BACKEND_SERVICE_TOKEN`에 같은 **별도 토큰**을 설정하고 AI `MOA_BACKEND_BASE_URL`을 백엔드 내부 주소로 지정한다. I01 서비스 인증 토큰을 콜백 토큰으로 자동 대체하지 않는다.

콜백이 없어도 worker의 I02 폴링으로 결과를 반영한다. 현재 운영 검증은 이 경로로 수행했다. worker는 API lifespan에서 실행되며 별도 systemd 분석 worker는 필요하지 않다. 문서 인덱싱·GitHub 동기화 worker는 별도 프로세스다.

## 읽기 전용 점검

서버에서는 실제 서비스가 읽는 환경파일을 지정한다.

```bash
cd /home/ubuntu/moa-backend
.venv/bin/python scripts/check_ai_connection.py --tenant syncnote --env-file .env.systemd
```

점검은 `/health`와 존재하지 않는 run의 조회만 호출한다. `RUN_NOT_FOUND` 응답으로 서비스 토큰·tenant 인증 통과를 확인하며 run을 만들거나 LLM을 호출하지 않는다. `ready=true`는 설정·HTTP 인증 점검 통과다. 실행 중인 worker 상태나 실제 모델 호출 성공을 보장하지 않으므로 `llm_execution_verified=false`를 명시한다. 원격 오류 본문·토큰·DB 주소는 출력하지 않는다.

실제 생성 검증은 가상 고객으로 문의를 만들고 관리자 상세의 `run_status=완료`, 분석·초안 결과, 고객 공개 메시지를 확인한다. 문서가 없으면 `추가 질문 필요`가 정상 결과일 수 있다. 근거 기반 답변은 문서 worker와 I05 검색 가능 확인 후 별도로 검증한다.

## 2026-11-05 검증 기록

- 원인: AI 주소·토큰은 정상이었지만 ENV_FILE에 worker 플래그가 없어 이전 배포 도구가 `false`를 생성했다.
- 실제 Bedrock 생성 호출과 1024차원 임베딩 호출 성공. IAM Role 인증 사용.
- 서버 환경파일을 비공개 백업한 뒤 worker를 활성화하고 백엔드 health 확인.
- 가상 문의 `Q-1016`, run `RUN-cbb0f603a4fb49cc88100ef85dc99b0f`: 실제 AI 분석 완료, `기능 오류`·`보통`·`추가 질문 필요`를 백엔드에 저장. 아직 문서 근거 기반 초안 검증은 아님.
- 격리 PostgreSQL 전체 회귀 테스트 583개 통과. 신규 12개는 배포 기본값·명시적 비활성화·인증/통신 오류·비밀값 비노출·worker 기동/종료를 검증한다.

## 중단과 재시작

분석 소비를 중단하려면 운영 `ENV_FILE`에 `AI_PIPELINE_WORKER_ENABLED=false`를 명시하고 재배포한다. 문의 저장은 계속되며 분석 대기 작업은 DB에 남는다. 재활성화하면 worker가 저장된 작업을 처리한다. 실패 원인 해결 전에는 반복 재분석을 생성하지 않는다. 기존 문의·run을 삭제하거나 DB revision을 직접 수정하는 방식으로 복구하지 않는다.

참고: [AI 연동 계약](https://github.com/MOA-teams/moa-ai/blob/main/docs/Integration.md), [문서 worker 운영](DocumentOperations.md).
