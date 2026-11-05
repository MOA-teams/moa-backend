import asyncio
from contextlib import suppress
from app.core.config import get_settings
from app.workers.analysis import run_forever
from app.workers.groups import run_forever as run_groups
from app.db.session import async_session_factory
from app.api.pipeline import router as pipeline_router
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.documentation import API_DESCRIPTION, OPENAPI_TAGS
from app.api.router import api_router
from app.core.errors import install_error_handlers
from app.db.session import engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    task=asyncio.create_task(run_forever(async_session_factory)) if get_settings().ai_pipeline_worker_enabled else None
    groups_task=asyncio.create_task(run_groups(async_session_factory)) if get_settings().groups_worker_enabled else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if groups_task:
            groups_task.cancel()
            with suppress(asyncio.CancelledError):
                await groups_task
        await engine.dispose()


app = FastAPI(
    title="MOA Backend",
    version="0.1.0",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    swagger_ui_parameters={"displayRequestDuration": True},
    lifespan=lifespan,
)
install_error_handlers(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "https://moa-client.vercel.app",
        "https://moa-admin-teal.vercel.app",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    expose_headers=["X-Request-ID"],
)
app.include_router(api_router)

app.include_router(pipeline_router)
