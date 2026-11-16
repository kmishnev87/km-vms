"""One bounded background remux; originals survive every failed publication."""
from __future__ import annotations

import os
import json
import hashlib
import queue
import shutil
import stat
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from .segment_recovery import inspect_closed_segment, same_file_version


JOURNAL_DIRECTORY = ".km-vms-recorder-recovery"


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_identity(path: Path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise RuntimeError("segment_recovery_invalid_object")
    return [str(value.st_dev), str(value.st_ino)]


def read_publication(marker: Path, root: Path):
    if not stat.S_ISREG(marker.lstat().st_mode) or marker.stat().st_size > 8192:
        raise RuntimeError("segment_recovery_invalid_journal")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    relative = Path(payload["relative_path"])
    if payload.get("version") != 1 or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("segment_recovery_invalid_journal")
    source = root / relative
    if not source.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("segment_recovery_invalid_journal")
    for field, prefix, suffix in (("backup", ".recovery-original-", ".bak"),
                                  ("candidate", ".recovery-remux-", source.suffix)):
        name = payload[field]
        if Path(name).name != name or not name.startswith(prefix) or not name.endswith(suffix):
            raise RuntimeError("segment_recovery_invalid_journal")
    return payload, source


def reconcile_publication(marker: Path, root: Path, *, committed: bool):
    """Caller holds the exact catalog/receipt locks and has proved their identity."""
    payload, source = read_publication(marker, root)
    backup, candidate = source.parent / payload["backup"], source.parent / payload["candidate"]
    current = file_identity(source) if source.exists() else None
    if backup.exists() and file_identity(backup) != payload["old"]:
        raise RuntimeError("segment_recovery_backup_identity_changed")
    if committed:
        if current != payload["new"]:
            raise RuntimeError("segment_recovery_publication_conflict")
    elif current == payload["new"] or current is None:
        if not backup.exists():
            raise RuntimeError("segment_recovery_original_missing")
        os.replace(backup, source)
    elif current != payload["old"]:
        raise RuntimeError("segment_recovery_publication_conflict")
    if candidate.exists():
        if file_identity(candidate) != payload["new"]:
            raise RuntimeError("segment_recovery_candidate_identity_changed")
        candidate.unlink()
    backup.unlink(missing_ok=True)
    sync_directory(source.parent)
    marker.unlink()
    sync_directory(marker.parent)


class ReplacementPublication:
    """Keep the old inode until the caller's surrounding DB transaction commits.

    Enter before engine.begin(); call publish only after locking the exact
    segment/receipt. Exit after DB commit/rollback, never the other way round.
    """

    def __init__(self, source: Path, replacement: Path | None, expected: os.stat_result, *, root: Path | None = None):
        self.source, self.replacement, self.expected = source, replacement, expected
        self.backup: Path | None = None
        self.published = False
        self.root = root or source.parent
        self.marker: Path | None = None

    def __enter__(self):
        return self

    def publish(self):
        if self.replacement is None:
            return
        if not same_file_version(self.expected, self.source.lstat()):
            raise RuntimeError("segment_recovery_file_changed")
        fd, name = tempfile.mkstemp(prefix=".recovery-original-", suffix=".bak", dir=self.source.parent)
        os.close(fd)
        self.backup = Path(name)
        self.backup.unlink()
        directory = self.root / JOURNAL_DIRECTORY
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise RuntimeError("segment_recovery_invalid_journal")
        relative = str(self.source.relative_to(self.root))
        marker = directory / (hashlib.sha256(relative.encode()).hexdigest() + ".json")
        payload = {"version": 1, "relative_path": relative, "backup": self.backup.name,
                   "candidate": self.replacement.name, "old": file_identity(self.source),
                   "new": file_identity(self.replacement)}
        # Persist intent BEFORE linking/renaming. A second outage can safely
        # roll back the file side, or clean up after an already committed DB.
        journal_fd, journal_name = tempfile.mkstemp(prefix=".journal-", suffix=".tmp", dir=directory)
        temporary_marker = Path(journal_name)
        try:
            with os.fdopen(journal_fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush(); os.fsync(handle.fileno())
            os.link(temporary_marker, marker, follow_symlinks=False)
            self.marker = marker
        finally:
            temporary_marker.unlink(missing_ok=True)
        sync_directory(directory)
        sync_directory(self.root)
        os.link(self.source, self.backup, follow_symlinks=False)
        sync_directory(self.source.parent)
        current = self.source.lstat()
        # Linking intentionally changes ctime, but never file content/mtime.
        if any(getattr(current, k) != getattr(self.expected, k)
               for k in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
            raise RuntimeError("segment_recovery_file_changed")
        os.replace(self.replacement, self.source)
        self.published = True
        self._sync_directory()

    def _sync_directory(self):
        sync_directory(self.source.parent)

    def __exit__(self, error_type, error, traceback):
        if self.backup is None:
            return False
        if self.backup.exists():
            original = self.backup.lstat()
            if (original.st_dev, original.st_ino) != (self.expected.st_dev, self.expected.st_ino):
                raise RuntimeError("segment_recovery_backup_identity_changed")
        if error_type is not None and self.published:
            os.replace(self.backup, self.source)
            self._sync_directory()
        else:
            self.backup.unlink(missing_ok=True)
        if self.marker is not None:
            self.marker.unlink(missing_ok=True)
            sync_directory(self.marker.parent)
        return False


def remux_closed_segment(source: Path, expected: os.stat_result, *, timeout: float = 180.0) -> Path:
    """Produce a verified sibling, without overwriting the source or decoding video."""
    if not stat.S_ISREG(expected.st_mode) or not same_file_version(expected, source.lstat()):
        raise RuntimeError("segment_recovery_file_changed")
    if source.suffix.lower() not in {".mkv", ".mp4"}:
        raise RuntimeError("segment_recovery_unsupported_format")
    if shutil.disk_usage(source.parent).free < expected.st_size + 64 * 1024 * 1024:
        raise RuntimeError("segment_recovery_insufficient_space")
    fd, name = tempfile.mkstemp(prefix=".recovery-remux-", suffix=source.suffix, dir=source.parent)
    os.close(fd)
    candidate = Path(name)
    try:
        command = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                   "-fflags", "+genpts", "-i", str(source),
                   "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                   "-avoid_negative_ts", "make_zero"]
        if source.suffix.lower() == ".mp4":
            command += ["-movflags", "+faststart"]
        command += ["-f", "matroska" if source.suffix.lower() == ".mkv" else "mp4", "-y", str(candidate)]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False)
        if result.returncode != 0:
            raise RuntimeError("segment_recovery_remux_failed")
        if not same_file_version(expected, source.lstat()):
            raise RuntimeError("segment_recovery_file_changed")
        os.chmod(candidate, stat.S_IMODE(expected.st_mode))
        os.utime(candidate, ns=(expected.st_atime_ns, expected.st_mtime_ns))
        with candidate.open("rb") as handle:
            os.fsync(handle.fileno())
        verified, reason = inspect_closed_segment(candidate)
        if verified is None:
            raise RuntimeError(reason)
        return candidate
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise


class SegmentRepairQueue:
    """Bounded memory/I/O and one worker; never block healthy camera orchestration."""

    def __init__(self, capacity: int = 16):
        self._queue = queue.Queue(maxsize=capacity)
        self._pending: set[tuple[str, str]] = set()
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None

    def contains(self, job_id: str, relative_path: str) -> bool:
        with self._guard:
            return (job_id, relative_path) in self._pending

    def paths(self, job_id: str) -> list[str]:
        with self._guard:
            return [path for current_job, path in self._pending if current_job == job_id]

    def submit(self, key: tuple[str, str], source: Path, expected: os.stat_result,
               publish: Callable, failed: Callable, *, verify_first: bool = False) -> bool:
        with self._guard:
            if key in self._pending:
                return True
            if len(self._pending) >= self._queue.maxsize:
                return False
            self._pending.add(key)
            self._queue.put_nowait((key, source, expected, publish, failed, verify_first))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="segment-remux", daemon=True)
                self._thread.start()
            return True

    def _run(self):
        while True:
            key, source, expected, publish, failed, verify_first = self._queue.get()
            candidate = None
            try:
                verified = inspect_closed_segment(source)[0] if verify_first else None
                if verified is None:
                    candidate = remux_closed_segment(source, expected)
                if not publish(candidate, expected):
                    raise RuntimeError("segment_recovery_publication_refused")
            except Exception as exc:
                try:
                    failed(type(exc).__name__ if not isinstance(exc, RuntimeError) else str(exc))
                except Exception:
                    pass  # A failed catalog is not permission to touch another file.
            finally:
                if candidate is not None:
                    candidate.unlink(missing_ok=True)
                with self._guard:
                    self._pending.discard(key)
                self._queue.task_done()
