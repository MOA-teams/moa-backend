"""기존 Uvicorn 및 IDE 실행 설정을 유지하기 위한 호환 진입점."""

from app.main import app

__all__ = ["app"]
