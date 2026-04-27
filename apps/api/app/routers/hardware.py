from fastapi import APIRouter, Depends

from app.models.user import User
from app.routers.deps import get_current_user
from app.services.hardware import get_hardware_capabilities, refresh_hardware_capabilities

router = APIRouter(prefix="/hardware", tags=["hardware"])


@router.get("/capabilities")
def hardware_capabilities(current_user: User = Depends(get_current_user)):
    # Intentional: hardware capabilities are protected because the payload exposes
    # host/container device details and ffmpeg build information.
    return get_hardware_capabilities()


@router.post("/rescan")
def hardware_rescan(current_user: User = Depends(get_current_user)):
    # Intentional: rescan may execute lightweight ffmpeg probes, so it stays authenticated.
    return refresh_hardware_capabilities()
