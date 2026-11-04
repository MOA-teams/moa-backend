from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.errors import install_error_handlers
from app.db.session import engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(
    title="MOA Backend",
    version="0.1.0",
    lifespan=lifespan,
)
install_error_handlers(app)
app.include_router(api_router)
