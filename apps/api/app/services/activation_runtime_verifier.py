from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Any, Callable
from sqlalchemy.orm import Session
from app.models.recording import ArchiveRoot
from app.services.recording_storage import archive_root_host_display_path

ACTIVE_JOB_STATES = ("starting", "recording", "stopping", "restarting")


@dataclass(frozen=True)
class ActivationRuntimeVerifier:
    """Bounded helper confirmation and effective root verification."""

    storage_confirmation_status: Callable
    _assert_worker_session: Callable
    _worker_side_effect_fence: Callable
    _touch_mutation_lock: Callable
    _root_access: Callable
    _root_by_id: Callable
    _runtime_binding_matches_root: Callable
    HELPER_WAIT_SECONDS: float
    HELPER_POLL_SECONDS: float

    def _confirmation_for_request(self, operation_id: str, request_id: str | None) -> dict[str, Any]:
        confirmation = self.storage_confirmation_status()
        if str(confirmation.get("operation_id") or "") != operation_id:
            return {"matched": False, "status": "stale_operation_result"}
        if not request_id or str(confirmation.get("runtime_request_id") or "") != str(request_id):
            return {"matched": False, "status": "stale_runtime_request_result"}
        return {"matched": True, **confirmation}

    def _wait_for_helper(self,
        operation_id: str,
        request_id: str,
        *,
        worker_session: Any | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.HELPER_WAIT_SECONDS
        last: dict[str, Any] = {"matched": False, "status": "waiting"}
        while time.monotonic() < deadline:
            self._assert_worker_session(worker_session)
            last = self._confirmation_for_request(operation_id, request_id)
            if last.get("matched"):
                status = str(last.get("apply_status") or last.get("status") or "")
                if status == "active":
                    self._assert_worker_session(worker_session)
                    return {**last, "ok": True}
                if status in {"activation_failed", "failed", "validation_failed"}:
                    apply_state = last.get("apply_state") if isinstance(last.get("apply_state"), dict) else {}
                    configuration_consistent = apply_state.get("configuration_consistent")
                    self._assert_worker_session(worker_session)
                    return {
                        **last,
                        "ok": False,
                        "reason_code": (
                            "persistent_storage_config_recovery_failed"
                            if configuration_consistent is False
                            else "runtime_activation_failed"
                        ),
                        "configuration_consistent": configuration_consistent,
                    }
            with self._worker_side_effect_fence(worker_session):
                self._touch_mutation_lock(operation_id)
            time.sleep(self.HELPER_POLL_SECONDS)
        self._assert_worker_session(worker_session)
        return {**last, "ok": False, "reason_code": "runtime_activation_timeout"}

    def _verify_effective_root(self,
        db: Session,
        *,
        root_id: str | None,
        expected_host_path: str | None,
        operation_id: str | None = None,
        request_id: str | None = None,
        worker_session: Any | None = None,
    ) -> dict[str, Any]:
        self._assert_worker_session(worker_session)
        confirmation = self.storage_confirmation_status()
        if operation_id and str(confirmation.get("operation_id") or "") != operation_id:
            return {"ok": False, "reason_code": "effective_root_operation_mismatch"}
        if request_id and str(confirmation.get("runtime_request_id") or "") != request_id:
            return {"ok": False, "reason_code": "effective_root_request_mismatch"}
        if str(confirmation.get("apply_status") or "") != "active":
            return {"ok": False, "reason_code": "effective_root_not_active"}
        if str(confirmation.get("selected_host_path") or "") != str(expected_host_path or ""):
            return {"ok": False, "reason_code": "effective_root_path_mismatch"}
        root = self._root_by_id(db, root_id)
        if not self._runtime_binding_matches_root(root):
            return {"ok": False, "reason_code": "effective_root_runtime_binding_mismatch"}
        access = self._root_access(root, require_write=True)
        self._assert_worker_session(worker_session)
        return {
            **access,
            "root_id": getattr(root, "id", None),
            "root_label": getattr(root, "label", None),
        }

    def _detect_effective_root(self, db: Session, state: dict[str, Any] | None = None) -> dict[str, Any]:
        confirmation = self.storage_confirmation_status()
        selected = str(confirmation.get("selected_host_path") or "")
        if state:
            candidates = (
                (state.get("target_root_id"), state.get("target_root_label"), state.get("target_host_path")),
                (state.get("previous_root_id"), state.get("previous_root_label"), state.get("previous_host_path")),
            )
            for root_id, root_label, host_path in candidates:
                root = self._root_by_id(db, root_id)
                if not self._runtime_binding_matches_root(root):
                    continue
                access = self._root_access(root, require_write=True)
                if access.get("ok"):
                    return {
                        **access,
                        "root_id": str(root_id),
                        "root_label": str(root_label or root_id),
                        "confirmation_path_matched": selected == str(host_path or ""),
                    }
            return {"ok": False, "reason_code": "effective_root_unknown"}
        for root in db.query(ArchiveRoot).filter(ArchiveRoot.retired_at.is_(None)).all():
            if not self._runtime_binding_matches_root(root):
                continue
            access = self._root_access(root, require_write=True)
            if access.get("ok"):
                return {
                    **access,
                    "root_id": str(root.id),
                    "root_label": str(root.label or root.id),
                    "confirmation_path_matched": archive_root_host_display_path(root) == selected,
                }
        return {"ok": False, "reason_code": "effective_root_unknown"}
