"""고객 발송 본문에서 명백한 코드 블록과 소스 파일 경로를 차단한다."""

import html
import re
import unicodedata


CODE_BLOCK = re.compile(r"```|~~~|<\s*/?\s*(?:pre|code)\b", re.IGNORECASE)
SOURCE_FILE = re.compile(
    r"(?<![\w])(?:[\w.@-]+[/\\])*[\w@-]+"
    r"\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|java|kt|kts|go|rs|rb|php|swift|sql|sh|c|h|cpp|hpp|cs)"
    r"(?![A-Za-z0-9_])|(?<![\w])\.env(?:\.[\w-]+)?(?![\w])",
    re.IGNORECASE,
)


def contains_internal_content(text: str) -> bool:
    # 화면이 해석할 HTML entity·전각 문자를 검사한다. 저장할 원문은 바꾸지 않는다.
    visible = unicodedata.normalize("NFKC", html.unescape(text))
    return bool(CODE_BLOCK.search(visible) or SOURCE_FILE.search(visible))
