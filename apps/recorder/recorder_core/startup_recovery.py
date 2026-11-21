"""One background sweep of interrupted owned writers; no archive-wide file scan."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from .archive_destination import ArchiveMountUnavailable
from .contracts import METADATA_SOURCE_RECORDER, OWNERSHIP_KM_VMS, RECORDER_RECEIPT_CONTRACT_VERSION
from .segment_accounting import bounded_file_fingerprint, receipt_object_identity
from .segment_repair import JOURNAL_DIRECTORY, file_identity, read_publication, reconcile_publication


class StartupSegmentRecovery:
    def __init__(self, engine, instance_id, accounting, repairs, log_event, *, root_mapper=None):
        self.engine, self.instance_id, self.accounting = engine, instance_id, accounting
        self.repairs, self.log_event = repairs, log_event
        self.root_mapper = root_mapper
        self._stop = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.run, name="startup-segment-recovery", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()

    def _rows(self, after):
        with self.engine.connect() as connection:
            return connection.execute(text("""
                SELECT s.id, s.camera_id, s.camera_name_snapshot, s.job_id, s.relative_path,
                       s.file_path, s.archive_root_id, a.root_path, a.is_active, r.inode, r.device_id, r.object_identity
                FROM recording_segments s
                JOIN recorder_file_receipts r ON r.segment_id=s.id
                JOIN archive_roots a ON a.id=s.archive_root_id
                JOIN recording_jobs j ON j.id=s.job_id
                WHERE s.id > :after AND s.status IN ('writing','stale_writing')
                  AND s.deleted_at IS NULL AND s.ownership=:ownership AND s.source=:source
                  AND j.recorder_instance_id IS DISTINCT FROM :instance
                  AND j.state IN ('stopped','error','disabled')
                  AND r.state='writing' AND r.contract_version=:contract
                  AND r.root_id=s.archive_root_id AND r.relative_path=s.relative_path
                  AND r.physical_identity IS NOT DISTINCT FROM a.physical_identity
                ORDER BY s.id LIMIT 32
            """), {"after": after, "ownership": OWNERSHIP_KM_VMS, "source": METADATA_SOURCE_RECORDER,
                   "instance": self.instance_id, "contract": RECORDER_RECEIPT_CONTRACT_VERSION}).mappings().all()

    def _unconfirmed(self, row, reason):
        # Do not destroy the file or claim success. Keep it eligible for a later
        # restart if a temporary volume/space failure prevented this attempt.
        with self.engine.begin() as connection:
            connection.execute(text("""
                UPDATE recording_segments SET integrity_error=:reason,
                    content_probe_status='recovery_unconfirmed', updated_at=NOW()
                WHERE id=:id AND job_id=:job AND status IN ('writing','stale_writing')
                    AND deleted_at IS NULL AND ownership=:ownership AND source=:source
            """), {"id": row["id"], "job": row["job_id"], "reason": reason[:180],
                   "ownership": OWNERSHIP_KM_VMS, "source": METADATA_SOURCE_RECORDER})
        self.log_event("warning", "startup_segment_recovery_unconfirmed", camera_id=row["camera_id"], segment_id=row["id"], reason=reason)

    def _submit(self, row):
        relative = Path(row["relative_path"])
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("segment_recovery_path_mismatch")
        root = self.root_mapper(row["archive_root_id"], row["is_active"]) if self.root_mapper else Path(row["root_path"])
        source = root / relative if self.root_mapper else Path(row["file_path"])
        if source.is_symlink():
            raise RuntimeError("segment_recovery_path_mismatch")
        if source.resolve(strict=True) != (root / relative).resolve(strict=True) or not source.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("segment_recovery_path_mismatch")
        original = source.lstat()
        if (str(original.st_ino), str(original.st_dev)) != (row["inode"], row["device_id"]):
            raise RuntimeError("segment_recovery_receipt_mismatch")
        if receipt_object_identity(row["archive_root_id"], row["relative_path"], original) != row["object_identity"]:
            raise RuntimeError("segment_recovery_receipt_mismatch")
        job = SimpleNamespace(camera_id=row["camera_id"], camera_name=row["camera_name_snapshot"],
            db_job_id=row["job_id"], archive_root_id=row["archive_root_id"], archive_root_path=root,
            segment_baseline_paths=set(), known_segment_paths={row["relative_path"]}, processed_segment_names=set(),
            segment_discovery_cache={})
        publish = lambda candidate, expected: self.accounting.finalize_segment_path(
            job, source, verified_stat=expected, replacement_path=candidate, interrupted_instance=self.instance_id)
        failed = lambda reason: self._unconfirmed(row, reason)
        while not self._stop.is_set():
            if self.repairs.submit((row["job_id"], row["relative_path"]), source, original, publish, failed, verify_first=True):
                return
            self._stop.wait(.25)  # Backpressure in this background sweep only.

    def run(self):
        after = 0
        try:
            self._recover_publications()
            while not self._stop.is_set():
                rows = self._rows(after)
                if not rows:
                    return
                for row in rows:
                    if self._stop.is_set():
                        return
                    after = row["id"]
                    try:
                        self._submit(row)
                    except ArchiveMountUnavailable:
                        # A deliberately omitted mount is not evidence of a
                        # damaged file; the next attach can recover it.
                        continue
                    except Exception as exc:
                        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                        self._unconfirmed(row, reason)
        except Exception as exc:
            self.log_event("warning", "startup_segment_recovery_failed", error=type(exc).__name__)

    def _recover_publications(self):
        # Only our tiny known journal directories, never a walk over video files.
        with self.engine.connect() as connection:
            roots = connection.execute(text("SELECT id, root_path, is_active FROM archive_roots WHERE retired_at IS NULL")).all()
        for root_id, root_name, active in roots:
            try:
                root = self.root_mapper(root_id, active) if self.root_mapper else Path(root_name)
            except (OSError, RuntimeError):
                continue
            directory = root / JOURNAL_DIRECTORY
            if directory.is_symlink() or not directory.is_dir():
                continue
            for marker in directory.glob("*.json"):
                if self._stop.is_set():
                    return
                try:
                    payload, source = read_publication(marker, root)
                    with self.engine.begin() as connection:
                        row = connection.execute(text("""
                            SELECT s.archive_root_id, r.object_identity, r.state AS receipt_state,
                                   r.content_fingerprint, s.status, r.device_id, r.inode
                            FROM recording_segments s
                            JOIN recorder_file_receipts r ON r.segment_id=s.id
                            JOIN archive_roots a ON a.id=s.archive_root_id
                            JOIN recording_jobs j ON j.id=s.job_id
                            WHERE a.id=:root_id AND s.relative_path=:relative
                              AND (:mapped OR s.file_path=:path)
                              AND s.deleted_at IS NULL AND s.ownership=:ownership AND s.source=:source
                              AND j.recorder_instance_id IS DISTINCT FROM :instance
                              AND j.state IN ('stopped','error','disabled')
                              AND r.contract_version=:contract AND r.relative_path=s.relative_path
                              AND r.root_id=s.archive_root_id
                              AND r.physical_identity IS NOT DISTINCT FROM a.physical_identity
                            FOR UPDATE OF s,r,a
                        """), {"path": str(source), "relative": payload["relative_path"], "root_id": root_id,
                               "mapped": self.root_mapper is not None,
                               "ownership": OWNERSHIP_KM_VMS, "source": METADATA_SOURCE_RECORDER,
                               "instance": self.instance_id, "contract": RECORDER_RECEIPT_CONTRACT_VERSION}).mappings().one_or_none()
                        if row is None:
                            raise RuntimeError("segment_recovery_journal_owner_mismatch")
                        committed = row["status"] == row["receipt_state"] == "finalized"
                        original = source if committed else source.parent / payload["backup"]
                        if not original.exists() and not committed:
                            original = source
                        expected = payload["new"] if committed else payload["old"]
                        if file_identity(original) != expected or expected != [row["device_id"], row["inode"]]:
                            raise RuntimeError("segment_recovery_journal_receipt_mismatch")
                        if receipt_object_identity(row["archive_root_id"], payload["relative_path"], original.lstat()) != row["object_identity"]:
                            raise RuntimeError("segment_recovery_journal_receipt_mismatch")
                        if committed:
                            if bounded_file_fingerprint(source, source.lstat()) != row["content_fingerprint"]:
                                raise RuntimeError("segment_recovery_journal_fingerprint_mismatch")
                        elif row["status"] not in {"writing", "stale_writing"} or row["receipt_state"] != "writing":
                            raise RuntimeError("segment_recovery_journal_state_mismatch")
                        reconcile_publication(marker, root, committed=committed)
                except Exception as exc:
                    self.log_event("warning", "startup_publication_recovery_unconfirmed", reason=type(exc).__name__)
