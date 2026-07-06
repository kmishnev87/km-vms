from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.version import installed_build_metadata
from app.models.system_settings import SystemSettings


UPDATE_REPORT_VERSION = "stage612.update_status.v1"
UPDATE_INTERVAL = timedelta(hours=24)
MAX_METADATA_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PUBLIC_COMMIT_BYTES = 512 * 1024
MAX_TEXT = 300
PUBLIC_REPO_DEFAULT = "kmishnev87/km-vms"
PUBLIC_RELEASE_DESCRIPTOR_RELATIVE = "release/km-vms-release.json"
PUBLIC_RELEASE_TIMEOUT_SECONDS = 5
RELEASE_DESCRIPTOR_RELATIVE = Path("release/km-vms-release.json")
SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,119}$")
SAFE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9._-]+)?$")
SENSITIVE_KEY_RE = re.compile(r"(password|passwd|secret|token|authorization|jwt|credential|private[_-]?key|cookie|session)", re.IGNORECASE)
SENSITIVE_VALUE_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9_]+|Bearer\s+[A-Za-z0-9._~+/=-]+|rtsp://[^@\s]+@|postgresql://[^:\s]+:[^@\s]+@|-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)

_LAST_RESULT: dict[str, Any] | None = None
_LAST_SUCCESSFUL_RESULT: dict[str, Any] | None = None
_CHECK_IN_PROGRESS = False


class UpdateCheckBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("summary") or diagnostics.get("message") or status)


@dataclass
class UpdateMetadataWarning:
    code: str
    message: str
    severity: str = "info"
    field: str | None = None


@dataclass
class UpdateBlocker:
    code: str
    message: str
    severity: str = "high"


@dataclass
class UpdateInstalledState:
    status: str
    installed_version: str | None
    installed_commit: str | None
    installed_title: str | None
    installed_summary: str | None
    source_kind: str | None
    repo: str | None
    ref: str | None
    channel: str | None
    release_channel: str | None
    installed_at: str | None
    installed_by: str | None
    metadata_source: str | None
    release_metadata_status: str | None
    last_update_status: str | None
    last_update_finished_at: str | None
    last_failed_phase: str | None
    metadata_validity: str
    identity_validity: str
    git_head: str | None
    legacy_source_commit: str | None
    legacy_update_commit: str | None
    warnings: list[UpdateMetadataWarning] = field(default_factory=list)


@dataclass
class UpdateManifestSummary:
    schema_version: int
    channel: str
    version: str
    git_ref: str | None
    commit: str | None
    published_at: str | None
    title: str | None
    summary: str | None
    release_notes_url: str | None
    requires_backup: bool
    requires_manual_action: bool
    requires_migration: bool
    minimum_current_version: str | None
    source_type: str | None
    source_repo: str | None
    source_ref: str | None
    breaking_changes: list[str] = field(default_factory=list)


@dataclass
class UpdateCheckResult:
    status: str
    installed: UpdateInstalledState
    latest: UpdateManifestSummary | None
    blockers: list[UpdateBlocker]
    warnings: list[UpdateMetadataWarning]
    checked_at: str
    manifest_source_status: str
    can_apply_from_ui: bool = False


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat() + "Z"


def _sanitize_text(value: Any, *, max_length: int = MAX_TEXT) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = SENSITIVE_VALUE_RE.sub("***", str(value).strip())
    return text[:max_length] or None


def _safe_field(key: str, value: Any, *, max_length: int = MAX_TEXT) -> str | None:
    if SENSITIVE_KEY_RE.search(str(key)):
        return None
    text = _sanitize_text(value, max_length=max_length)
    if text and SENSITIVE_VALUE_RE.search(text):
        return None
    return text


def _safe_timestamp(value: Any) -> str | None:
    text = _sanitize_text(value, max_length=80)
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return text


def _safe_url(value: Any) -> str | None:
    text = _sanitize_text(value, max_length=300)
    if text is None:
        return None
    if text.startswith(("https://", "http://")) and not SENSITIVE_VALUE_RE.search(text):
        return text
    return None


def _read_json_file(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        if not path.exists():
            return None, "missing"
        if path.stat().st_size > MAX_METADATA_BYTES:
            return None, "too_large"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "invalid_shape"
    return payload, "valid"


def _app_root() -> Path:
    return Path(os.getenv("KMVMS_APP_ROOT") or os.getenv("KM_VMS_APP_DIR") or Path.cwd())


def _metadata_paths(root: Path | None = None) -> tuple[Path, Path]:
    base = root or _app_root()
    return base / ".km-vms-source.json", base / ".km-vms-update.json"


def _release_identity_path(root: Path | None = None) -> Path:
    return (root or _app_root()) / ".km-vms-release.json"


def _release_descriptor_path(root: Path | None = None) -> Path:
    return (root or _app_root()) / RELEASE_DESCRIPTOR_RELATIVE


def _read_git_head(root: Path | None = None) -> str | None:
    base = root or _app_root()
    git_dir = base / ".git"
    try:
        if git_dir.is_file():
            text = git_dir.read_text(encoding="utf-8", errors="ignore").strip()
            if text.startswith("gitdir:"):
                git_dir = (base / text.split(":", 1)[1].strip()).resolve()
        head_path = git_dir / "HEAD"
        if not head_path.is_file():
            return None
        head = head_path.read_text(encoding="utf-8", errors="ignore").strip()
        if SHA_RE.fullmatch(head):
            return head.lower()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_path = git_dir / ref
            if ref_path.is_file():
                value = ref_path.read_text(encoding="utf-8", errors="ignore").strip()
                return value.lower() if SHA_RE.fullmatch(value) else None
    except Exception:
        return None
    return None


def read_installed_update_state(*, app_root: Path | None = None) -> UpdateInstalledState:
    build = installed_build_metadata()
    source_path, update_path = _metadata_paths(app_root)
    release_path = _release_identity_path(app_root)
    source_payload, source_validity = _read_json_file(source_path)
    update_payload, update_validity = _read_json_file(update_path)
    release_payload, release_validity = _read_json_file(release_path)
    git_head = _read_git_head(app_root)
    warnings: list[UpdateMetadataWarning] = []
    if release_validity != "valid":
        warnings.append(UpdateMetadataWarning("release_identity_" + release_validity, "Installed release identity is unavailable or invalid.", field=".km-vms-release.json"))
    if source_validity not in {"valid", "missing"}:
        warnings.append(UpdateMetadataWarning("source_metadata_" + source_validity, "Installed source metadata is unavailable or invalid.", field=".km-vms-source.json"))
    if update_validity not in {"valid", "missing"}:
        warnings.append(UpdateMetadataWarning("update_metadata_" + update_validity, "Last update metadata is unavailable or invalid.", field=".km-vms-update.json"))

    source = source_payload or {}
    update = update_payload or {}
    release = release_payload or {}
    source_schema = source.get("schema_version")
    update_schema = update.get("schema_version")
    release_schema = release.get("schema_version")
    if release_payload and release_schema != 1:
        warnings.append(UpdateMetadataWarning("release_identity_unsupported_schema", "Installed release identity schema is unsupported.", field="schema_version"))
    if source_payload and source_schema != 1:
        warnings.append(UpdateMetadataWarning("source_metadata_unsupported_schema", "Installed source metadata schema is unsupported.", field="schema_version"))
    if update_payload and update_schema != 1:
        warnings.append(UpdateMetadataWarning("update_metadata_unsupported_schema", "Last update metadata schema is unsupported.", field="schema_version"))

    legacy_source_commit = _safe_field("commit_sha", source.get("commit_sha"), max_length=40)
    legacy_update_commit = _safe_field("commit_sha", update.get("commit_sha"), max_length=40)
    release_metadata_status = _safe_field("metadata_status", release.get("metadata_status"), max_length=40)
    if release_validity == "valid" and release_metadata_status in {"precompose", "partial"}:
        warnings.append(UpdateMetadataWarning("release_identity_" + release_metadata_status, "Installed release identity was written before update verification completed.", severity="high", field=".km-vms-release.json"))
    repo = _safe_field("source_repo", release.get("source_repo") or source.get("github_repo") or update.get("github_repo"), max_length=160)
    ref = _safe_field("source_ref", release.get("source_ref") or source.get("ref") or update.get("ref"), max_length=120)
    commit = _safe_field("commit_sha", release.get("commit_sha") or build.get("git_commit"), max_length=40)
    if not commit and release_validity != "valid":
        commit = _safe_field("commit_sha", legacy_source_commit or legacy_update_commit, max_length=40)
    if commit and not SHA_RE.fullmatch(commit):
        warnings.append(UpdateMetadataWarning("installed_commit_invalid", "Installed commit value is not a valid SHA-like value.", field="commit_sha"))
        commit = None
    for code, value in (("legacy_source_commit_invalid", legacy_source_commit), ("legacy_update_commit_invalid", legacy_update_commit)):
        if value and not SHA_RE.fullmatch(value):
            warnings.append(UpdateMetadataWarning(code, "Legacy update metadata commit value is not valid.", field="commit_sha"))
    if git_head and commit and git_head.lower() != commit.lower():
        warnings.append(UpdateMetadataWarning("installed_identity_drift", "Installed release identity does not match the deployed git HEAD.", severity="high", field=".km-vms-release.json"))
    if commit and legacy_source_commit and legacy_source_commit.lower() != commit.lower():
        warnings.append(UpdateMetadataWarning("legacy_source_metadata_mismatch", "Legacy source metadata does not match installed release identity.", field=".km-vms-source.json"))
    if commit and legacy_update_commit and legacy_update_commit.lower() != commit.lower():
        warnings.append(UpdateMetadataWarning("legacy_update_metadata_mismatch", "Legacy update metadata does not match installed release identity.", field=".km-vms-update.json"))
    version = _safe_field("version", release.get("version"), max_length=80)
    if not version and build.get("metadata_source") != "development_fallback":
        version = _safe_field("app_version", build.get("app_version"), max_length=80)
    last_status = _safe_field("status", update.get("status"), max_length=40)
    last_failed_phase = _safe_field("failed_phase", update.get("failed_phase"), max_length=80)
    legacy_validity = "valid" if source_validity == "valid" and update_validity in {"valid", "missing"} else ("missing" if source_validity == "missing" and update_validity == "missing" else "invalid")
    if release_validity == "valid" and release_metadata_status in {"precompose", "partial"}:
        status = "identity_incomplete"
        identity_validity = release_metadata_status
    elif release_validity == "valid" and commit and git_head and git_head.lower() != commit.lower():
        status = "installed_identity_drift"
        identity_validity = "drift"
    elif release_validity == "valid":
        status = "known"
        identity_validity = "valid"
    elif legacy_validity == "valid":
        status = "identity_incomplete"
        identity_validity = "missing"
    elif legacy_validity == "missing":
        status = "identity_incomplete"
        identity_validity = "missing"
    else:
        status = "metadata_invalid"
        identity_validity = "invalid"
    return UpdateInstalledState(
        status=status,
        installed_version=version,
        installed_commit=commit,
        installed_title=_safe_field("title", release.get("title"), max_length=160),
        installed_summary=_safe_field("summary", release.get("summary"), max_length=800),
        source_kind=_safe_field("source_kind", release.get("source_kind") or source.get("source_kind") or update.get("source_kind") or build.get("install_source"), max_length=80),
        repo=repo,
        ref=ref,
        channel=_safe_field("channel", os.getenv("KMVMS_UPDATE_CHANNEL_ID") or release.get("source_channel") or build.get("source_channel_id") or ref, max_length=80),
        release_channel=_safe_field("release_channel", release.get("release_channel"), max_length=80),
        installed_at=_safe_timestamp(release.get("installed_at")),
        installed_by=_safe_field("installed_by", release.get("installed_by"), max_length=80),
        metadata_source=_safe_field("metadata_source", release.get("metadata_source") or build.get("metadata_source"), max_length=80),
        release_metadata_status=release_metadata_status,
        last_update_status=last_status,
        last_update_finished_at=_safe_timestamp(update.get("finished_at")),
        last_failed_phase=last_failed_phase,
        metadata_validity=legacy_validity,
        identity_validity=identity_validity,
        git_head=git_head,
        legacy_source_commit=legacy_source_commit,
        legacy_update_commit=legacy_update_commit,
        warnings=warnings,
    )


def _asdict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value


def _semver(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = SEMVER_RE.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def compare_versions(installed: Any, latest: Any) -> dict[str, Any]:
    current = _semver(_sanitize_text(installed, max_length=80))
    target = _semver(_sanitize_text(latest, max_length=80))
    if current is None or target is None:
        return {"ordering": "unknown_ordering", "reason": "non_semver_or_unknown_version"}
    if target > current:
        return {"ordering": "newer_available"}
    if target == current:
        return {"ordering": "same_version"}
    return {"ordering": "installed_newer_than_channel"}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:20]:
        text = _sanitize_text(item, max_length=160)
        if text:
            result.append(text)
    return result


def _bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise UpdateCheckBlocked("check_failed", {"summary": f"Release manifest field {key} must be boolean.", "error_category": "manifest_schema_invalid"})
    return value


def _manifest_text(payload: dict[str, Any], key: str, *, required: bool = False, max_length: int = MAX_TEXT) -> str | None:
    value = _safe_field(key, payload.get(key), max_length=max_length)
    if required and not value:
        raise UpdateCheckBlocked("check_failed", {"summary": f"Release manifest field {key} is required.", "error_category": "manifest_schema_invalid"})
    return value


def _normalize_manifest(payload: dict[str, Any]) -> UpdateManifestSummary:
    if payload.get("schema_version") != 1:
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest schema_version is unsupported.", "error_category": "manifest_schema_invalid"})
    channel = _manifest_text(payload, "channel", required=True, max_length=80) or "stable"
    version = _manifest_text(payload, "version", required=True, max_length=80) or "unknown"
    git_ref = _manifest_text(payload, "git_ref", max_length=120)
    commit = _manifest_text(payload, "commit", max_length=40)
    if git_ref and not SAFE_REF_RE.fullmatch(git_ref):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest git_ref is invalid.", "error_category": "manifest_schema_invalid"})
    if commit and not SHA_RE.fullmatch(commit):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest commit is invalid.", "error_category": "manifest_schema_invalid"})
    published_at = _safe_timestamp(payload.get("published_at"))
    if payload.get("published_at") is not None and not published_at:
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest published_at is invalid.", "error_category": "manifest_schema_invalid"})
    minimum = _manifest_text(payload, "minimum_current_version", max_length=80)
    if minimum and not _semver(minimum):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest minimum_current_version is invalid.", "error_category": "manifest_schema_invalid"})
    artifacts = payload.get("artifacts")
    source = artifacts.get("source") if isinstance(artifacts, dict) and isinstance(artifacts.get("source"), dict) else {}
    source_type = _safe_field("type", source.get("type"), max_length=80)
    source_repo = _safe_field("repo", source.get("repo"), max_length=160)
    source_ref = _safe_field("ref", source.get("ref"), max_length=120)
    if source_repo and not SAFE_REPO_RE.fullmatch(source_repo):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest source repo is invalid.", "error_category": "manifest_schema_invalid"})
    if source_ref and not SAFE_REF_RE.fullmatch(source_ref):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest source ref is invalid.", "error_category": "manifest_schema_invalid"})
    return UpdateManifestSummary(
        schema_version=1,
        channel=channel,
        version=version,
        git_ref=git_ref,
        commit=commit,
        published_at=published_at,
        title=_manifest_text(payload, "title", max_length=160),
        summary=_manifest_text(payload, "summary", max_length=800),
        release_notes_url=_safe_url(payload.get("release_notes_url")),
        requires_backup=_bool(payload, "requires_backup"),
        requires_manual_action=_bool(payload, "requires_manual_action"),
        requires_migration=_bool(payload, "requires_migration"),
        minimum_current_version=minimum,
        source_type=source_type,
        source_repo=source_repo,
        source_ref=source_ref,
        breaking_changes=_string_list(payload.get("breaking_changes")),
    )


def read_trusted_local_manifest(path: Path) -> UpdateManifestSummary:
    resolved = path.expanduser()
    try:
        if not resolved.exists():
            raise UpdateCheckBlocked("not_configured", {"summary": "Trusted release manifest is unavailable.", "error_category": "manifest_missing"})
        if resolved.stat().st_size > MAX_MANIFEST_BYTES:
            raise UpdateCheckBlocked("check_failed", {"summary": "Trusted release manifest exceeds size limit.", "error_category": "manifest_too_large"})
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except UpdateCheckBlocked:
        raise
    except Exception as exc:
        raise UpdateCheckBlocked("check_failed", {"summary": type(exc).__name__, "error_category": "manifest_invalid_json"}) from exc
    if not isinstance(payload, dict):
        raise UpdateCheckBlocked("check_failed", {"summary": "Trusted release manifest must be a JSON object.", "error_category": "manifest_not_object"})
    return _normalize_manifest(payload)


def _manifest_path() -> Path | None:
    raw = os.getenv("KMVMS_UPDATE_MANIFEST_PATH")
    return Path(raw) if raw else None


def _public_release_descriptor_path() -> Path | None:
    raw = os.getenv("KMVMS_PUBLIC_RELEASE_MANIFEST_PATH")
    if raw:
        path = Path(raw)
        return path if path.exists() else None
    return None


def _public_provider_enabled() -> bool:
    return str(os.getenv("KMVMS_PUBLIC_RELEASE_PROVIDER", "1")).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _public_release_descriptor_url() -> str | None:
    if not _public_provider_enabled():
        return None
    url = _sanitize_text(os.getenv("KMVMS_PUBLIC_RELEASE_MANIFEST_URL"), max_length=300)
    if not url or not url.startswith("https://"):
        return None
    return url


def _public_provider_mode() -> str:
    return str(os.getenv("KMVMS_PUBLIC_RELEASE_PROVIDER_MODE") or "release_tag").strip().lower()


def _public_timeout_seconds() -> float:
    raw = os.getenv("KMVMS_PUBLIC_RELEASE_TIMEOUT_SECONDS")
    if not raw:
        return PUBLIC_RELEASE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return PUBLIC_RELEASE_TIMEOUT_SECONDS
    return min(max(value, 1.0), 15.0)


def _read_public_release_payload(url: str) -> dict[str, Any]:
    if not url.startswith("https://"):
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release provider URL must use HTTPS.", "error_category": "provider_url_invalid"})
    request = urllib.request.Request(url, headers={"User-Agent": "KM-VMS-Update-Check/0.7.1", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_public_timeout_seconds()) as response:  # nosec B310 - public HTTPS release metadata only
            data = response.read(MAX_MANIFEST_BYTES + 1)
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError):
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release metadata is temporarily unavailable.", "error_category": "public_provider_unavailable"})
    if len(data) > MAX_MANIFEST_BYTES:
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release metadata is too large.", "error_category": "public_provider_too_large"})
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release metadata is not valid JSON.", "error_category": "public_provider_invalid_json"})
    if not isinstance(payload, dict):
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release metadata must be a JSON object.", "error_category": "public_provider_invalid_shape"})
    return payload


def _read_public_json_url(url: str, *, max_bytes: int = MAX_MANIFEST_BYTES) -> Any:
    if not url.startswith("https://"):
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release provider URL must use HTTPS.", "error_category": "provider_url_invalid"})
    request = urllib.request.Request(url, headers={"User-Agent": "KM-VMS-Update-Check/0.7.2", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=_public_timeout_seconds()) as response:  # nosec B310 - public GitHub release metadata only
            data = response.read(max_bytes + 1)
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError):
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release metadata is temporarily unavailable.", "error_category": "public_provider_unavailable"})
    if len(data) > max_bytes:
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release metadata is too large.", "error_category": "public_provider_too_large"})
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release metadata is not valid JSON.", "error_category": "public_provider_invalid_json"})


def _tag_version_key(tag: str) -> tuple[int, int, int] | None:
    version = _semver(tag)
    return version if version is not None else None


def _public_release_repo() -> str | None:
    repo = _safe_field("repo", os.getenv("KMVMS_PUBLIC_RELEASE_REPO") or PUBLIC_REPO_DEFAULT, max_length=160)
    return repo if repo and SAFE_REPO_RE.fullmatch(repo) else None


def _discover_latest_public_release_tag(source_repo: str) -> str | None:
    refs_url = f"https://api.github.com/repos/{source_repo}/git/matching-refs/tags/v"
    payload = _read_public_json_url(refs_url)
    if not isinstance(payload, list):
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release tags response is invalid.", "error_category": "public_provider_invalid_shape"})
    tags: list[str] = []
    for item in payload[:200]:
        ref = item.get("ref") if isinstance(item, dict) else None
        if not isinstance(ref, str) or not ref.startswith("refs/tags/"):
            continue
        tag = ref.rsplit("/", 1)[-1]
        if SAFE_TAG_RE.fullmatch(tag) and _tag_version_key(tag):
            tags.append(tag)
    if not tags:
        return None
    return sorted(tags, key=lambda value: _tag_version_key(value) or (0, 0, 0), reverse=True)[0]


def _resolve_public_tag_commit(source_repo: str, tag: str) -> str | None:
    if not SAFE_REPO_RE.fullmatch(source_repo) or not SAFE_TAG_RE.fullmatch(tag):
        return None
    encoded_tag = urllib.parse.quote(tag, safe="")
    try:
        payload = _read_public_json_url(f"https://api.github.com/repos/{source_repo}/git/ref/tags/{encoded_tag}")
    except UpdateCheckBlocked:
        return None
    obj = payload.get("object") if isinstance(payload, dict) else None
    obj_type = _safe_field("type", obj.get("type") if isinstance(obj, dict) else None, max_length=40)
    sha = _safe_field("sha", obj.get("sha") if isinstance(obj, dict) else None, max_length=40)
    if obj_type == "commit" and sha and SHA_RE.fullmatch(sha):
        return sha.lower()
    if obj_type == "tag" and sha and SHA_RE.fullmatch(sha):
        try:
            tag_payload = _read_public_json_url(f"https://api.github.com/repos/{source_repo}/git/tags/{sha}")
        except UpdateCheckBlocked:
            return None
        tag_obj = tag_payload.get("object") if isinstance(tag_payload, dict) else None
        tag_sha = _safe_field("sha", tag_obj.get("sha") if isinstance(tag_obj, dict) else None, max_length=40)
        tag_type = _safe_field("type", tag_obj.get("type") if isinstance(tag_obj, dict) else None, max_length=40)
        if tag_type == "commit" and tag_sha and SHA_RE.fullmatch(tag_sha):
            return tag_sha.lower()
    return None


def _resolve_public_commit(source_repo: str, source_ref: str) -> str | None:
    if SHA_RE.fullmatch(source_ref):
        return source_ref.lower()
    if not SAFE_REPO_RE.fullmatch(source_repo) or not SAFE_REF_RE.fullmatch(source_ref):
        return None
    encoded_ref = urllib.parse.quote(source_ref, safe="/")
    for kind in ("heads", "tags"):
        url = f"https://api.github.com/repos/{source_repo}/git/ref/{kind}/{encoded_ref}"
        request = urllib.request.Request(url, headers={"User-Agent": "KM-VMS-Update-Check/0.7.1", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(request, timeout=_public_timeout_seconds()) as response:  # nosec B310 - public GitHub ref metadata only
                data = response.read(MAX_MANIFEST_BYTES + 1)
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        obj = payload.get("object") if isinstance(payload, dict) else None
        sha = _safe_field("sha", obj.get("sha") if isinstance(obj, dict) else None, max_length=40)
        if sha and SHA_RE.fullmatch(sha):
            return sha.lower()
    encoded_commit_ref = urllib.parse.quote(source_ref, safe="")
    url = f"https://api.github.com/repos/{source_repo}/commits/{encoded_commit_ref}"
    request = urllib.request.Request(url, headers={"User-Agent": "KM-VMS-Update-Check/0.7.1", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=_public_timeout_seconds()) as response:  # nosec B310 - public GitHub commit metadata only
            data = response.read(MAX_PUBLIC_COMMIT_BYTES + 1)
        if len(data) > MAX_PUBLIC_COMMIT_BYTES:
            return None
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    sha = _safe_field("sha", payload.get("sha") if isinstance(payload, dict) else None, max_length=40)
    return sha.lower() if sha and SHA_RE.fullmatch(sha) else None


def _manifest_from_release_payload(payload: dict[str, Any], *, resolve_commit: bool = False) -> UpdateManifestSummary:
    if payload.get("schema_version") != 1:
        raise UpdateCheckBlocked("check_failed", {"summary": "Public release descriptor schema_version is unsupported.", "error_category": "manifest_schema_invalid"})
    source_repo = _safe_field("source_repo", payload.get("source_repo"), max_length=160) or PUBLIC_REPO_DEFAULT
    source_ref = _safe_field("source_ref", payload.get("source_ref") or payload.get("tag"), max_length=120) or "main"
    commit = _manifest_text(payload, "commit_sha", max_length=40)
    if commit and not SHA_RE.fullmatch(commit):
        raise UpdateCheckBlocked("check_failed", {"summary": "Public release descriptor commit_sha is invalid.", "error_category": "manifest_schema_invalid"})
    if not commit and resolve_commit:
        commit = _resolve_public_commit(source_repo, source_ref)
    normalized = {
        "schema_version": 1,
        "channel": _safe_field("release_channel", payload.get("release_channel"), max_length=80) or "public-github",
        "version": _manifest_text(payload, "version", required=True, max_length=80),
        "git_ref": source_ref,
        "commit": commit,
        "published_at": _safe_timestamp(payload.get("published_at")),
        "title": _manifest_text(payload, "title", max_length=160),
        "summary": _manifest_text(payload, "summary", max_length=800),
        "release_notes_url": None,
        "breaking_changes": _string_list(payload.get("changelog")),
        "requires_backup": bool(payload.get("requires_backup") is True),
        "requires_manual_action": bool(payload.get("requires_manual_action") is True),
        "requires_migration": bool(payload.get("requires_migration") is True),
        "minimum_current_version": None,
        "artifacts": {"source": {"type": "github_tarball", "repo": source_repo, "ref": commit or source_ref}},
    }
    return _normalize_manifest(normalized)


def _manifest_from_release_descriptor(path: Path) -> UpdateManifestSummary:
    payload, validity = _read_json_file(path)
    if validity != "valid" or not payload:
        raise UpdateCheckBlocked("check_failed", {"summary": "Public release descriptor is unavailable or invalid.", "error_category": "release_descriptor_" + validity})
    return _manifest_from_release_payload(payload, resolve_commit=False)


def _manifest_from_public_release_url(url: str) -> UpdateManifestSummary:
    payload = _read_public_release_payload(url)
    return _manifest_from_release_payload(payload, resolve_commit=True)


def _manifest_from_public_release_tag(source_repo: str, tag: str) -> UpdateManifestSummary:
    commit = _resolve_public_tag_commit(source_repo, tag)
    if not commit:
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "Public release tag evidence is unavailable.", "error_category": "public_tag_evidence_missing"})
    encoded_tag = urllib.parse.quote(tag, safe="")
    url = f"https://raw.githubusercontent.com/{source_repo}/{encoded_tag}/{PUBLIC_RELEASE_DESCRIPTOR_RELATIVE}"
    payload = _read_public_release_payload(url)
    payload["source_repo"] = source_repo
    payload["source_ref"] = tag
    payload["commit_sha"] = commit
    return _manifest_from_release_payload(payload, resolve_commit=False)


def _manifest_from_latest_public_release_tag(source_repo: str) -> UpdateManifestSummary:
    tag = _discover_latest_public_release_tag(source_repo)
    if not tag:
        raise UpdateCheckBlocked("provider_unavailable", {"summary": "No public semver release tags are available.", "error_category": "public_release_tags_missing"})
    return _manifest_from_public_release_tag(source_repo, tag)


def _available_release_source_path(manifest_path_for_test_only: str | Path | None = None) -> tuple[Path | str | None, str]:
    if manifest_path_for_test_only:
        return Path(manifest_path_for_test_only), "local_static_manifest"
    local = _manifest_path()
    if local and str(os.getenv("KMVMS_UPDATE_MANIFEST_FORCE_LOCAL", "")).strip().lower() in {"1", "true", "yes", "on"}:
        return local, "local_static_manifest"
    public = _public_release_descriptor_path()
    if public:
        return public, "public_github_release"
    public_url = _public_release_descriptor_url()
    if public_url and _public_provider_mode() in {"development_raw", "raw", "raw_main"}:
        return public_url, "public_github_release_development"
    public_repo = _public_release_repo()
    if public_repo and _public_provider_enabled():
        explicit_tag = _safe_field("tag", os.getenv("KMVMS_PUBLIC_RELEASE_TAG"), max_length=80)
        if explicit_tag and SAFE_TAG_RE.fullmatch(explicit_tag):
            return f"tag:{public_repo}:{explicit_tag}", "public_github_release"
        return f"latest-tag:{public_repo}", "public_github_release"
    return None, "not_configured"


def _system_row(db: Session) -> SystemSettings | None:
    try:
        return db.query(SystemSettings).first()
    except Exception:
        return None


def _schedule(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    row = _system_row(db)
    anchor = getattr(row, "created_at", None) or now
    return {
        "automatic_checks_enabled": False,
        "schedule_source": "manual_only_stage608",
        "cache_persistence": "in_memory_last_result_only",
        "last_update_check_at": _LAST_RESULT.get("checked_at") if _LAST_RESULT else None,
        "next_update_check_at": None,
        "automatic_due_now": False,
        "anchor_created_at": _iso(anchor) if isinstance(anchor, datetime) else None,
    }


def _cache_payload() -> dict[str, Any]:
    return {
        "has_last_result": _LAST_RESULT is not None,
        "last_result_status": _LAST_RESULT.get("status") if _LAST_RESULT else None,
        "has_last_successful_check": _LAST_SUCCESSFUL_RESULT is not None,
        "last_check_status": _LAST_RESULT.get("status") if _LAST_RESULT else None,
        "last_successful_check_status": _LAST_SUCCESSFUL_RESULT.get("status") if _LAST_SUCCESSFUL_RESULT else None,
        "last_successful_check_at": _LAST_SUCCESSFUL_RESULT.get("checked_at") if _LAST_SUCCESSFUL_RESULT else None,
    }


def _blocker(code: str, message: str, severity: str = "high") -> UpdateBlocker:
    return UpdateBlocker(code=code, message=message, severity=severity)


def _warning(code: str, message: str, severity: str = "info") -> UpdateMetadataWarning:
    return UpdateMetadataWarning(code=code, message=message, severity=severity)


def _compare(installed: UpdateInstalledState, latest: UpdateManifestSummary) -> tuple[str, list[UpdateBlocker], list[UpdateMetadataWarning]]:
    blockers: list[UpdateBlocker] = []
    warnings: list[UpdateMetadataWarning] = []
    if installed.status in {"installed_identity_drift", "identity_incomplete", "metadata_invalid"}:
        blockers.append(_blocker(installed.status, "Installed release identity is incomplete or does not match deployed source evidence."))
        return installed.status, blockers, warnings
    if installed.installed_commit and latest.commit and installed.installed_commit.lower() == latest.commit.lower():
        return "current", blockers, warnings
    if installed.git_head and latest.commit and installed.git_head.lower() == latest.commit.lower() and not installed.installed_commit:
        blockers.append(_blocker("identity_incomplete", "Deployed source matches the release but installed release identity is missing."))
        return "identity_incomplete", blockers, warnings
    if latest.minimum_current_version:
        current = _semver(installed.installed_version)
        minimum = _semver(latest.minimum_current_version)
        if current is None or minimum is None or current < minimum:
            blockers.append(_blocker("minimum_current_version_not_satisfied", "Installed version does not satisfy the release minimum_current_version."))
            return "blocked", blockers, warnings
    if latest.requires_backup:
        blockers.append(_blocker("requires_backup", "Release requires backup before any future apply."))
    if latest.requires_manual_action:
        blockers.append(_blocker("requires_manual_action", "Release requires manual operator action."))
    if latest.requires_migration:
        blockers.append(_blocker("requires_migration", "Release requires migration support outside Stage 6.0.8."))
    if blockers:
        return "blocked", blockers, warnings
    ordering = compare_versions(installed.installed_version, latest.version)["ordering"]
    if ordering == "same_version" and not latest.commit:
        warnings.append(_warning("commit_evidence_missing", "Version matches but commit evidence is unavailable."))
        return "current_or_unknown", blockers, warnings
    if ordering == "same_version" and latest.commit:
        if installed.installed_commit and installed.installed_commit.lower() != latest.commit.lower():
            blockers.append(_blocker("commit_mismatch", "Installed release commit does not match the same-version release commit."))
            return "blocked", blockers, warnings
        blockers.append(_blocker("identity_incomplete", "Same-version release includes commit evidence, but installed release commit evidence is missing."))
        return "identity_incomplete", blockers, warnings
    if not latest.commit:
        blockers.append(_blocker("trusted_commit_missing", "Available release does not include commit evidence required for apply."))
        if ordering == "newer_available":
            return "blocked", blockers, warnings
        return "unknown", blockers, warnings
    if ordering == "newer_available":
        return "update_available", blockers, warnings
    if ordering == "installed_newer_than_channel":
        blockers.append(_blocker("installed_newer_than_available", "Installed version is newer than available release."))
        return "installed_newer_than_available", blockers, warnings
    warnings.append(_warning("comparison_evidence_insufficient", "Installed/latest comparison evidence is insufficient."))
    return "unknown", blockers, warnings


def _result_payload(result: UpdateCheckResult) -> dict[str, Any]:
    payload = _asdict(result)
    installed = payload["installed"]
    latest = payload["latest"]
    available_release = None if latest is None else {
        "version": latest["version"],
        "title": latest["title"],
        "summary": latest["summary"],
        "changelog": latest["breaking_changes"],
        "published_at": latest["published_at"],
        "tag": latest["source_ref"] or latest["git_ref"],
        "commit_sha": latest["commit"],
        "commit_short": latest["commit"][:12] if latest["commit"] else None,
        "provider": payload["manifest_source_status"],
        "requires_backup": latest["requires_backup"],
        "requires_manual_action": latest["requires_manual_action"],
        "requires_migration": latest["requires_migration"],
    }
    installed_release = {
        "version": installed["installed_version"],
        "title": installed["installed_title"],
        "summary": installed["installed_summary"],
        "commit_sha": installed["installed_commit"],
        "commit_short": installed["installed_commit"][:12] if installed["installed_commit"] else None,
        "source_kind": installed["source_kind"],
        "source_repo": installed["repo"],
        "source_ref": installed["ref"],
        "release_channel": installed["release_channel"] or installed["channel"],
        "installed_at": installed["installed_at"],
        "installed_by": installed["installed_by"],
        "metadata_status": installed["release_metadata_status"] or installed["identity_validity"],
        "identity_validity": installed["identity_validity"],
        "metadata_source": installed["metadata_source"],
    }
    evidence = {
        "git_head": installed["git_head"],
        "git_head_short": installed["git_head"][:12] if installed["git_head"] else None,
        "legacy_source_commit": installed["legacy_source_commit"],
        "legacy_update_commit": installed["legacy_update_commit"],
            "drift_detected": installed["status"] == "installed_identity_drift",
    }
    payload.update(
        {
            "report_version": UPDATE_REPORT_VERSION,
            "installed_release": installed_release,
            "available_release": available_release,
            "comparison": {
                "status": payload["status"],
                "can_apply_from_ui": False,
                "reason_code": payload["blockers"][0]["code"] if payload["blockers"] else payload["status"],
            },
            "evidence": evidence,
            "installed_build": {
                "status": installed["status"],
                "app_version": installed["installed_version"],
                "git_commit": installed["installed_commit"],
                "install_source": installed["source_kind"],
                "source_channel_id": installed["channel"],
                "metadata_source": installed["metadata_validity"],
            },
            "latest_release": None
            if latest is None
            else {
                "latest_version": latest["version"],
                "version": latest["version"],
                "release_id": latest["commit"] or latest["version"],
                "build_id": latest["commit"],
                "git_ref": latest["git_ref"],
                "commit": latest["commit"],
                "release_notes_summary": latest["summary"],
                "requires_backup": latest["requires_backup"],
                "requires_manual_action": latest["requires_manual_action"],
                "requires_migration": latest["requires_migration"],
            },
            "source_channel": {
                "status": payload["manifest_source_status"],
                "source_channel_id": installed["channel"],
                "trusted_source_type": payload["manifest_source_status"],
                "arbitrary_url_supported": False,
                "remote_check_status": "public_github_release_metadata" if payload["manifest_source_status"] == "public_github_release" else "local_or_not_configured",
            },
            "classification": {
                "availability": payload["status"],
                "classification": "blocked" if payload["blockers"] else payload["status"],
                "ordering": compare_versions(installed["installed_version"], latest["version"] if latest else None)["ordering"] if latest else "unknown_ordering",
                "severity": "unknown",
            },
            "preflight": {
                "status": "blocked" if payload["blockers"] else "ok",
                "blockers": [item["code"] for item in payload["blockers"]],
                "warnings": [item["code"] for item in payload["warnings"]],
                "side_effects": {
                    "update_applied": False,
                    "artifact_downloaded": False,
                    "containers_restarted": False,
                    "migration_executed": False,
                    "backup_created": False,
                    "restore_executed": False,
                },
            },
            "side_effects": {
                "update_applied": False,
                "artifact_downloaded": False,
                "containers_restarted": False,
                "migration_executed": False,
                "backup_created": False,
                "restore_executed": False,
            },
            "raw_manifest_exposed": False,
            "next_recommended_action": "apply_update_when_confirmed" if payload["status"] == "update_available" else "no_update_apply_action",
        }
    )
    helper_enabled = str(os.getenv("KMVMS_UPDATE_HELPER_ENABLED") or os.getenv("KM_VMS_UPDATE_HELPER_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    payload["can_apply_from_ui"] = bool(payload["status"] == "update_available" and not payload["blockers"] and helper_enabled and latest and latest.get("commit"))
    payload["comparison"]["can_apply_from_ui"] = payload["can_apply_from_ui"]
    return payload


def build_update_status(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    installed = read_installed_update_state()
    source_path, source_status = _available_release_source_path()
    manifest_configured = source_path is not None
    warnings = list(installed.warnings)
    if not manifest_configured:
        warnings.append(_warning("no_release_published", "No public release metadata is available."))
    status = "not_configured" if not manifest_configured else installed.status
    result = UpdateCheckResult(
        status=status,
        installed=installed,
        latest=None,
        blockers=[],
        warnings=warnings,
        checked_at=_iso(now),
        manifest_source_status=source_status if manifest_configured else "not_configured",
    )
    payload = _result_payload(result)
    payload["schedule"] = _schedule(db, now=now)
    payload["cache"] = _cache_payload()
    payload["has_last_successful_check"] = _LAST_SUCCESSFUL_RESULT is not None
    payload["last_check_status"] = _LAST_RESULT.get("status") if _LAST_RESULT else None
    payload["last_update_check"] = _LAST_RESULT
    return payload


def run_update_check(db: Session, *, manual: bool = False, manifest_path_for_test_only: str | Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    global _LAST_RESULT, _LAST_SUCCESSFUL_RESULT, _CHECK_IN_PROGRESS
    now = now or _utcnow()
    if _CHECK_IN_PROGRESS:
        raise UpdateCheckBlocked("update_check_already_running", {"summary": "An update check is already in progress."})
    _CHECK_IN_PROGRESS = True
    try:
        installed = read_installed_update_state()
        source_path, source_status = _available_release_source_path(manifest_path_for_test_only)
        if not source_path:
            result = UpdateCheckResult(
                status="not_configured",
                installed=installed,
                latest=None,
                blockers=[],
                warnings=[*installed.warnings, _warning("no_release_published", "No public release metadata is available.")],
                checked_at=_iso(now),
                manifest_source_status="not_configured",
            )
            payload = _result_payload(result)
            payload["schedule"] = _schedule(db, now=now)
            payload["has_last_successful_check"] = True
            payload["last_check_status"] = payload.get("status")
            _LAST_RESULT = payload
            _LAST_SUCCESSFUL_RESULT = payload
            payload["cache"] = _cache_payload()
            return payload
        if source_status == "public_github_release" and isinstance(source_path, str) and source_path.startswith("latest-tag:"):
            latest = _manifest_from_latest_public_release_tag(source_path.split(":", 1)[1])
        elif source_status == "public_github_release" and isinstance(source_path, str) and source_path.startswith("tag:"):
            _, repo, tag = source_path.split(":", 2)
            latest = _manifest_from_public_release_tag(repo, tag)
        elif source_status == "public_github_release_development" and isinstance(source_path, str):
            latest = _manifest_from_public_release_url(source_path)
        elif source_status == "public_github_release":
            latest = _manifest_from_release_descriptor(source_path)
        else:
            latest = read_trusted_local_manifest(source_path)
        status, blockers, compare_warnings = _compare(installed, latest)
        result = UpdateCheckResult(
            status=status,
            installed=installed,
            latest=latest,
            blockers=blockers,
            warnings=[*installed.warnings, *compare_warnings],
            checked_at=_iso(now),
            manifest_source_status=source_status,
        )
        payload = _result_payload(result)
        payload["schedule"] = _schedule(db, now=now)
        payload["has_last_successful_check"] = True
        payload["last_check_status"] = payload.get("status")
        payload["last_success_at"] = _iso(now)
        _LAST_RESULT = payload
        _LAST_SUCCESSFUL_RESULT = payload
        payload["cache"] = _cache_payload()
        return payload
    except UpdateCheckBlocked as exc:
        result = UpdateCheckResult(
            status=exc.status if exc.status in {"not_configured", "blocked"} else "check_failed",
            installed=installed,
            latest=None,
            blockers=[],
            warnings=installed.warnings,
            checked_at=_iso(now),
            manifest_source_status="check_failed",
        )
        payload = _result_payload(result)
        payload["errors"] = [{"code": exc.status, "summary": _sanitize_text(exc.diagnostics.get("summary"), max_length=200), "error_category": exc.diagnostics.get("error_category")}]
        payload["schedule"] = _schedule(db, now=now)
        payload["has_last_successful_check"] = _LAST_SUCCESSFUL_RESULT is not None
        payload["last_check_status"] = payload.get("status")
        _LAST_RESULT = payload
        payload["cache"] = _cache_payload()
        return payload
    finally:
        _CHECK_IN_PROGRESS = False


def run_startup_due_check(db: Session) -> dict[str, Any]:
    return build_update_status(db)


def reset_update_check_cache_for_tests() -> None:
    global _LAST_RESULT, _LAST_SUCCESSFUL_RESULT, _CHECK_IN_PROGRESS
    _LAST_RESULT = None
    _LAST_SUCCESSFUL_RESULT = None
    _CHECK_IN_PROGRESS = False
