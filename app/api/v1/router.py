from fastapi import APIRouter

from app.api.v1 import admin_inquiries, attachments, auth, inquiries, tenants

router = APIRouter()
router.include_router(auth.router)
router.include_router(tenants.router)
router.include_router(attachments.router)
router.include_router(inquiries.router)
router.include_router(admin_inquiries.router)
