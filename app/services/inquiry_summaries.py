"""문의 요약 동기화 의도. 외부 검색의 실패는 접수·답변 트랜잭션과 분리한다."""
import hashlib

from sqlalchemy import select
from app.db.models import InquirySummaryJob


async def enqueue_summary(session, inquiry, group, result=None):
    previous = list((await session.scalars(select(InquirySummaryJob).where(
        InquirySummaryJob.inquiry_id == inquiry.inquiry_id).order_by(InquirySummaryJob.source_revision))).all())
    latest = previous[-1] if previous else None
    content = result.analysis.summary if result and result.analysis else latest.payload["text"] if latest else inquiry.title
    content = content[:200000]
    metadata = {"tenant_id": inquiry.tenant_id, "inquiry_id": inquiry.public_id,
                "group_id": group.public_id if group else None,
                "category": (inquiry.category or inquiry.customer_category).value,
                "normalized_error_code": inquiry.normalized_error_code}
    status = "유효" if group else "폐기"
    if latest and (latest.payload["text"], latest.payload["metadata"], latest.payload["status"]) == (content, metadata, status):
        return latest
    revision = latest.source_revision + 2 if latest else 2
    version = (latest.version + (latest.payload["text"] != content)) if latest else 1
    payload = {"tenant_id": inquiry.tenant_id, "source_type": "문의 요약", "source_id": inquiry.public_id,
               "version": version, "source_revision": revision, "title": f"{inquiry.public_id} 문의 요약", "text": content,
               "visibility": "내부 전용", "status": status, "review_required": False,
               "content_hash": hashlib.sha256(content.encode()).hexdigest(), "metadata": metadata,
               "evidence": {"docs": [], "code": []}}
    # 이전 요청이 AI에 도착했는지 알 수 없어도 모든 버전을 높은 revision으로 먼저 차단한다.
    versions = {job.version: job.payload for job in previous}
    barriers = [{**source, "source_revision": revision - 1, "status": "폐기", "review_required": False}
                for source in versions.values()]
    job = InquirySummaryJob(inquiry_id=inquiry.inquiry_id, version=version, source_revision=revision,
                            payload=payload, barriers=barriers)
    session.add(job)
    await session.flush()
    return job
