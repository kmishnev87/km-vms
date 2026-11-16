"""Atomic policy-only editing. Never accepts connection credentials or recording toggles."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy.orm import Session

from app.core.camera_limits import MAX_RETENTION_DAYS
from app.db.session import get_db
from app.models.camera import Camera
from app.models.user import User
from app.routers.deps import require_permission
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.retention_automation import advance_retention_signal

router = APIRouter()
FIELDS = ("segment_minutes", "retention_days", "storage_quota_gb")


class RuleValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_minutes: StrictInt = Field(ge=1, le=2147483647)
    retention_days: StrictInt = Field(ge=1, le=MAX_RETENTION_DAYS)
    storage_quota_gb: StrictInt = Field(ge=1, le=2147483647)


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_minutes: StrictInt | None = Field(default=None, ge=1, le=2147483647)
    retention_days: StrictInt | None = Field(default=None, ge=1, le=MAX_RETENTION_DAYS)
    storage_quota_gb: StrictInt | None = Field(default=None, ge=1, le=2147483647)

    @model_validator(mode="after")
    def nonempty_patch(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("archive_rule_values_required")
        return self


class RuleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: StrictInt = Field(ge=1)
    expected: RuleValues
    values: RulePatch


class RuleChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[RuleChange] = Field(min_length=1, max_length=1000)


def rule_values(camera):
    return {field: int(getattr(camera, field)) for field in FIELDS}


def rule_row(camera):
    return {"camera_id": camera.id, "name": camera.name, "enabled": bool(camera.enabled), **rule_values(camera)}


@router.get("/archive-rules")
def list_archive_rules(db: Session = Depends(get_db), current_user: User = Depends(require_permission("manage_cameras"))):
    cameras = db.query(Camera).filter(Camera.deleted_at.is_(None)).order_by(Camera.name.asc()).all()
    return {"cameras": [rule_row(camera) for camera in cameras], "max_retention_days": MAX_RETENTION_DAYS, "max_batch": 1000}


@router.put("/archive-rules")
def save_archive_rules(payload: RuleChanges, request: Request, db: Session = Depends(get_db),
                       current_user: User = Depends(require_permission("manage_cameras"))):
    ids = [change.camera_id for change in payload.changes]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, detail={"code": "duplicate_camera_rules"})
    cameras = db.query(Camera).filter(Camera.id.in_(ids), Camera.deleted_at.is_(None)).order_by(Camera.id).with_for_update().all()
    by_id = {camera.id: camera for camera in cameras}
    conflicts = []
    prepared = []
    for change in payload.changes:
        camera = by_id.get(change.camera_id)
        expected = change.expected.model_dump()
        values = change.values.model_dump(exclude_unset=True)
        target = {**expected, **values}
        if camera is None or rule_values(camera) not in (expected, target):
            conflicts.append(change.camera_id)
        else:
            prepared.append((camera, values))
    if conflicts:
        db.rollback()
        raise HTTPException(409, detail={"code": "archive_rules_changed", "camera_ids": conflicts})
    changed_ids = []
    retention_changed = False
    try:
        for camera, values in prepared:
            changed = {key: {"old": getattr(camera, key), "new": value} for key, value in values.items() if getattr(camera, key) != value}
            if not changed:
                continue
            for key, change in changed.items():
                setattr(camera, key, change["new"])
            if {"retention_days", "storage_quota_gb"} & changed.keys():
                camera.retention_policy_version = int(camera.retention_policy_version or 1) + 1
                retention_changed = True
            db.add(camera)
            event = create_event(db=db, actor=current_user, category="cameras", event_type="cameras.updated",
                                 message_ru=f"{current_user.username} изменил правила архива камеры {camera.name}",
                                 message_en=f"{current_user.username} updated archive rules for camera {camera.name}",
                                 target_type="camera", target_id=camera.id, target_name=camera.name,
                                 metadata={"changed": changed, "source": "storage_archive_rules"},
                                 ip_address=request_ip(request), user_agent=request_user_agent(request), commit=False)
            if event is None:
                raise RuntimeError("archive_rules_audit_failed")
            changed_ids.append(camera.id)
        if retention_changed:
            advance_retention_signal(db, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"ok": True, "updated_camera_ids": changed_ids, "cameras": [rule_row(camera) for camera, _ in prepared]}
