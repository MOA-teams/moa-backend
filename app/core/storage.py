"""비공개 S3 객체 저장과 한시적 GET URL 발급. 기본 AWS 자격 증명 체인을 사용한다."""
from functools import lru_cache
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.core.errors import APIError


def storage_error() -> APIError:
    return APIError(503, "STORAGE_UNAVAILABLE", "첨부 저장소를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.")


class S3Storage:
    def __init__(self, client, bucket: str, expires_in: int):
        self.client = client
        self.bucket = bucket
        self.expires_in = expires_in

    def put(self, key: str, file: BinaryIO, mime: str, size: int) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=file, ContentType=mime, ContentLength=size)
        except (BotoCoreError, ClientError):
            raise storage_error() from None

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError):
            raise storage_error() from None

    def read(self, key: str) -> bytes:
        """DB에서 권한 확인을 마친 객체만 읽고, 업로드 한도를 다시 적용한다."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                data = body.read(10 * 1024 * 1024 + 1)
            finally:
                body.close()
            if not data or len(data) > 10 * 1024 * 1024:
                raise APIError(400, "INVALID_STORED_FILE", "저장된 파일 크기가 올바르지 않습니다.")
            return data
        except (BotoCoreError, ClientError):
            raise storage_error() from None

    def download_url(self, key: str) -> str:
        try:
            return self.client.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.expires_in, HttpMethod="GET",
            )
        except (BotoCoreError, ClientError):
            raise storage_error() from None


@lru_cache
def get_s3_storage() -> S3Storage:
    # 앱 import 시에는 Role 메타데이터나 AWS 네트워크에 접근하지 않는다.
    settings = get_settings()
    if not settings.aws_region or not settings.s3_bucket_name:
        raise storage_error()
    try:
        client = boto3.client(
            "s3", region_name=settings.aws_region,
            config=Config(signature_version="s3v4", connect_timeout=3, read_timeout=10,
                          retries={"mode": "standard", "total_max_attempts": 2}),
        )
    except (BotoCoreError, ClientError):
        raise storage_error() from None
    return S3Storage(client, settings.s3_bucket_name, settings.s3_presigned_url_expire_seconds)
