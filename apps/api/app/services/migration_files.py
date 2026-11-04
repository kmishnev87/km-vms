from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.models.archive_migration import ArchiveMigrationItem, ArchiveMigrationPlan
from app.models.recording import ArchiveRoot, RecordingSegment
from app.services.storage_filesystem import StorageFilesystemError, create_relative_exclusive, open_relative_read, relative_parent_fd, remove_empty_internal_parents, rename_relative, stat_relative, unlink_relative, verified_root_fd
from app.services.storage_operations_foundation import OperationHandle, database_now, heartbeat_operation


from app.services.migration_contracts import COPY_CHUNK_BYTES, TEMP_CREATE_MODE, ArchiveMigrationBlocked, ArchiveMigrationPartial, _CopyThroughputSampler
from app.services.migration_manifest import _segment_metadata_fingerprint, _item_snapshot


@dataclass(frozen=True)
class MigrationFileExecutor:
    """Copy and clean only under the injected operation fence."""

    _lock_owned_migration_item: Callable
    _operation_progress: Callable
    _root_by_id: Callable
    _require_runtime_authority: Callable
    _hash_descriptor: Callable
    heartbeat_operation: Callable

    def _stat_matches_source(self, item: ArchiveMigrationItem, current: os.stat_result) -> bool:
        return bool(
            int(current.st_dev) == int(item.source_device)
            and int(current.st_ino) == int(item.source_inode)
            and int(current.st_size) == int(item.source_size_bytes)
            and int(current.st_mtime_ns) == int(item.source_mtime_ns)
        )

    def _stat_matches_target_provenance(self, item: ArchiveMigrationItem, current: os.stat_result) -> bool:
        return bool(
            item.target_device is not None
            and item.target_inode is not None
            and int(current.st_dev) == int(item.target_device)
            and int(current.st_ino) == int(item.target_inode)
            and int(current.st_size) == int(item.source_size_bytes)
            and str(item.target_sha256 or "") == str(item.source_sha256 or "")
        )

    def _pending_temp_stat_is_exact(self, current: os.stat_result) -> bool:
        effective_uid = os.geteuid() if hasattr(os, "geteuid") else int(current.st_uid)
        return bool(
            stat.S_ISREG(current.st_mode)
            and int(current.st_size) == 0
            and int(current.st_nlink) == 1
            and int(current.st_uid) == int(effective_uid)
            and stat.S_IMODE(current.st_mode) == TEMP_CREATE_MODE
        )

    def _raise_pending_temp_ambiguous(self, ) -> None:
        raise ArchiveMigrationPartial(
            "migration_temp_pending_object_ambiguous",
            retry_mode="cleanup_only",
        )

    def _stat_pending_temp(self, root_fd: int, item: ArchiveMigrationItem) -> os.stat_result:
        try:
            current = stat_relative(root_fd, item.target_temp_relative_path, allow_internal=True)
        except StorageFilesystemError:
            self._raise_pending_temp_ambiguous()
        if not self._pending_temp_stat_is_exact(current):
            self._raise_pending_temp_ambiguous()
        return current

    def _open_pending_temp_for_recovery(self, root_fd: int, item: ArchiveMigrationItem) -> int:
        expected = self._stat_pending_temp(root_fd, item)
        with relative_parent_fd(
            root_fd,
            item.target_temp_relative_path,
            create=False,
            allow_internal=True,
        ) as (parent_fd, name):
            descriptor = os.open(name, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if (
                int(opened.st_dev) != int(expected.st_dev)
                or int(opened.st_ino) != int(expected.st_ino)
                or not self._pending_temp_stat_is_exact(opened)
            ):
                self._raise_pending_temp_ambiguous()
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _stage_temp_create_intent(self,
        db: Session,
        plan: ArchiveMigrationPlan,
        item: ArchiveMigrationItem,
        handle: OperationHandle,
    ) -> None:
        _operation, _locked_plan, item = self._lock_owned_migration_item(db, plan, item, handle)
        if item.phase != "planned":
            raise ArchiveMigrationPartial("migration_temp_phase_invalid", retry_mode="cleanup_only")
        item.phase = "target_temp_create_pending"
        item.operation_id = str(handle.operation_id)
        item.operation_fencing_token = int(handle.fencing_token)
        item.attempt_count = int(item.attempt_count or 0) + 1
        item.target_device = None
        item.target_inode = None
        item.target_size_bytes = 0
        item.transferred_bytes = 0
        db.add(item)
        db.commit()

    def _verify_durable_final_target(self, item: ArchiveMigrationItem, target: ArchiveRoot) -> None:
        if not item.target_sha256:
            raise ArchiveMigrationPartial("migration_final_provenance_unknown", retry_mode="cleanup_only")
        with verified_root_fd(
            target,
            _item_snapshot(item, source=False),
            require_write=True,
        ) as target_root_fd:
            try:
                current = stat_relative(target_root_fd, item.target_final_relative_path)
            except FileNotFoundError as exc:
                raise ArchiveMigrationPartial("migration_final_target_missing", retry_mode="cleanup_only") from exc
            if not self._stat_matches_target_provenance(item, current):
                raise ArchiveMigrationPartial("migration_final_provenance_mismatch", retry_mode="cleanup_only")
            descriptor = open_relative_read(target_root_fd, item.target_final_relative_path)
            try:
                if self._hash_descriptor(descriptor) != str(item.target_sha256):
                    raise ArchiveMigrationPartial("migration_final_checksum_mismatch", retry_mode="cleanup_only")
                if int(current.st_mode) & 0o444 == 0:
                    raise ArchiveMigrationPartial("migration_final_not_readable", retry_mode="cleanup_only")
            finally:
                os.close(descriptor)

    def _copy_or_resume_temp(self,
        db: Session,
        plan: ArchiveMigrationPlan,
        item: ArchiveMigrationItem,
        source: ArchiveRoot,
        target: ArchiveRoot,
        handle: OperationHandle,
    ) -> None:
        source_expected = _item_snapshot(item, source=True)
        target_expected = _item_snapshot(item, source=False)
        with verified_root_fd(source, source_expected, require_write=True) as source_root_fd:
            source_fd = open_relative_read(source_root_fd, item.source_relative_path)
            try:
                source_stat = os.fstat(source_fd)
                if not self._stat_matches_source(item, source_stat):
                    raise ArchiveMigrationBlocked("migration_source_changed", retry_mode="refresh_plan")
                if self._hash_descriptor(
                    source_fd,
                    progress_callback=lambda: heartbeat_operation(
                        db,
                        handle,
                        progress=self._operation_progress(plan, phase="verifying_source", current_item=item),
                    ),
                ) != str(item.source_sha256 or ""):
                    raise ArchiveMigrationBlocked("migration_source_checksum_changed", retry_mode="refresh_plan")
                with verified_root_fd(target, target_expected, require_write=True) as target_root_fd:
                    fresh_intent = False
                    if item.phase == "planned":
                        try:
                            stat_relative(target_root_fd, item.target_temp_relative_path, allow_internal=True)
                        except FileNotFoundError:
                            pass
                        except StorageFilesystemError:
                            self._raise_pending_temp_ambiguous()
                        else:
                            self._raise_pending_temp_ambiguous()
                        self._stage_temp_create_intent(db, plan, item, handle)
                        fresh_intent = True

                    temp_fd: int | None = None
                    try:
                        if item.phase == "target_temp_create_pending":
                            _operation, _locked_plan, item = self._lock_owned_migration_item(
                                db,
                                plan,
                                item,
                                handle,
                            )
                            if item.phase != "target_temp_create_pending":
                                raise ArchiveMigrationPartial(
                                    "migration_temp_phase_invalid",
                                    retry_mode="cleanup_only",
                                )
                            if fresh_intent:
                                try:
                                    temp_fd = create_relative_exclusive(
                                        target_root_fd,
                                        item.target_temp_relative_path,
                                        mode=TEMP_CREATE_MODE,
                                    )
                                except FileExistsError:
                                    self._raise_pending_temp_ambiguous()
                            else:
                                try:
                                    temp_fd = self._open_pending_temp_for_recovery(target_root_fd, item)
                                except FileNotFoundError:
                                    try:
                                        temp_fd = create_relative_exclusive(
                                            target_root_fd,
                                            item.target_temp_relative_path,
                                            mode=TEMP_CREATE_MODE,
                                        )
                                    except FileExistsError:
                                        self._raise_pending_temp_ambiguous()
                            temp_stat = os.fstat(temp_fd)
                            if not self._pending_temp_stat_is_exact(temp_stat):
                                self._raise_pending_temp_ambiguous()
                            item.target_device = int(temp_stat.st_dev)
                            item.target_inode = int(temp_stat.st_ino)
                            item.phase = "copying"
                            db.add(item)
                            try:
                                db.commit()
                            except Exception:
                                db.rollback()
                                raise
                        elif item.phase == "copying":
                            if item.target_device is None or item.target_inode is None:
                                raise ArchiveMigrationPartial(
                                    "migration_temp_provenance_unknown",
                                    retry_mode="cleanup_only",
                                )
                            try:
                                temp_stat = stat_relative(
                                    target_root_fd,
                                    item.target_temp_relative_path,
                                    allow_internal=True,
                                )
                            except FileNotFoundError as exc:
                                raise ArchiveMigrationPartial(
                                    "migration_temp_missing",
                                    retry_mode="cleanup_only",
                                ) from exc
                            if not (
                                int(temp_stat.st_dev) == int(item.target_device)
                                and int(temp_stat.st_ino) == int(item.target_inode)
                                and int(temp_stat.st_nlink) == 1
                                and int(temp_stat.st_size) <= int(item.source_size_bytes)
                            ):
                                raise ArchiveMigrationPartial("migration_temp_collision", retry_mode="cleanup_only")
                            with relative_parent_fd(
                                target_root_fd,
                                item.target_temp_relative_path,
                                create=False,
                                allow_internal=True,
                            ) as (parent_fd, name):
                                temp_fd = os.open(
                                    name,
                                    os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                                    dir_fd=parent_fd,
                                )
                        else:
                            raise ArchiveMigrationPartial(
                                "migration_temp_phase_invalid",
                                retry_mode="cleanup_only",
                            )

                        temp_stat = os.fstat(temp_fd)
                        if not (
                            int(temp_stat.st_dev) == int(item.target_device or -1)
                            and int(temp_stat.st_ino) == int(item.target_inode or -1)
                            and int(temp_stat.st_nlink) == 1
                            and int(temp_stat.st_size) <= int(item.source_size_bytes)
                        ):
                            raise ArchiveMigrationPartial("migration_temp_provenance_mismatch", retry_mode="cleanup_only")
                        offset = int(temp_stat.st_size)
                        if int(item.transferred_bytes or 0) != offset:
                            _operation, _locked_plan, item = self._lock_owned_migration_item(
                                db,
                                plan,
                                item,
                                handle,
                            )
                            if item.phase != "copying":
                                raise ArchiveMigrationPartial(
                                    "migration_temp_phase_invalid",
                                    retry_mode="cleanup_only",
                                )
                            item.transferred_bytes = offset
                            db.add(item)
                            db.commit()
                        os.lseek(source_fd, offset, os.SEEK_SET)
                        os.lseek(temp_fd, offset, os.SEEK_SET)
                        sampler = _CopyThroughputSampler()
                        sampler.observe(offset)
                        self.heartbeat_operation(
                            db,
                            handle,
                            progress=self._operation_progress(
                                plan,
                                phase="copying",
                                current_item=item,
                            ),
                        )
                        while offset < int(item.source_size_bytes):
                            _operation, _locked_plan, item = self._lock_owned_migration_item(
                                db,
                                plan,
                                item,
                                handle,
                            )
                            if item.phase != "copying":
                                raise ArchiveMigrationPartial(
                                    "migration_temp_phase_invalid",
                                    retry_mode="cleanup_only",
                                )
                            batch_started = time.monotonic()
                            batch_bytes = 0
                            while (
                                offset < int(item.source_size_bytes)
                                and batch_bytes < 16 * COPY_CHUNK_BYTES
                                and time.monotonic() - batch_started < 2.0
                            ):
                                chunk = os.read(
                                    source_fd,
                                    min(COPY_CHUNK_BYTES, int(item.source_size_bytes) - offset),
                                )
                                if not chunk:
                                    raise ArchiveMigrationPartial(
                                        "migration_source_short_read",
                                        retry_mode="cleanup_only",
                                    )
                                view = memoryview(chunk)
                                while view:
                                    written = os.write(temp_fd, view)
                                    if written <= 0:
                                        raise ArchiveMigrationPartial(
                                            "migration_target_short_write",
                                            retry_mode="cleanup_only",
                                        )
                                    view = view[written:]
                                offset += len(chunk)
                                batch_bytes += len(chunk)
                                sampler.observe(offset)
                            item.transferred_bytes = offset
                            db.add(item)
                            speed, eta = sampler.values(
                                remaining_bytes=max(
                                    0,
                                    int(plan.total_bytes or 0)
                                    - int(plan.completed_bytes or 0)
                                    - offset,
                                )
                            )
                            self.heartbeat_operation(
                                db,
                                handle,
                                progress=self._operation_progress(
                                    plan,
                                    phase="copying",
                                    current_item=item,
                                    speed_bytes_per_second=speed,
                                    eta_seconds=eta,
                                ),
                            )
                        _operation, _locked_plan, item = self._lock_owned_migration_item(
                            db,
                            plan,
                            item,
                            handle,
                        )
                        if item.phase != "copying":
                            raise ArchiveMigrationPartial(
                                "migration_temp_phase_invalid",
                                retry_mode="cleanup_only",
                            )
                        os.fsync(temp_fd)
                        try:
                            os.fchmod(temp_fd, 0o640)
                            if item.source_uid is not None and item.source_gid is not None and hasattr(os, "fchown"):
                                os.fchown(temp_fd, int(item.source_uid), int(item.source_gid))
                        except OSError:
                            # Runtime user may not be allowed to preserve ownership; readable mode remains mandatory.
                            os.fchmod(temp_fd, 0o640)
                        final_temp_stat = os.fstat(temp_fd)
                        item.transferred_bytes = int(final_temp_stat.st_size)
                        item.target_size_bytes = int(final_temp_stat.st_size)
                        item.phase = "target_temp_written"
                        db.add(item)
                        db.commit()
                    finally:
                        if temp_fd is not None:
                            os.close(temp_fd)
            finally:
                os.close(source_fd)

    def _verify_and_finalize_target(self,
        db: Session,
        plan: ArchiveMigrationPlan,
        item: ArchiveMigrationItem,
        source: ArchiveRoot,
        target: ArchiveRoot,
        handle: OperationHandle,
    ) -> None:
        source_expected = _item_snapshot(item, source=True)
        target_expected = _item_snapshot(item, source=False)
        with verified_root_fd(source, source_expected, require_write=True) as source_root_fd:
            source_stat = stat_relative(source_root_fd, item.source_relative_path)
            if not self._stat_matches_source(item, source_stat):
                raise ArchiveMigrationPartial("migration_source_changed_after_copy", retry_mode="cleanup_only")
        with verified_root_fd(target, target_expected, require_write=True) as target_root_fd:
            if item.phase == "target_temp_written":
                temp_fd = open_relative_read(target_root_fd, item.target_temp_relative_path, allow_internal=True)
                try:
                    temp_stat = os.fstat(temp_fd)
                    if not (
                        int(temp_stat.st_dev) == int(item.target_device or -1)
                        and int(temp_stat.st_ino) == int(item.target_inode or -1)
                        and int(temp_stat.st_size) == int(item.source_size_bytes)
                    ):
                        raise ArchiveMigrationPartial("migration_temp_provenance_mismatch", retry_mode="cleanup_only")
                    checksum = self._hash_descriptor(
                        temp_fd,
                        progress_callback=lambda: heartbeat_operation(
                            db,
                            handle,
                            progress=self._operation_progress(plan, phase="verifying_target", current_item=item),
                        ),
                    )
                finally:
                    os.close(temp_fd)
                if checksum != str(item.source_sha256 or ""):
                    raise ArchiveMigrationPartial("migration_target_checksum_mismatch", retry_mode="cleanup_only")
                _operation, _locked_plan, item = self._lock_owned_migration_item(
                    db,
                    plan,
                    item,
                    handle,
                )
                if item.phase != "target_temp_written":
                    raise ArchiveMigrationPartial("migration_temp_phase_invalid", retry_mode="cleanup_only")
                item.target_sha256 = checksum
                item.phase = "target_verified"
                db.add(item)
                db.commit()

            if item.phase == "target_verified":
                _operation, _locked_plan, item = self._lock_owned_migration_item(
                    db,
                    plan,
                    item,
                    handle,
                )
                if item.phase != "target_verified":
                    raise ArchiveMigrationPartial("migration_temp_phase_invalid", retry_mode="cleanup_only")
                try:
                    final_stat = stat_relative(target_root_fd, item.target_final_relative_path)
                except FileNotFoundError:
                    rename_relative(
                        target_root_fd,
                        item.target_temp_relative_path,
                        target_root_fd,
                        item.target_final_relative_path,
                        source_internal=True,
                        target_internal=False,
                    )
                    final_stat = stat_relative(target_root_fd, item.target_final_relative_path)
                else:
                    # A crash after atomic finalize is recoverable only when the
                    # inode is the exact object previously created for this item.
                    if not self._stat_matches_target_provenance(item, final_stat):
                        raise ArchiveMigrationPartial("migration_target_collision", retry_mode="cleanup_only")
                if not (
                    int(final_stat.st_dev) == int(item.target_device or -1)
                    and int(final_stat.st_ino) == int(item.target_inode or -1)
                    and int(final_stat.st_size) == int(item.source_size_bytes)
                ):
                    raise ArchiveMigrationPartial("migration_final_provenance_mismatch", retry_mode="cleanup_only")
                if int(final_stat.st_mode) & 0o444 == 0:
                    raise ArchiveMigrationPartial("migration_final_not_readable", retry_mode="cleanup_only")
                item.phase = "target_finalized"
                item.target_finalized_at = database_now(db)
                db.add(item)
                db.commit()
                self._verify_durable_final_target(item, target)

    def _switch_metadata(self,
        db: Session,
        plan: ArchiveMigrationPlan,
        item: ArchiveMigrationItem,
        handle: OperationHandle,
    ) -> None:
        if item.phase != "target_finalized":
            return
        self._verify_durable_final_target(item, self._root_by_id(db, item.target_root_id))
        _operation, _locked_plan, item = self._lock_owned_migration_item(db, plan, item, handle)
        if item.phase != "target_finalized":
            raise ArchiveMigrationPartial("migration_metadata_phase_invalid", retry_mode="cleanup_only")
        segment = db.query(RecordingSegment).filter(RecordingSegment.id == item.segment_id).with_for_update().one()
        if (
            str(segment.archive_root_id or "") != str(item.source_root_id)
            or str(segment.relative_path or "") != str(item.source_relative_path)
            or _segment_metadata_fingerprint(segment) != str(item.source_metadata_fingerprint)
        ):
            raise ArchiveMigrationPartial("migration_metadata_changed_before_switch", retry_mode="cleanup_only")
        segment.archive_root_id = str(item.target_root_id)
        segment.relative_path = str(item.target_final_relative_path)
        segment.file_path = str(item.target_final_relative_path)
        segment.size_bytes = int(item.source_size_bytes)
        segment.updated_at = database_now(db)
        item.phase = "metadata_switched"
        item.metadata_switched_at = database_now(db)
        item.cleanup_pending = True
        item.cleanup_status = "pending"
        db.add(segment)
        db.add(item)
        db.commit()

    def _cleanup_source(self,
        db: Session,
        plan: ArchiveMigrationPlan,
        item: ArchiveMigrationItem,
        source: ArchiveRoot,
        handle: OperationHandle,
    ) -> None:
        self._require_runtime_authority(db, plan)
        self._verify_durable_final_target(item, self._root_by_id(db, item.target_root_id))
        if item.phase == "metadata_switched":
            _operation, _locked_plan, item = self._lock_owned_migration_item(db, plan, item, handle)
            if item.phase != "metadata_switched":
                raise ArchiveMigrationPartial("migration_cleanup_phase_invalid", retry_mode="cleanup_only")
            item.phase = "source_cleanup_pending"
            item.cleanup_pending = True
            item.cleanup_status = "quarantine_pending"
            db.add(item)
            db.commit()
        source_expected = _item_snapshot(item, source=True)
        with verified_root_fd(source, source_expected, require_write=True) as source_root_fd:
            if item.phase == "source_cleanup_pending":
                _operation, _locked_plan, item = self._lock_owned_migration_item(db, plan, item, handle)
                if item.phase != "source_cleanup_pending":
                    raise ArchiveMigrationPartial("migration_cleanup_phase_invalid", retry_mode="cleanup_only")
                try:
                    source_stat = stat_relative(source_root_fd, item.source_relative_path)
                except FileNotFoundError:
                    try:
                        quarantine_stat = stat_relative(
                            source_root_fd,
                            item.source_quarantine_relative_path,
                            allow_internal=True,
                        )
                    except FileNotFoundError as exc:
                        raise ArchiveMigrationPartial("migration_source_cleanup_truth_unknown", retry_mode="cleanup_only") from exc
                    if not self._stat_matches_source(item, quarantine_stat):
                        raise ArchiveMigrationPartial("migration_quarantine_provenance_mismatch", retry_mode="cleanup_only")
                else:
                    if not self._stat_matches_source(item, source_stat):
                        raise ArchiveMigrationPartial("migration_source_provenance_mismatch", retry_mode="cleanup_only")
                    rename_relative(
                        source_root_fd,
                        item.source_relative_path,
                        source_root_fd,
                        item.source_quarantine_relative_path,
                        source_internal=False,
                        target_internal=True,
                    )
                    quarantine_stat = stat_relative(
                        source_root_fd,
                        item.source_quarantine_relative_path,
                        allow_internal=True,
                    )
                item.quarantine_device = int(quarantine_stat.st_dev)
                item.quarantine_inode = int(quarantine_stat.st_ino)
                item.phase = "source_quarantined"
                item.source_quarantined_at = database_now(db)
                item.cleanup_status = "quarantined"
                db.add(item)
                db.commit()

            if item.phase == "source_quarantined":
                _operation, _locked_plan, item = self._lock_owned_migration_item(db, plan, item, handle)
                if item.phase != "source_quarantined":
                    raise ArchiveMigrationPartial("migration_cleanup_phase_invalid", retry_mode="cleanup_only")
                quarantine_stat = stat_relative(
                    source_root_fd,
                    item.source_quarantine_relative_path,
                    allow_internal=True,
                )
                if not (
                    int(quarantine_stat.st_dev) == int(item.quarantine_device or -1)
                    and int(quarantine_stat.st_ino) == int(item.quarantine_inode or -1)
                    and self._stat_matches_source(item, quarantine_stat)
                ):
                    raise ArchiveMigrationPartial("migration_quarantine_provenance_mismatch", retry_mode="cleanup_only")
                item.phase = "source_delete_committing"
                item.cleanup_status = "delete_committing"
                db.add(item)
                db.commit()

            if item.phase == "source_delete_committing":
                _operation, _locked_plan, item = self._lock_owned_migration_item(db, plan, item, handle)
                if item.phase != "source_delete_committing":
                    raise ArchiveMigrationPartial("migration_cleanup_phase_invalid", retry_mode="cleanup_only")
                source_exists = True
                quarantine_exists = True
                try:
                    stat_relative(source_root_fd, item.source_relative_path)
                except FileNotFoundError:
                    source_exists = False
                try:
                    stat_relative(source_root_fd, item.source_quarantine_relative_path, allow_internal=True)
                except FileNotFoundError:
                    quarantine_exists = False
                if quarantine_exists:
                    quarantine_stat = stat_relative(
                        source_root_fd,
                        item.source_quarantine_relative_path,
                        allow_internal=True,
                    )
                    if not (
                        int(quarantine_stat.st_dev) == int(item.quarantine_device or -1)
                        and int(quarantine_stat.st_ino) == int(item.quarantine_inode or -1)
                        and self._stat_matches_source(item, quarantine_stat)
                    ):
                        raise ArchiveMigrationPartial(
                            "migration_quarantine_provenance_mismatch",
                            retry_mode="cleanup_only",
                        )
                    unlink_relative(source_root_fd, item.source_quarantine_relative_path, allow_internal=True)
                    remove_empty_internal_parents(source_root_fd, item.source_quarantine_relative_path)
                    quarantine_exists = False
                if source_exists or quarantine_exists:
                    raise ArchiveMigrationPartial("migration_source_cleanup_incomplete", retry_mode="cleanup_only")
                item.phase = "completed"
                item.cleanup_pending = False
                item.cleanup_status = "completed"
                item.result_code = "migrated"
                item.completed_at = database_now(db)
                db.add(item)
                db.commit()

    def _remove_owned_target_residue(self,
        db: Session,
        plan: ArchiveMigrationPlan,
        item: ArchiveMigrationItem,
        target: ArchiveRoot,
        handle: OperationHandle,
        *,
        final: bool,
    ) -> bool:
        relative = item.target_final_relative_path if final else item.target_temp_relative_path
        with verified_root_fd(target, _item_snapshot(item, source=False), require_write=True) as target_root_fd:
            try:
                current = stat_relative(target_root_fd, relative, allow_internal=not final)
            except FileNotFoundError:
                return False
            except StorageFilesystemError:
                if not final and item.phase == "target_temp_create_pending":
                    self._raise_pending_temp_ambiguous()
                raise
            if item.target_device is None or item.target_inode is None:
                if not final and item.phase == "target_temp_create_pending":
                    if not self._pending_temp_stat_is_exact(current):
                        self._raise_pending_temp_ambiguous()
                    descriptor = self._open_pending_temp_for_recovery(target_root_fd, item)
                    try:
                        reopened = os.fstat(descriptor)
                        if (
                            int(reopened.st_dev) != int(current.st_dev)
                            or int(reopened.st_ino) != int(current.st_ino)
                        ):
                            self._raise_pending_temp_ambiguous()
                        _operation, _locked_plan, item = self._lock_owned_migration_item(
                            db,
                            plan,
                            item,
                            handle,
                        )
                        current = stat_relative(target_root_fd, relative, allow_internal=True)
                        if (
                            int(reopened.st_dev) != int(current.st_dev)
                            or int(reopened.st_ino) != int(current.st_ino)
                            or not self._pending_temp_stat_is_exact(current)
                        ):
                            self._raise_pending_temp_ambiguous()
                        unlink_relative(target_root_fd, relative, allow_internal=True)
                        remove_empty_internal_parents(target_root_fd, relative)
                        return True
                    finally:
                        os.close(descriptor)
                raise ArchiveMigrationPartial("migration_target_residue_provenance_unknown", retry_mode="cleanup_only")
            if int(current.st_dev) != int(item.target_device) or int(current.st_ino) != int(item.target_inode):
                raise ArchiveMigrationPartial("migration_target_residue_provenance_mismatch", retry_mode="cleanup_only")
            if final:
                if int(current.st_size) != int(item.source_size_bytes) or not item.target_sha256:
                    raise ArchiveMigrationPartial("migration_final_provenance_mismatch", retry_mode="cleanup_only")
                descriptor = open_relative_read(target_root_fd, relative)
                try:
                    checksum = self._hash_descriptor(descriptor)
                finally:
                    os.close(descriptor)
                if checksum != str(item.target_sha256):
                    raise ArchiveMigrationPartial("migration_final_checksum_mismatch", retry_mode="cleanup_only")
            elif int(current.st_size) > int(item.source_size_bytes):
                raise ArchiveMigrationPartial("migration_temp_provenance_mismatch", retry_mode="cleanup_only")
            _operation, _locked_plan, item = self._lock_owned_migration_item(db, plan, item, handle)
            current = stat_relative(target_root_fd, relative, allow_internal=not final)
            if int(current.st_dev) != int(item.target_device) or int(current.st_ino) != int(item.target_inode):
                raise ArchiveMigrationPartial("migration_target_residue_provenance_mismatch", retry_mode="cleanup_only")
            unlink_relative(target_root_fd, relative, allow_internal=not final)
            if not final:
                remove_empty_internal_parents(target_root_fd, relative)
        return True
