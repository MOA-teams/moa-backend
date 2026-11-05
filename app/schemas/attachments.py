from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class AttachmentMetadata(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attachment_id: str = Field(validation_alias=AliasChoices("public_id", "attachment_id"), description="외부 문자열 ID", examples=["ATT-0001"])
    file_name: str = Field(description="원본 파일명. S3 경로로 사용하지 않습니다.", examples=["error.png"])
    mime: str = Field(description="검증한 MIME 유형", examples=["image/png"])
    size: int = Field(description="파일 크기(bytes), 최대 10MiB", examples=[182344])


class AttachmentResponse(AttachmentMetadata):
    url: str = Field(description="한시적 presigned GET URL. 영구 저장하지 말고 만료 시 다시 조회하세요.")
    expires_at: datetime = Field(description="서버가 요청한 URL 유효기간의 종료 시각. AWS 임시 자격 증명이 먼저 만료되면 더 일찍 만료될 수 있습니다.")
