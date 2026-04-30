from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.routers.deps import require_permission
from app.services.audit_log import list_events, serialize_event

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events")
def audit_events(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    category: str | None = Query(None),
    severity: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    events = list_events(
        db,
        limit=limit,
        offset=offset,
        category=category,
        severity=severity,
    )
    return {
        "items": [serialize_event(event) for event in events],
        "count": len(events),
        "limit": limit,
        "offset": offset,
    }
