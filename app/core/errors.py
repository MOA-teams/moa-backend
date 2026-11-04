from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {
            "code": code,
            "message": message,
            "request_id": request.state.request_id,
        }},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def assign_request_id(request: Request, call_next):
        request.state.request_id = f"REQ-{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, error: APIError) -> JSONResponse:
        return error_response(request, error.status_code, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # 검증 오류의 원본 입력에는 평문 비밀번호가 포함될 수 있다.
        return error_response(request, 400, "VALIDATION_ERROR", "요청 값이 올바르지 않습니다.")
