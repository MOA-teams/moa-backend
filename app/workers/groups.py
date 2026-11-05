"""그룹 만료 평가(5초)와 Issue·문의 요약 outbox 실행. python -m app.workers.groups"""
import asyncio
import logging
from contextlib import suppress

from app.services.groups import tick
from app.workers.group_issues import run_once as issue_once
from app.workers.inquiry_summaries import run_once as summary_once

logger = logging.getLogger(__name__)


async def loop(factory, action, interval):
    while True:
        try:
            await action(factory)
        except Exception:
            logger.error("groups_worker_iteration_failed action=%s", action.__module__)
        await asyncio.sleep(interval)


async def run_forever(factory):
    tasks = [asyncio.create_task(loop(factory, action, interval)) for action, interval in ((tick,5),(issue_once,1),(summary_once,1))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


async def main():
    from app.db.session import async_session_factory, engine
    try:
        await run_forever(async_session_factory)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
