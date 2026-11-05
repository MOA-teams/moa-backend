"""자동 응대의 서버 설정과 결정적 내용 검사. AI의 허용 판단만으로 발송하지 않는다."""

import re
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field
from app.core.enums import InquiryCategory

TenantID = Annotated[str, Field(pattern=r"^[a-z]{1,64}$")]
DocumentID = Annotated[str, Field(pattern=r"^DOC-[0-9]{3}$")]


class AutoResponsePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions_enabled: bool = False
    answers_enabled: bool = False
    categories: list[InquiryCategory] = Field(
        default_factory=lambda: [InquiryCategory.USAGE]
    )
    # 문서 새 버전은 공개 자동 발송 허용을 다시 확인해야 한다.
    public_documents: dict[DocumentID, Annotated[int, Field(strict=True, ge=1)]] = (
        Field(default_factory=dict)
    )
    max_questions: int = Field(default=2, strict=True, ge=1, le=3)


REQUIRED_CHECKS = frozenset(
    {
        "evidence_current",
        "source_conflict",
        "claims_supported",
        "internal_data_excluded",
        "auto_response_scope",
    }
)

# AI 질문 원문은 검수되지 않는다. 허용된 정보 항목만 고정 문구로 질문한다.
QUESTION_TEMPLATES = {
    "발생 화면": "어떤 화면에서 문제가 발생했나요?",
    "재현 단계": "문제가 발생하기 직전에 어떤 동작을 했는지 알려주세요.",
    "발생 환경": "사용 중인 운영체제와 브라우저 종류를 알려주세요.",
    "앱 버전": "사용 중인 앱 버전을 알려주세요.",
    "오류 코드": "화면에 표시된 오류 코드가 있으면 알려주세요.",
}
QUESTION_FIELDS = {
    "발생 화면": "발생 화면",
    "화면": "발생 화면",
    "screen": "발생 화면",
    "재현 단계": "재현 단계",
    "재현 방법": "재현 단계",
    "steps_to_reproduce": "재현 단계",
    "발생 환경": "발생 환경",
    "환경": "발생 환경",
    "os": "발생 환경",
    "앱 버전": "앱 버전",
    "app_version": "앱 버전",
    "오류 코드": "오류 코드",
    "error_code": "오류 코드",
}

INTERNAL_CONTENT = re.compile(
    r"```|<[^>]+>|(?:https?://|www\.)|(?:[\w.-]+/[\w./-]+\.(?:py|ts|tsx|js|jsx|java|go|sql|env))"
    r"|\b(?:[a-f0-9]{7,40}|FAKE_KEY_\w+|AKIA[A-Z0-9]+|sk-[\w-]+)\b"
    r"|\b[A-Za-z_]\w*\([^)]*\)|(?:password|api[_ -]?key|authorization|비밀번호|인증번호|주민등록번호)"
    r"|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    re.IGNORECASE,
)


def safe_answer(text: str) -> bool:
    return (
        bool(text.strip()) and len(text) <= 10000 and not INTERNAL_CONTENT.search(text)
    )


def safe_question(fields: list[str]) -> str | None:
    normalized = list(dict.fromkeys(QUESTION_FIELDS.get(f.strip()) for f in fields))
    if not normalized or None in normalized or len(normalized) > 3:
        return None
    return "\n".join(QUESTION_TEMPLATES[field] for field in normalized)
