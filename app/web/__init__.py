from fastapi import APIRouter

from app.web.admin import router as admin_router
from app.web.auth import router as auth_router
from app.web.checkout import router as checkout_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(admin_router)
router.include_router(checkout_router)
