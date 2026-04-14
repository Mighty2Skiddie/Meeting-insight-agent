from fastapi import APIRouter

from src.api.v1 import budget, meetings
from src.api.v1.live import router as live_router

router = APIRouter(prefix="/api/v1")

router.include_router(meetings.router)
router.include_router(budget.router)
router.include_router(live_router, prefix="/meetings")
