from typing import Literal
from pathlib import PurePosixPath
from pydantic import Field, SecretStr, field_validator, model_validator
from app.schemas.analysis import Contract

BASE_EXCLUDES = [".env*", "*.pem", "*.key", "secrets/", "config/prod*"]


def safe_path(value, *, pattern=False):
    if (
        not value
        or len(value) > 1000
        or value.startswith("/")
        or "\\" in value
        or ".." in PurePosixPath(value).parts
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("저장소 상대 경로가 필요합니다.")
    if not pattern and any(c in value for c in "*?[]"):
        raise ValueError("경로에는 glob을 지정할 수 없습니다.")
    return value


class AuthorizationComplete(Contract):
    state: str = Field(min_length=32, max_length=200)
    code: SecretStr


class DocumentBinding(Contract):
    path: str
    doc_id: str = Field(pattern=r"^DOC-[0-9]{3}$")

    @field_validator("path")
    @classmethod
    def path_valid(cls, value):
        return safe_path(value)


class GitHubConnect(Contract):
    authorization_id: str | None = Field(default=None, max_length=200)
    installation_id: int | None = Field(default=None, strict=True, gt=0)
    repository: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200
    )
    watch_branch: str = Field(default="main", min_length=1, max_length=200)
    security_level: Literal[0, 1, 2] = 2
    include_paths: list[str] = Field(default_factory=list, max_length=30)
    exclude_patterns: list[str] = Field(
        default_factory=lambda: list(BASE_EXCLUDES), max_length=50
    )
    document_bindings: list[DocumentBinding] = Field(
        default_factory=list, max_length=50
    )

    @field_validator("security_level", mode="before")
    @classmethod
    def strict_level(cls, value):
        if type(value) is not int:
            raise ValueError("보안 레벨은 정수여야 합니다.")
        return value

    @field_validator("include_paths", "exclude_patterns")
    @classmethod
    def paths_valid(cls, value, info):
        return list(
            dict.fromkeys(
                safe_path(v, pattern=info.field_name == "exclude_patterns")
                for v in value
            )
        )

    @field_validator("watch_branch")
    @classmethod
    def branch_valid(cls, value):
        if (
            any(c in value for c in "~^:?*[\\")
            or ".." in value
            or "@{" in value
            or value.startswith(("/", "-"))
            or value.endswith(("/", ".", ".lock"))
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError("유효한 Git 브랜치가 필요합니다.")
        return value

    @model_validator(mode="after")
    def enabled_fields(self):
        if self.security_level and (
            not self.installation_id
            or not self.repository
            or not self.authorization_id
            or not self.include_paths
        ):
            raise ValueError("인증된 설치·저장소·허용 경로가 필요합니다.")
        return self


class GitHubAccepted(Contract):
    integration_id: str
    status: str


class GitHubStatus(GitHubAccepted):
    installation_id: int | None
    repository: str | None
    watch_branch: str
    security_level: int
    include_paths: list[str]
    exclude_patterns: list[str]
    document_bindings: list[DocumentBinding]
    analysis_provider: Literal["external_llm", "none"]
    last_synced_sha: str | None
    last_synced_at: str | None
    error_code: str | None
    report: dict


class AuthorizationStart(Contract):
    authorization_url: str
    installation_url: str
    expires_in: int


class AuthorizedRepository(Contract):
    installation_id: int
    repository_id: int
    repository: str
    default_branch: str


class AuthorizationResult(Contract):
    authorization_id: str
    repositories: list[AuthorizedRepository]
    expires_in: int


class Branch(Contract):
    name: str
    sha: str


class BranchSelection(Contract):
    branches: list[Branch]


class PathSelection(Contract):
    sha: str
    paths: list[str]


class WebhookAccepted(Contract):
    accepted: bool
    event_id: str
