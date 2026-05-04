from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.routers.deps import require_permission
from app.services.audit_log import CATEGORIES, SEVERITIES, list_events, serialize_event

router = APIRouter(prefix="/audit", tags=["audit"])


def _parse_datetime_filter(value: str | None, field_name: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name} filter")


@router.get("/events")
def audit_events(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    category: str | None = Query(None),
    severity: str | None = Query(None),
    event_type: str | None = Query(None, max_length=160),
    actor: str | None = Query(None, max_length=120),
    target: str | None = Query(None, max_length=120),
    target_type: str | None = Query(None, max_length=80),
    target_id: str | None = Query(None, max_length=120),
    date_from: str | None = Query(None, max_length=40),
    date_to: str | None = Query(None, max_length=40),
    since_minutes: int | None = Query(None, ge=1, le=60 * 24 * 30),
    q: str | None = Query(None, min_length=1, max_length=120),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    if category and category not in CATEGORIES:
        raise HTTPException(status_code=422, detail="Invalid category filter")
    if severity and severity not in SEVERITIES:
        raise HTTPException(status_code=422, detail="Invalid severity filter")
    parsed_date_from = _parse_datetime_filter(date_from, "date_from")
    parsed_date_to = _parse_datetime_filter(date_to, "date_to")
    if parsed_date_from and parsed_date_to and parsed_date_from > parsed_date_to:
        raise HTTPException(status_code=422, detail="Invalid date range")
    events = list_events(
        db,
        limit=limit,
        offset=offset,
        category=category,
        severity=severity,
        event_type=event_type,
        actor=actor,
        target=target,
        target_type=target_type,
        target_id=target_id,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        since_minutes=since_minutes,
        q=q,
    )
    return {
        "items": [serialize_event(event) for event in events],
        "count": len(events),
        "limit": limit,
        "offset": offset,
    }
