"""Only installer-approved archive bind targets may receive secondary writes."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


CRITICAL_FREE_PERCENT = 1.0
RECOVERY_FREE_PERCENT = 9.0
RETURN_STABILITY_SECONDS = 30.0
RUNTIME_ROOTS = Path(os.environ.get("KMVMS_ARCHIVE_ROOTS_RUNTIME_BASE") or "/storage/archive-roots")
ROOT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,36}$")


def mounted_paths() -> frozenset[str]:
    with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
        return frozenset(
            re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), row.split(" ")[4])
            for row in mountinfo
        )


def mounted_secondary_root(root_id: str, mounts=None) -> Path:
    safe_id = root_id.strip(".-").lower()
    if not ROOT_ID.fullmatch(root_id) or not safe_id:
        raise RuntimeError("camera_archive_root_unavailable")
    path = RUNTIME_ROOTS / safe_id
    try:
        # os.path.ismount() cannot reliably detect same-filesystem bind mounts.
        # Never write into the container's empty mountpoint after a mount loss.
        mounted = path.as_posix() in (mounted_paths() if mounts is None else mounts)
        if not mounted or path.is_symlink() or not path.is_dir():
            raise RuntimeError("camera_archive_root_unavailable")
    except OSError as exc:
        raise RuntimeError("camera_archive_root_unavailable") from exc
    return path


def secondary_write_state(root_path: Path, namespace: str, *, was_full=False) -> str | None:
    return probe_write(root_path, namespace, was_full=was_full)[0]


def probe_write(root_path: Path, namespace: str, *, was_full=False):
    path = root_path / namespace
    try:
        if path.resolve(strict=True) != path or not path.is_dir():
            return "camera_archive_root_unavailable", 0, None
        fs = os.statvfs(path)
        device = path.stat().st_dev
        if fs.f_flag & os.ST_RDONLY or not os.access(path, os.W_OK | os.X_OK):
            return "camera_archive_root_read_only", 0, device
        threshold = RECOVERY_FREE_PERCENT if was_full else CRITICAL_FREE_PERCENT
        if not fs.f_blocks or fs.f_bavail * 100 / fs.f_blocks < threshold:
            return "camera_archive_root_low_disk", 0, device
        return None, fs.f_bavail * getattr(fs, "f_frsize", 1), device
    except OSError:
        return "camera_archive_root_unavailable", 0, None


@dataclass(frozen=True)
class Destination:
    root_id: str
    volume_id: str | None
    path: Path | None
    bytes_available: int
    device: int | None
    problem: str | None

    @property
    def ready(self):
        return self.path is not None and self.problem is None


@dataclass(frozen=True)
class Route:
    primary_id: str
    target: Destination | None
    problem: str | None = None
    healthy_since: float | None = None

    @property
    def fallback(self):
        return self.target is not None and self.target.root_id != self.primary_id


def _other_volume(primary: Destination, choice: Destination) -> bool:
    if primary.root_id == choice.root_id:
        return False
    if primary.device is not None and primary.device == choice.device:
        return False
    if primary.volume_id and choice.volume_id:
        return primary.volume_id != choice.volume_id
    return primary.device is not None and choice.device is not None


def select_route(primary: Destination, destinations: dict[str, Destination], previous: Route | None, *,
                 allow_fallback: bool, now: float) -> Route:
    former = None
    if allow_fallback and previous and previous.primary_id == primary.root_id and previous.fallback:
        candidate = destinations.get(previous.target.root_id)
        if candidate and candidate.ready and _other_volume(primary, candidate):
            former = candidate
    if primary.ready:
        if former:
            since = previous.healthy_since if previous.healthy_since is not None else now
            if now - since < RETURN_STABILITY_SECONDS:
                return Route(primary.root_id, former, previous.problem, since)
        return Route(primary.root_id, primary)
    if former:
        return Route(primary.root_id, former, primary.problem)
    if allow_fallback:
        # Prefer absolute writable bytes. Stable ties don't reshuffle cameras.
        candidates = (item for item in destinations.values() if item.ready and _other_volume(primary, item))
        target = min(candidates, key=lambda item: (-item.bytes_available, item.root_id), default=None)
        if target:
            return Route(primary.root_id, target, primary.problem)
    return Route(primary.root_id, None, primary.problem or "camera_archive_root_unavailable")


def writer_disk_problem(message: str | None) -> str | None:
    detail = (message or "").lower()
    if "no space left on device" in detail:
        return "camera_archive_root_low_disk"
    if "read-only file system" in detail:
        return "camera_archive_root_read_only"
    if any(key in detail for key in ("mux", "writing", "output", "trailer", "/storage/archive")) and any(
        key in detail for key in ("input/output error", "permission denied", "stale file handle", "no such file or directory")
    ):
        return "camera_archive_root_unavailable"
    return None
