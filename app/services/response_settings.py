"""Resolve current tenant delivery policy; settings never bypass AI/evidence checks."""

from app.core.auto_response import AutoResponsePolicy
from app.core.config import get_settings
from app.core.enums import InquiryCategory
from app.core.errors import APIError
from app.db.models import TenantResponsePolicy
from app.schemas.response_settings import ResponseSettingsUpdate, ResponseSettingsView
from app.services.knowledge import lock_knowledge


async def resolve_policy(session, tenant):
    row = await session.get(TenantResponsePolicy, tenant, populate_existing=True)
    if row is not None:
        return AutoResponsePolicy.model_validate(row.policy)
    return get_settings().auto_response_policies.get(tenant)


async def settings_view(session, tenant):
    row = await session.get(TenantResponsePolicy, tenant, populate_existing=True)
    configured = get_settings().auto_response_policies.get(tenant)
    policy = (
        AutoResponsePolicy.model_validate(row.policy)
        if row
        else (configured or AutoResponsePolicy())
    )
    return ResponseSettingsView(
        tenant_id=tenant,
        revision=row.revision if row else 0,
        questions_enabled=policy.questions_enabled,
        answers_enabled=policy.answers_enabled,
        max_questions=policy.max_questions,
        updated_at=row.updated_at if row else None,
        source="database" if row else ("environment" if configured else "default"),
    )


async def update_settings(session, user, payload: ResponseSettingsUpdate):
    try:
        # Same tenant lock as final evidence validation: disabling and delivery serialize.
        if await lock_knowledge(session, user.tenant_id) is None:
            raise APIError(404, "TENANT_NOT_FOUND", "기업을 찾을 수 없습니다.")
        row = await session.get(
            TenantResponsePolicy, user.tenant_id, populate_existing=True
        )
        if payload.expected_revision != (row.revision if row else 0):
            raise APIError(
                409,
                "SETTINGS_CONFLICT",
                "설정이 변경됐습니다. 새로 조회한 뒤 저장하세요.",
            )
        policy = await resolve_policy(session, user.tenant_id)
        if policy is None:
            policy = AutoResponsePolicy(
                categories=[
                    InquiryCategory.USAGE,
                    InquiryCategory.ACCOUNT,
                    InquiryCategory.BILLING,
                ],
                allow_current_public_documents=True,
            )
        values = policy.model_dump(mode="json")
        values.update(
            payload.model_dump(exclude_unset=True, exclude={"expected_revision"})
        )
        validated = AutoResponsePolicy.model_validate(values)
        if row is None:
            row = TenantResponsePolicy(
                tenant_id=user.tenant_id,
                revision=1,
                policy=validated.model_dump(mode="json"),
                updated_by=user.user_id,
            )
            session.add(row)
        else:
            row.policy = validated.model_dump(mode="json")
            row.revision += 1
            row.updated_by = user.user_id
        await session.commit()
        return await settings_view(session, user.tenant_id)
    except Exception:
        await session.rollback()
        raise
