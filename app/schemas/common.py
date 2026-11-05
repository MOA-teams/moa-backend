from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="프론트에서 오류 유형을 구분하는 코드", examples=["VALIDATION_ERROR"])
    message: str = Field(description="오류 안내 메시지. 요청의 비밀번호나 토큰은 포함하지 않습니다.", examples=["요청 값이 올바르지 않습니다."])
    request_id: str = Field(description="서버가 생성한 요청 추적 ID. 응답의 X-Request-ID 헤더와 같습니다.", examples=["REQ-00000000000000000000000000000001"])


class ErrorResponse(BaseModel):
    """예상 가능한 API 오류의 공통 응답 구조."""

    error: ErrorDetail = Field(description="오류 코드·메시지·요청 추적 ID")
