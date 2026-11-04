from __future__ import annotations
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from sqlalchemy.orm import Session
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.storage_operation import StorageOperation
from app.services.audit_log import create_event
from app.services.storage_operations_foundation import OPERATION_PROGRESS_MAX_BYTES, bounded_payload
from app.services.activation_errors import ActivationStateConflict

ACTIVE_JOB_STATES = ("starting", "recording", "stopping", "restarting")


@dataclass(frozen=True)
class CameraPauseCoordinator:
    """Transactional pause intent and exact restoration; no helper commands."""

    list_camera_recording_states: Callable
    _worker_side_effect_fence: Callable
    _assert_worker_session: Callable
    STOP_WAIT_SECONDS: float
    STOP_POLL_SECONDS: float

    def _confirmed_recording_camera_ids(self, db: Session) -> set[int]:
        return {
            int(item["camera_id"])
            for item in self.list_camera_recording_states(db)
            if bool(item.get("confirmed_recording"))
            and bool(item.get("enabled"))
            and str(item.get("recording_mode") or "").lower() == "always"
        }

    def _activation_camera_snapshot(self, db: Session, operation_id: str) -> tuple[list[dict[str, Any]], int]:
        snapshots: list[dict[str, Any]] = []
        ignored_active_looking = 0
        for item in self.list_camera_recording_states(db):
            active_looking = str(item.get("job_state") or "") in ACTIVE_JOB_STATES
            if active_looking and not bool(item.get("confirmed_recording")):
                ignored_active_looking += 1
            if not (
                bool(item.get("confirmed_recording"))
                and bool(item.get("enabled"))
                and str(item.get("recording_mode") or "").lower() == "always"
            ):
                continue
            snapshots.append(
                {
                    "operation_id": operation_id,
                    "camera_id": int(item["camera_id"]),
                    "name": str(item.get("camera_name") or item["camera_id"]),
                    "enabled_intent": True,
                    "recording_mode": str(item.get("recording_mode") or "always"),
                    "pre_operation_camera_status": item.get("camera_status"),
                    "pre_operation_recording_health": item.get("recording_health"),
                    "eligibility_reason": "confirmed_current_instance_media_progress",
                    "confirmed_evidence_at": item.get("media_progress_at"),
                    "confirmed_segment_id": item.get("current_segment_id"),
                    "confirmed_job_id": item.get("job_id"),
                    "confirmed_recorder_instance_id": item.get("recorder_instance_id"),
                    "changed_by_operation": False,
                }
            )
        return snapshots, ignored_active_looking

    def _writing_segments_count(self, db: Session, camera_ids: list[int]) -> int:
        if not camera_ids:
            return 0
        return (
            db.query(RecordingSegment)
            .filter(
                RecordingSegment.camera_id.in_(camera_ids),
                RecordingSegment.status.in_(("writing", "starting")),
                RecordingSegment.deleted_at.is_(None),
            )
            .count()
        )

    def _paused_camera_rows(self, db: Session, camera_ids: list[int]) -> list[Camera]:
        if not camera_ids:
            return []
        return (
            db.query(Camera)
            .filter(Camera.id.in_(camera_ids), Camera.deleted_at.is_(None))
            .order_by(Camera.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )

    def _restore_cameras(self,
        db: Session,
        camera_ids: list[int],
        *,
        reason: str,
        snapshot_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> dict[str, list[int]]:
        restored: list[int] = []
        failed: list[int] = []
        for camera in self._paused_camera_rows(db, camera_ids):
            snapshot = (snapshot_by_id or {}).get(int(camera.id), {})
            if not snapshot or not snapshot.get("enabled_intent", False) or not snapshot.get("changed_by_operation", False):
                continue
            try:
                camera.enabled = True
                camera.status = "enabled"
                camera.updated_at = datetime.utcnow()
                db.add(camera)
                restored.append(int(camera.id))
            except Exception:
                failed.append(int(camera.id))
        return {"restored": restored, "failed": failed}

    def _wait_for_recordings_to_stop(self,
        db: Session,
        camera_ids: list[int],
        *,
        worker_session: Any | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.STOP_WAIT_SECONDS
        last_confirmed = 0
        last_writing = 0
        while True:
            self._assert_worker_session(worker_session)
            db.expire_all()
            last_confirmed = sum(
                1
                for item in self.list_camera_recording_states(db)
                if int(item.get("camera_id") or 0) in camera_ids and bool(item.get("confirmed_recording"))
            )
            last_writing = self._writing_segments_count(db, camera_ids)
            if last_confirmed == 0 and last_writing == 0:
                self._assert_worker_session(worker_session)
                return {"ok": True, "confirmed_recordings": 0, "writing_segments": 0}
            if time.monotonic() >= deadline:
                self._assert_worker_session(worker_session)
                return {"ok": False, "confirmed_recordings": int(last_confirmed), "writing_segments": int(last_writing)}
            time.sleep(self.STOP_POLL_SECONDS)

    def _pause_operation_cameras(self,
        db: Session,
        state: dict[str, Any],
        *,
        worker_session: Any | None = None,
    ) -> tuple[list[int], list[dict[str, Any]]]:
        snapshots = [dict(item) for item in state.get("camera_snapshots") or [] if isinstance(item, dict)]
        snapshot_by_id = {int(item["camera_id"]): item for item in snapshots}
        if not snapshots:
            return [], []
        with self._worker_side_effect_fence(worker_session):
            operation = self._pause_owner(db, state)
            progress = dict(operation.progress or {})
            changed = set(progress.get("activation_paused_camera_ids") or [])
            for camera in self._paused_camera_rows(db, sorted(snapshot_by_id)):
                snapshot = snapshot_by_id[int(camera.id)]
                if bool(camera.enabled) and bool(snapshot.get("enabled_intent")):
                    camera.enabled = False
                    camera.status = "storage_switch_paused"
                    camera.updated_at = datetime.utcnow()
                    db.add(camera)
                    snapshot["changed_by_operation"] = True
                    changed.add(int(camera.id))
                if int(camera.id) in changed:
                    snapshot["changed_by_operation"] = True
            operation.progress = bounded_payload(
                {**progress, "activation_paused_camera_ids": sorted(changed)},
                max_bytes=OPERATION_PROGRESS_MAX_BYTES, field="progress",
            )
            db.add(operation)
            # Camera mutation and its durable owner proof commit together. JSON is
            # only the projection: replay recovers these ids after a lost reply.
            db.commit()
        return sorted(changed), snapshots

    def _pause_owner(self, db: Session, state: dict[str, Any], *, required: bool = True) -> StorageOperation | None:
        operation = db.query(StorageOperation).filter(
            StorageOperation.id == str(state.get("operation_id") or ""),
            StorageOperation.operation_type == "archive_root_activation",
        ).populate_existing().with_for_update().first()
        if operation is None and required:
            raise ActivationStateConflict("archive_root_activation_pause_owner_missing")
        return operation

    def preserve_manual_camera_disable(self, db: Session, camera_id: int) -> None:
        """Stage an explicit user disable in the same transaction as its camera."""
        operations = db.query(StorageOperation).filter(
            StorageOperation.operation_type == "archive_root_activation",
            StorageOperation.status.in_(("queued", "running", "cancel_requested", "partial")),
        ).populate_existing().with_for_update().all()
        for operation in operations:
            progress = dict(operation.progress or {})
            ids = list(progress.get("activation_paused_camera_ids") or [])
            if int(camera_id) in ids:
                progress["activation_paused_camera_ids"] = [item for item in ids if item != int(camera_id)]
                operation.progress = progress
                db.add(operation)

    def _restore_operation_cameras(self,
        db: Session,
        state: dict[str, Any],
        *,
        reason: str,
        worker_session: Any | None = None,
    ) -> dict[str, list[int]]:
        snapshots = [item for item in state.get("camera_snapshots") or [] if isinstance(item, dict)]
        by_id = {int(item["camera_id"]): item for item in snapshots}
        with self._worker_side_effect_fence(worker_session):
            if not snapshots and not state.get("paused_camera_ids"):
                return {"restored": [], "failed": []}
            operation = self._pause_owner(db, state, required=False)
            # A pre-existing completed JSON proof remains readable; all newly
            # performed pauses require the transactionally persisted owner above.
            progress = dict(operation.progress or {}) if operation is not None else {}
            if "activation_paused_camera_ids" in progress:
                camera_ids = [int(item) for item in progress["activation_paused_camera_ids"]]
            else:
                # Existing pre-fix operations can carry a completed JSON proof.
                camera_ids = [int(item) for item in state.get("paused_camera_ids") or []]
            for camera_id in camera_ids:
                if camera_id in by_id and "activation_paused_camera_ids" in progress:
                    by_id[camera_id] = {**by_id[camera_id], "changed_by_operation": True}
            result = self._restore_cameras(
                db,
                camera_ids,
                reason=reason,
                snapshot_by_id=by_id,
            )
            previous_restored = set(progress.get("activation_restored_camera_ids") or [])
            previous_restored.update(result["restored"])
            if operation is not None:
                operation.progress = bounded_payload({
                    **progress,
                    "activation_paused_camera_ids": [item for item in camera_ids if item not in result["restored"]],
                    "activation_restored_camera_ids": sorted(previous_restored),
                }, max_bytes=OPERATION_PROGRESS_MAX_BYTES, field="progress")
                db.add(operation)
            db.commit()
            if result["restored"]:
                create_event(
                    db=db, actor=None, category="storage",
                    event_type="archive_root.activation_recordings_restored", severity="info",
                    message_ru="Archive root activation restored paused recordings",
                    message_en="Archive root activation restored paused recordings",
                    metadata={"camera_ids": result["restored"], "reason": reason},
                )
            result["restored"] = sorted(previous_restored)
        return result
