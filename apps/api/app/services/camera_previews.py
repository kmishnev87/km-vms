"""Temporary onboarding frames, separate from saved camera previews and archives."""
from pathlib import Path
import re
import time

from app.core.config import settings

TEST_PREVIEW_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TEST_PREVIEW_TTL_SECONDS = 24 * 60 * 60


def _test_preview_root() -> Path | None:
    root = Path(settings.storage_previews) / "camera-tests"
    if root.is_symlink() or not root.is_dir():
        return None
    try:
        root.resolve().relative_to(Path(settings.storage_previews).resolve())
    except (OSError, ValueError):
        return None
    return root


def discard_test_previews(tokens: list[str]) -> int:
    root = _test_preview_root()
    if root is None:
        return 0
    removed = 0
    for token in set(tokens):
        if not TEST_PREVIEW_TOKEN_RE.fullmatch(token):
            continue
        path = root / f"{token}.jpg"
        try:
            if path.is_symlink() or not path.is_file():
                continue
            path.unlink()
            removed += 1
        except OSError:
            # Cleanup must not turn cancelling a form into a failed camera action.
            continue
    return removed


def purge_expired_test_previews(now: float | None = None) -> int:
    """Run only during an explicit video test; no timer or additional service."""
    root = _test_preview_root()
    if root is None:
        return 0
    cutoff = (time.time() if now is None else now) - TEST_PREVIEW_TTL_SECONDS
    expired = []
    try:
        for path in root.glob("*.jpg"):
            try:
                if TEST_PREVIEW_TOKEN_RE.fullmatch(path.stem) and not path.is_symlink() and path.is_file() and path.stat().st_mtime < cutoff:
                    expired.append(path.stem)
            except OSError:
                continue
    except OSError:
        return 0
    return discard_test_previews(expired)
