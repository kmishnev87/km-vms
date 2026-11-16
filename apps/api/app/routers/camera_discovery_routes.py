from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.models.user import User
from app.routers.deps import require_permission
from app.services.camera_discovery_client import DiscoveryRequestError, DiscoveryUnavailable, discovery_request

router = APIRouter()


class DiscoverySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local: StrictBool = True
    networks: list[str] = Field(default_factory=list, max_length=8)
    protocols: list[str] = Field(default_factory=lambda: ["onvif", "rtsp"], min_length=1, max_length=2)
    onvif_ports: list[StrictInt] | None = Field(default=None, min_length=1, max_length=12)
    rtsp_ports: list[StrictInt] | None = Field(default=None, min_length=1, max_length=12)


def call_discovery(path, payload):
    try:
        return discovery_request(path, payload)
    except DiscoveryRequestError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code}) from exc
    except DiscoveryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "discovery_unavailable"}) from exc


@router.get("/discovery/networks")
def discovery_networks(current_user: User = Depends(require_permission("manage_cameras"))):
    return call_discovery("/networks", {"owner": str(current_user.id)})


@router.post("/discovery/jobs")
def start_discovery(payload: DiscoverySearch, current_user: User = Depends(require_permission("manage_cameras"))):
    return call_discovery("/jobs", {**payload.model_dump(exclude_none=True), "owner": str(current_user.id)})


@router.get("/discovery/jobs/{job_id}")
def discovery_status(job_id: str, current_user: User = Depends(require_permission("manage_cameras"))):
    return call_discovery("/status", {"owner": str(current_user.id), "job_id": job_id})


@router.post("/discovery/jobs/{job_id}/cancel")
def cancel_discovery(job_id: str, current_user: User = Depends(require_permission("manage_cameras"))):
    return call_discovery("/cancel", {"owner": str(current_user.id), "job_id": job_id})
