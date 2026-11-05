from enum import Enum


class UserRole(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    ADMIN = "admin"


class InquiryCategory(str, Enum):
    USAGE = "usage"
    ACCOUNT = "account"
    BILLING = "billing"
    BUG = "bug"
    FEATURE_REQUEST = "feature_request"
    OTHER = "other"


class InquiryUrgency(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class InquiryStatus(str, Enum):
    RECEIVED = "received"
    IN_REVIEW = "in_review"
    NEEDS_INFO = "needs_info"
    ANSWERED = "answered"
    CLOSED = "closed"


class MessageSender(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    AI_AUTO = "ai_auto"


class MessageType(str, Enum):
    CUSTOMER_MESSAGE = "customer_message"
    ACKNOWLEDGEMENT = "acknowledgement"
    INFORMATION_REQUEST = "information_request"
    FINAL_ANSWER = "final_answer"
