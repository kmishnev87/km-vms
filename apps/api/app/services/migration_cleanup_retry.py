from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.archive_migration import ArchiveMigrationItem, ArchiveMigrationPlan
from app.services.storage_filesystem import assert_root_snapshot
from app.services.storage_operations_foundation import OperationHandle, database_now, heartbeat_operation


from app.services.migration_contracts import ITEM_TERMINAL_PHASES
from app.services.migration_manifest import _item_snapshot


@dataclass(frozen=True)
class MigrationCleanupRetry:
    """Continue durable cleanup without repeating completed copy."""

    _cleanup_source: Callable
    _remove_owned_target_residue: Callable
    _lock_owned_migration_item: Callable
    _root_by_id: Callable
    _operation_progress: Callable
    _recount_plan: Callable
    _terminalize_operation: Callable
    heartbeat_operation: Callable

    def _cleanup_retry_item(self,
        db: Session,
        plan: ArchiveMigrationPlan,
        item: ArchiveMigrationItem,
        handle: OperationHandle,
    ) -> None:
        source = self._root_by_id(db, item.source_root_id)
        target = self._root_by_id(db, item.target_root_id)
        assert_root_snapshot(source, _item_snapshot(item, source=True), require_write=True)
        assert_root_snapshot(target, _item_snapshot(item, source=False), require_write=True)
        if item.metadata_switched_at or item.phase in {
            "metadata_switched",
            "source_cleanup_pending",
            "source_quarantined",
            "source_delete_committing",
        }:
            if item.phase not in {"metadata_switched", "source_cleanup_pending", "source_quarantined", "source_delete_committing"}:
                _operation, _locked_plan, item = self._lock_owned_migration_item(db, plan, item, handle)
                item.phase = "source_cleanup_pending"
                db.add(item)
                db.commit()
            self._cleanup_source(db, plan, item, source, handle)
            return

        final_removed = self._remove_owned_target_residue(db, plan, item, target, handle, final=True)
        temp_removed = self._remove_owned_target_residue(db, plan, item, target, handle, final=False)
        _operation, _locked_plan, item = self._lock_owned_migration_item(db, plan, item, handle)
        item.phase = "failed"
        item.cleanup_pending = False
        item.cleanup_status = "uncommitted_target_removed" if final_removed or temp_removed else "no_residue_found"
        item.result_code = "migration_uncommitted_target_cleaned"
        item.retry_mode = "refresh"
        item.completed_at = database_now(db)
        db.add(item)
        db.commit()

    def _execute_cleanup_only_retry(self,
        db: Session,
        plan: ArchiveMigrationPlan,
        handle: OperationHandle,
    ) -> None:
        while True:
            item = (
                db.query(ArchiveMigrationItem)
                .filter(
                    ArchiveMigrationItem.plan_id == plan.id,
                    or_(
                        ArchiveMigrationItem.cleanup_pending.is_(True),
                        ArchiveMigrationItem.phase.notin_(tuple(ITEM_TERMINAL_PHASES)),
                    ),
                )
                .order_by(ArchiveMigrationItem.item_index.asc())
                .first()
            )
            if item is None:
                break
            if item.cleanup_pending or item.phase != "planned":
                self._cleanup_retry_item(db, plan, item, handle)
            else:
                _operation, _locked_plan, item = self._lock_owned_migration_item(
                    db,
                    plan,
                    item,
                    handle,
                    require_runtime_authority=False,
                )
                item.phase = "blocked"
                item.result_code = "migration_not_processed_after_partial"
                item.retry_mode = "refresh"
                item.completed_at = database_now(db)
                db.add(item)
                db.commit()
            self._recount_plan(db, plan, handle=handle)
            self.heartbeat_operation(db, handle, progress=self._operation_progress(plan, phase="cleanup_retry"))
        self._recount_plan(db, plan, handle=handle)
        if int(plan.completed_count or 0) == int(plan.item_count or 0) and not plan.cleanup_pending:
            self._terminalize_operation(db, plan, handle, status="completed")
        else:
            self._terminalize_operation(
                db,
                plan,
                handle,
                status="partial" if int(plan.completed_count or 0) else "failed",
                reason_code="migration_manifest_incomplete",
                retry_mode="refresh",
            )
