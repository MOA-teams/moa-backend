from enum import Enum


class UserRole(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    ADMIN = "admin"


class InquiryCategory(str, Enum):
    USAGE = "이용 방법"
    ACCOUNT = "계정"
    BILLING = "결제"
    BUG = "기능 오류"
    FEATURE_REQUEST = "개선 요청"
    OTHER = "기타"


class InquiryUrgency(str, Enum):
    HIGH = "높음"
    NORMAL = "보통"
    LOW = "낮음"


class InquiryStatus(str, Enum):
    RECEIVED = "접수"
    IN_REVIEW = "담당자 확인 중"
    NEEDS_INFO = "추가 정보 필요"
    ANSWERED = "답변 완료"
    CLOSED = "상담 종료"


class MessageSender(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    AI_AUTO = "ai_auto"


class MessageType(str, Enum):
    CUSTOMER_MESSAGE = "고객 메시지"
    ACKNOWLEDGEMENT = "접수 안내"
    INFORMATION_REQUEST = "정보 수집 질문"
    FINAL_ANSWER = "최종 답변"


class RunStatus(str, Enum):
    PENDING = "대기"
    RUNNING = "처리 중"
    COMPLETED = "완료"
    FAILED = "실패"

class DraftResult(str, Enum):
    READY = "초안 가능"
    MANUAL = "직접 확인 필요"
    NEED_INFO = "추가 질문 필요"

class CheckStatus(str, Enum):
    PASS = "통과"
    FAIL = "실패"
    UNKNOWN = "확인 불가"
    SKIPPED = "미실행"
