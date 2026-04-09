from fastapi import APIRouter

from src.api.v1 import meetings, budget

router = APIRouter(prefix="/api/v1")

router.include_router(meetings.router)
router.include_router(budget.router)
