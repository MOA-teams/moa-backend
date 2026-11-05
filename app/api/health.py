from fastapi import APIRouter

from app.api.documentation import documented_success

router = APIRouter(tags=["system"])


@router.get(
    "/health", summary="서버 응답 상태 확인",
    description="인증 없이 서버 응답 여부를 확인합니다. DB 연결이나 외부 서비스 상태를 검사하는 엔드포인트는 아닙니다.",
    response_description="앱이 응답하면 status=ok",
    responses={200: documented_success("서버 응답 정상", {
        "healthy": {"summary": "정상 응답", "value": {"status": "ok"}},
    })},
)
async def health() -> dict[str, str]:
    return {"status": "ok"}
