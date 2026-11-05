"""User credential generation, deliberately outside database backups.

Restoring a database can recycle numeric user ids. Rotate before either source
restore or rollback, never the shared maintenance/signing/encryption secrets.
"""
import fcntl
import os
from pathlib import Path
import re
import secrets
import tempfile

from app.core.config import settings

GENERATION_FILE = "user-session-generation"


def _path() -> Path:
    return Path(settings.maintenance_control_root) / "user-sessions" / GENERATION_FILE


def _read(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="ascii") as handle:
        value = handle.read(65).strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("user_session_generation_invalid")
    return value


def _write(path: Path) -> str:
    value = secrets.token_hex(16)
    descriptor, temporary = tempfile.mkstemp(prefix=".user-sessions-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return value


def user_session_generation(*, rotate: bool = False, create: bool = False) -> str:
    path = _path()
    if not rotate:
        try:
            return _read(path)
        except FileNotFoundError:
            if not create:
                raise
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not rotate and path.exists():
            return _read(path)
        return _write(path)


def validate_user_session_generation(payload: dict) -> None:
    generation = payload.get("usg")
    if not isinstance(generation, str) or generation != user_session_generation():
        raise ValueError("user_session_expired")
