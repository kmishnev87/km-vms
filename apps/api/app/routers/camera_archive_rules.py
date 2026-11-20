"""Atomic archive rules and explicit, camera-scoped transfer consent."""
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy.orm import Session

from app.core.camera_limits import MAX_RETENTION_DAYS
from app.db.session import get_db
from app.models.camera import Camera
from app.models.recording import ArchiveRoot
from app.models.user import User
from app.routers.deps import require_permission, require_permissions
from app.services.archive_migration import ArchiveMigrationBlocked, request_migration_plan
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.camera_archive_placement import RootId, archive_move_offer, current_default_id, move_source_counts, target_catalog, validate_target
from app.services.retention_automation import advance_retention_signal

router = APIRouter()
FIELDS = ("segment_minutes", "retention_days", "storage_quota_gb")
ROOT_FIELD = "recording_archive_root_id"


class RuleValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_minutes: StrictInt = Field(ge=1, le=2147483647)
    retention_days: StrictInt = Field(ge=1, le=MAX_RETENTION_DAYS)
    storage_quota_gb: StrictInt = Field(ge=1, le=2147483647)
    recording_archive_root_id: RootId | None = None


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_minutes: StrictInt | None = Field(default=None, ge=1, le=2147483647)
    retention_days: StrictInt | None = Field(default=None, ge=1, le=MAX_RETENTION_DAYS)
    storage_quota_gb: StrictInt | None = Field(default=None, ge=1, le=2147483647)
    recording_archive_root_id: RootId | None = None

    @model_validator(mode="after")
    def nonempty_patch(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set if key != ROOT_FIELD):
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


class MoveChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: StrictInt = Field(ge=1)
    target_root_id: RootId


class ConfirmCameraMoves(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    cameras: list[MoveChoice] = Field(min_length=1, max_length=1000)


def rule_values(camera):
    return {field: int(getattr(camera, field)) for field in FIELDS}


def rule_row(camera):
    return {"camera_id": camera.id, "name": camera.name, "enabled": bool(camera.enabled),
            ROOT_FIELD: getattr(camera, ROOT_FIELD, None), **rule_values(camera)}


@router.get("/archive-targets")
def archive_targets(db: Session = Depends(get_db), current_user: User = Depends(require_permission("manage_cameras"))):
    return target_catalog(db, current_user)


@router.post("/archive-moves")
def confirm_camera_moves(payload: ConfirmCameraMoves, db: Session = Depends(get_db),
                         current_user: User = Depends(require_permissions("manage_cameras", "manage_settings", "delete_recordings"))):
    ids = [row.camera_id for row in payload.cameras]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, detail={"code": "duplicate_camera_rules"})
    cameras = {row.id: row for row in db.query(Camera).filter(Camera.id.in_(ids), Camera.deleted_at.is_(None)).all()}
    default_id = current_default_id(db)
    if any(row.camera_id not in cameras or
           (cameras[row.camera_id].recording_archive_root_id or default_id) != row.target_root_id for row in payload.cameras):
        raise HTTPException(409, detail={"code": "camera_archive_target_changed"})
    accepted, failed = [], []
    for row in payload.cameras:
        for source_id, _count in move_source_counts(db, row.camera_id, row.target_root_id):
            request_key = uuid5(payload.request_id, f"{row.camera_id}:{source_id}:{row.target_root_id}").hex
            try:
                plan = request_migration_plan(db, actor=current_user, source_root_id=source_id,
                    target_root_id=row.target_root_id, idempotency_key=request_key, selected_camera_id=row.camera_id)
            except ArchiveMigrationBlocked as exc:
                db.rollback()
                failed.append({"camera_id": row.camera_id, "source_root_id": source_id, "reason": exc.reason_code})
            else:
                accepted.append(plan)
    return {"plans": accepted, "skipped": failed}


@router.get("/archive-rules")
def list_archive_rules(db: Session = Depends(get_db), current_user: User = Depends(require_permission("manage_cameras"))):
    cameras = db.query(Camera).filter(Camera.deleted_at.is_(None)).order_by(Camera.name.asc()).all()
    return {"cameras": [rule_row(camera) for camera in cameras], "archive_targets": target_catalog(db, current_user),
            "max_retention_days": MAX_RETENTION_DAYS, "max_batch": 1000}


@router.put("/archive-rules")
def save_archive_rules(payload: RuleChanges, request: Request, db: Session = Depends(get_db),
                       current_user: User = Depends(require_permission("manage_cameras"))):
    ids = [change.camera_id for change in payload.changes]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, detail={"code": "duplicate_camera_rules"})
    cameras = db.query(Camera).filter(Camera.id.in_(ids), Camera.deleted_at.is_(None)).order_by(Camera.id).with_for_update().all()
    by_id = {camera.id: camera for camera in cameras}
    changed_destinations = [change.values.recording_archive_root_id or current_default_id(db)
                            for change in payload.changes if ROOT_FIELD in change.values.model_fields_set]
    if changed_destinations:
        # Two editors may swap different groups of cameras between the same two roots.
        db.query(ArchiveRoot.id).filter(ArchiveRoot.id.in_([root for root in changed_destinations if root])) \
            .order_by(ArchiveRoot.id).with_for_update().all()
    conflicts = []
    prepared = []
    for change in payload.changes:
        camera = by_id.get(change.camera_id)
        expected = change.expected.model_dump(exclude_unset=True)
        values = change.values.model_dump(exclude_unset=True)
        target = {**expected, **values}
        observed = ({**rule_values(camera), **({ROOT_FIELD: getattr(camera, ROOT_FIELD, None)}
                     if ROOT_FIELD in expected else {})} if camera is not None else None)
        if camera is None or ROOT_FIELD in values and ROOT_FIELD not in expected or observed not in (expected, target):
            conflicts.append(change.camera_id)
        else:
            current = getattr(camera, ROOT_FIELD, None)
            effective = validate_target(db, values[ROOT_FIELD], existing=current) if ROOT_FIELD in values else None
            previous = (current or current_default_id(db)) if effective else None
            prepared.append((camera, values, effective if previous != effective else None))
    if conflicts:
        db.rollback()
        raise HTTPException(409, detail={"code": "archive_rules_changed", "camera_ids": conflicts})
    changed_ids = []
    offers = []
    retention_changed = False
    try:
        for camera, values, move_target in prepared:
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
            if move_target:
                offer = archive_move_offer(db, camera, move_target, current_user)
                if offer:
                    offers.append(offer)
        if retention_changed:
            advance_retention_signal(db, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"ok": True, "updated_camera_ids": changed_ids, "archive_move_offers": offers,
            "cameras": [rule_row(camera) for camera, _, _ in prepared]}
