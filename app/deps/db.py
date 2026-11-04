from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """요청 종료 시 세션을 닫는다. 커밋은 업무 서비스에서 명시적으로 수행한다."""
    async with async_session_factory() as session:
        yield session
