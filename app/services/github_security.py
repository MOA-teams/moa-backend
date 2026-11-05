"""파일은 허용 경로 안에서만 읽고, 원문을 DB/AI로 전달하기 전에 비밀값을 마스킹한다."""

import fnmatch
import hashlib
import re
from pathlib import PurePosixPath
from app.schemas.github import BASE_EXCLUDES

SECRET = re.compile(
    r"FAKE_KEY_moa_demo_[A-Za-z0-9_]+|(?:AKIA|ASIA)[A-Z0-9]{16}|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+|sk_(?:live|test)_[A-Za-z0-9]+|sk-(?:proj-)?[A-Za-z0-9_-]{16,}|(?i:(?:password|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*[\"'][^\"']{4,}[\"'])"
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
)
EXTENSIONS = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".py",
    ".java",
    ".kt",
    ".go",
    ".rs",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".rb",
    ".css",
    ".html",
    ".sql",
    ".c",
    ".h",
    ".cpp",
    ".swift",
}


def includes(path, prefixes):
    return any(
        prefix == "."
        or path == prefix.rstrip("/")
        or path.startswith(prefix.rstrip("/") + "/")
        for prefix in prefixes
    )


def exclusion(path, prefixes, patterns):
    parts = PurePosixPath(path).parts
    if (
        path.startswith("/")
        or ".." in parts
        or "\\" in path
        or any(ord(c) < 32 for c in path)
    ):
        return "invalid_path"
    if not includes(path, prefixes):
        return "outside_watch"
    for pattern in dict.fromkeys(BASE_EXCLUDES + patterns):
        if pattern.endswith("/"):
            if includes(path, [pattern]) or pattern.rstrip("/") in parts[:-1]:
                return "excluded_pattern"
        elif any(
            fnmatch.fnmatchcase("/".join(parts[i:]), pattern) for i in range(len(parts))
        ) or any(fnmatch.fnmatchcase(part, pattern) for part in parts):
            return "excluded_pattern"
    if any(p in {"node_modules", ".git", ".venv", "vendor"} for p in parts):
        return "dependency_directory"
    return None


def sanitize(text, path):
    findings = []

    def private(match):
        findings.append(
            {
                "path": path,
                "line": text[: match.start()].count("\n") + 1,
                "kind": "private_key",
            }
        )
        return "[REDACTED PRIVATE KEY]" + "\n" * match.group().count("\n")

    text = PRIVATE_KEY.sub(private, text)
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if SECRET.search(line):
            findings.append({"path": path, "line": i + 1, "kind": "secret_pattern"})
            lines[i] = "[REDACTED SECRET LINE]\n"
    return "".join(lines), findings


def source_payload(integration, entry, sha, text, kind, commit_message):
    source_id = (
        f"github:{integration.repository_id}:{kind}:"
        + hashlib.sha256(entry["path"].encode()).hexdigest()[:32]
    )
    return {
        "tenant_id": integration.tenant_id,
        "source_type": kind,
        "source_id": source_id,
        "version": sha,
        "source_revision": 1,
        "title": f"{integration.repository}:{entry['path']}",
        "text": text,
        "visibility": "내부 전용",
        "status": "유효",
        "review_required": False,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "metadata": {
            "repository": integration.repository,
            "path": entry["path"],
            "commit_sha": sha,
            "security_level": integration.security_level,
            "commit_message": commit_message,
        },
        "evidence": {"docs": [], "code": [{"path": entry["path"], "commit_sha": sha}]},
    }
