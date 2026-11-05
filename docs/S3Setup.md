# S3 첨부 인프라 준비·연동

## 저장 방식

`louter`의 백엔드 업로드·S3 key 저장·presigned GET 방식에 맞춘다.

1. 고객이 Bearer token으로 POST /api/v1/attachments에 multipart file 하나를 보낸다.
2. 백엔드가 PNG/JPEG/PDF의 확장자·MIME·시그니처, 빈 파일·파일명·10MiB 제한을 검증한다.
3. 백엔드가 `attachments/{tenant_id}/{user_id}/{uuid}.{ext}`에 비공개 객체를 저장한다.
4. DB에 업로더·기업·key·파일 메타데이터를 저장하고 숫자 attachment_id를 반환한다.
5. 고객이 문의·메시지의 attachments 배열로 첨부 ID를 연결한다. 첨부 하나는 메시지 하나에만 연결한다.
6. 다운로드 직전에 GET /api/v1/attachments/{attachment_id}로 기본 300초의 GET URL을 발급받는다.

URL 소지자는 만료 전까지 접근할 수 있다. URL을 DB나 로그에 저장하지 않는다. AWS 임시 자격 증명이 먼저 만료되면 URL도 더 일찍 만료될 수 있다.
AWS 자격 증명이나 S3 key를 프론트 요청으로 받지 않는다. 비공개 key는 DB에서만 조회하며 외부에는 첨부 ID를 사용한다.

## 인프라 담당자가 해야 할 일

- 개발·운영의 MOA 첨부 전용 비공개 버킷을 각각 생성하고 해당 환경 백엔드와 같은 리전을 사용한다.
- Block Public Access 전체 활성화, Bucket owner enforced로 ACL 비활성화, 기본 SSE-S3 적용.
- 버킷 정책에서 aws:SecureTransport=false를 거절한다.
- 백엔드 EC2에 IAM Role을 연결하고 해당 버킷 `attachments/*`에만 아래 권한을 허용한다.
  - s3:PutObject
  - s3:GetObject
  - s3:DeleteObject
- EC2가 S3 HTTPS 엔드포인트에 접근할 수 있도록 한다. 사용자가 브라우저에서 다운로드해야 하므로 모든 GET을 EC2/VPC 전용으로 제한하지 않는다.
- 로컬에는 개발 버킷에만 접근할 수 있는 AWS profile 인증 방법을 제공한다. 운영 장기 Access Key를 나눠 쓰지 않는다.
- S3 CORS는 아래 origin의 GET·HEAD만 허용한다. 실제 사용 origin이 바뀌면 백엔드 담당자에게도 전달한다.
  - http://localhost:5173
  - http://localhost:3000
  - https://moa-client.vercel.app
  - https://moa-admin-teal.vercel.app
- 브라우저가 업로드할 곳은 백엔드이므로 S3 PUT CORS는 필요하지 않다. CORS 자체가 S3 접근 권한을 부여하는 것은 아니다.
- 백엔드 HTTPS·인증서·리버스 프록시를 구성하고 프록시 본문 제한을 12MiB로 설정한다. 파일 제한 10MiB와 multipart 부가 용량을 구분한다.
- 운영 프로세스에 AWS_REGION, S3_BUCKET_NAME, S3_PRESIGNED_URL_EXPIRE_SECONDS를 주입한다.
- 배포 시 신규 코드·의존성 준비 후 Alembic upgrade head를 실행하고 성공한 경우에만 서버를 재시작한다.
- main 반영 시 CI 성공 후 배포 스크립트가 Alembic을 항상 실행한다. 기본 /etc/systemd/system/moa-backend.service.d/db.conf가 있으면 기존 형식의 DATABASE_URL·JWT_SECRET_KEY를 읽고, 없으면 실행 환경 또는 프로젝트 .env를 사용한다. 파일이 없다는 이유로 마이그레이션을 건너뛰지 않는다. 설정 누락·DB 접속·마이그레이션 실패 시 서버를 재시작하지 않고 배포를 실패 처리한다.
- 스키마 변경은 커밋된 Alembic revision을 upgrade head로 자동 적용한다. 같은 revision은 재배포 시 다시 적용하지 않는다. 새 모델 변경에는 migration 파일도 함께 커밋해야 한다.
- 배포 기본 경로는 ~/moa-backend이며 필요한 경우 MOA_APP_DIR로 변경할 수 있다. 기존 형식 systemd drop-in 경로는 MOA_SERVICE_ENV_FILE로 변경할 수 있다. 두 값은 배포 셸 환경변수이며 앱의 .env 설정과 구분한다.
- 문의에 연결된 첨부가 지워지지 않도록 버킷 전체 만료 Lifecycle을 설정하지 않는다. 보관 기간·임시 첨부 정리 정책은 후속 합의한다.

## 인프라 담당자가 전달할 정보

- 환경별 버킷 이름과 리전.
- EC2 IAM Role 이름 또는 ARN, 권한 적용·접속 확인 결과.
- 로컬 개발 AWS profile 이름과 인증 방법. 장기 비밀 키를 문서·저장소에 기록하지 않는다.
- 최종 백엔드 HTTPS 주소, 프론트 origin과 적용된 S3 CORS 목록.
- 서버 환경변수 주입 방식, 프록시 업로드 제한·마이그레이션 실행 반영 여부.

## 백엔드 환경설정

기존 DATABASE_URL·JWT_SECRET_KEY는 유지하고 인프라 담당자가 전달한 값만 로컬 .env 또는 서버 실행 환경에 설정한다.

```dotenv
AWS_REGION=<해당 환경의 버킷 리전>
S3_BUCKET_NAME=<해당 환경의 버킷 이름>
S3_PRESIGNED_URL_EXPIRE_SECONDS=300
```

S3 URL 유효기간은 1~3600초이며 기본 300초다. S3 설정이 없으면 첨부 API는 503 STORAGE_UNAVAILABLE이다.
S3 설정과 자격 증명은 첨부 기능을 사용할 때 확인하므로 앱 import·헬스체크·첨부 없는 문의 접수는 가능하다.

로컬 AWS profile은 Boto3 기본 자격 증명 체인이 읽도록 **쉘 환경변수로** 선택한다. AWS_PROFILE을 .env에만 적는 방식은 지원하지 않는다.

```bash
export AWS_PROFILE=moa-dev
```

EC2 운영 환경에서는 AWS_PROFILE·장기 Access Key를 지정하지 않고 instance Role을 사용한다.

별도 worktree에서 실행:

```bash
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
# 공유 업무 DB를 가리키지 않는지 확인한 개발 DB에만 적용
python -m alembic upgrade head
python -m uvicorn app.main:app --reload --port 8001
```

기존 인증 개발 서버와의 포트 충돌을 피하기 위해 8001을 사용한다. 기존 인증 폴더의 .env·가상환경은 변경하지 않는다.
테스트는 python -m pytest -q로 실행하며 DATABASE_URL이 가리키는 DB 안에서 UUID 이름의 임시 스키마를 생성·삭제한다. 일반 업무 테이블에는 마이그레이션하지 않는다.

## 실제 버킷 준비 후 확인 순서

- 개발 환경 S3 설정·AWS profile 인증을 적용한다.
- customer 계정으로 로그인하고 기업 소속을 연결한다.
- Swagger에서 PNG/JPEG/PDF를 업로드하고 attachment_id·메타데이터·한시적 URL을 확인한다.
- 서명이 없는 객체 URL이 공개되지 않는지 확인한다.
- 첨부 ID로 문의를 생성하고 고객 이력에서 첨부 메타데이터를 확인한다.
- 같은 기업 agent는 연결된 첨부에 접근할 수 있고 다른 기업·다른 고객은 404인지 확인한다.
- 문의 연결 전 타인의 임시 첨부는 같은 기업 agent도 404인지 확인한다.
- presigned URL 다운로드·만료·재발급, 프론트 fetch/PDF 표시의 S3 CORS를 확인한다.
- S3 설정이 없는 프로세스에서 첨부 요청만 503이고 첨부 없는 문의·헬스체크는 정상인지 확인한다.
- 테스트 업로드는 개발 버킷에서만 수행하고 검증 후 수동 정리한다.

## 실패·정리 범위

S3 저장 후 DB 저장·서명 URL 생성에 실패하면 해당 UUID key의 객체를 삭제한다.
보상 삭제가 실패하면 attachment_cleanup_failed와 request_id·storage_key를 기록하고 원본 파일명·파일 내용·서명 URL·AWS 예외는 로그에 남기지 않는다.
로그에 남은 key는 DB 연결 유무를 확인한 뒤 수동 정리한다. 프로세스 중단으로 로그 없이 객체가 남을 수 있으며 정기 고아 객체 정리는 이번 범위에 포함하지 않았다.

공식 동작 참고: [비공개 S3 공개 접근 차단](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html), [EC2 Role](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html), [presigned URL](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html).
