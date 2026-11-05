from fastapi import APIRouter

from app.api.v1 import attachments, auth, tenants

router = APIRouter()
router.include_router(auth.router)
router.include_router(tenants.router)
router.include_router(attachments.router)
