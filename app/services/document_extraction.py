"""원문 보존과 AI 입력 제한 검증. PDF/OCR은 별도 프로세스에 격리한다."""
import asyncio
import hashlib
import json
import os
import signal
import sys


class ExtractionError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_text(text: str) -> tuple[str, str]:
    if not text.strip():
        raise ExtractionError("EMPTY_DOCUMENT")
    if len(text) > 200_000:
        raise ExtractionError("DOCUMENT_TEXT_LIMIT")
    # moa-ai split_source와 같은 offset 규칙. 공백 chunk는 세지 않는다.
    chunks = 0
    for start in range(0, len(text), 850):
        end = min(start + 1000, len(text))
        chunks += bool(text[start:end].strip())
        if end == len(text):
            break
    if chunks > 64:
        raise ExtractionError("DOCUMENT_CHUNK_LIMIT")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


async def extract_file(data: bytes, mime: str) -> tuple[str, str]:
    if not data or len(data) > 10 * 1024 * 1024:
        raise ExtractionError("DOCUMENT_FILE_LIMIT")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.workers.extract_document", mime,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), timeout=300)
    except (TimeoutError, asyncio.CancelledError) as exc:
        # Tesseract 자식까지 종료하여 처리 제한 이후에도 OCR이 남지 않게 한다.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ExtractionError("DOCUMENT_EXTRACTION_TIMEOUT") from None
    try:
        result = json.loads(output)
        if process.returncode != 0 or "error" in result:
            raise ExtractionError(result.get("error", "DOCUMENT_EXTRACTION_FAILED"))
        return validate_text(result["text"])
    except (ValueError, TypeError, KeyError):
        raise ExtractionError("DOCUMENT_EXTRACTION_FAILED") from None
