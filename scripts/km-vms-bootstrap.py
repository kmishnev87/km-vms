#!/usr/bin/env python3
"""Stable KM VMS bootstrap and installation-level lifecycle authority.

The materialized copy of this file lives outside release slots.  It resolves
only validated immutable slots, delegates non-terminal activation to the
existing bridge, and owns the narrow Docker restart-policy contract.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


BOOTSTRAP_SCHEMA_VERSION = 1
BOOTSTRAP_RELATIVE = Path("data/update-runtime/bootstrap")
BUNDLES_NAME = "bundles"
CURRENT_NAME = "current"
MANIFEST_NAME = "bootstrap-manifest.json"
CHECKSUMS_NAME = "bootstrap-files.sha256"
LIFECYCLE_NAME = "docker-compose.lifecycle.yml"
DERIVED_COMPOSE_RELATIVE = Path("data/update-runtime/derived-compose")
SOURCE_FILES = (
    "km-vms-bootstrap.py",
    "km-vms-bootstrap-dispatch.sh",
    "km-vms-release-slots.py",
    "km-vms-compose-common.sh",
    "km-vms-restart.sh",
    "km-vms-update-launcher.sh",
    "km-vms-storage-apply.sh",
    "km-vms-setup-activation-helper.sh",
)
PERSISTENT_SERVICES = (
    "postgres",
    "redis",
    "update-status-reader",
    "update-retry-admission",
    "api",
    "setup-helper",
    "update-helper",
    "recorder",
    "web",
    "nginx",
)
ONE_SHOT_SERVICES = (
    "update-helper-bootstrap",
    "schema-update",
    "restore-executor",
)
WRITER_SERVICES = frozenset({"api", "recorder"})
NONTERMINAL_ACTIVATION_PHASES = frozenset(
    {
        "target_prepared",
        "quiescing",
        "schema_preparing",
        "activating",
        "verifying_target",
        "committing_target",
        "rolling_back",
    }
)
FENCED_ACTIVATION_PHASES = frozenset(
    {
        "quiescing",
        "schema_preparing",
        "activating",
        "verifying_target",
        "committing_target",
        "rolling_back",
    }
)
FENCED_RESTORE_PHASES = frozenset(
    {"writers_paused", "restore_running", "services_starting", "post_restore_check"}
)
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
CONTAINER_RE = re.compile(r"^[0-9a-f]{12,64}$")
RESTORE_OPERATION_RE = re.compile(r"^restore-[0-9a-f]{32}$")
RESTORE_REQUEST_SCHEMA = "stage13.7.8.current-restore-request.v1"


class BootstrapError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BootstrapError(
                "bootstrap_file_invalid",
                "Bootstrap evidence contains an unsafe file.",
            )
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(
            "bootstrap_file_unavailable",
            "Bootstrap evidence is unavailable.",
        ) from exc
    return digest.hexdigest()


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise BootstrapError(
            "bootstrap_publish_failed",
            "Bootstrap evidence could not be published atomically.",
        ) from exc


def read_json(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BootstrapError("bootstrap_evidence_invalid", "Evidence path is unsafe.")
        if info.st_size <= 0 or info.st_size > 1024 * 1024:
            raise BootstrapError("bootstrap_evidence_invalid", "Evidence size is invalid.")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise BootstrapError("bootstrap_evidence_missing", "Required evidence is missing.")
    except BootstrapError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("bootstrap_evidence_invalid", "Evidence is invalid.") from exc
    if type(value) is not dict:
        raise BootstrapError("bootstrap_evidence_invalid", "Evidence must be an object.")
    return value


def require_app_dir(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(character in str(path) for character in "\x00\r\n"):
        raise BootstrapError("app_dir_invalid", "Stable APP_DIR must be absolute.")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BootstrapError("app_dir_invalid", "Stable APP_DIR is unavailable.") from exc
    if not resolved.is_dir() or not (resolved / "data").is_dir():
        raise BootstrapError("app_dir_invalid", "Stable APP_DIR is incomplete.")
    return resolved


def source_scripts_dir(source_dir: Path) -> Path:
    scripts = source_dir / "scripts"
    if scripts.is_symlink() or not scripts.is_dir():
        raise BootstrapError("bootstrap_source_invalid", "Bootstrap source is incomplete.")
    for name in SOURCE_FILES:
        candidate = scripts / name
        if candidate.is_symlink() or not candidate.is_file():
            raise BootstrapError("bootstrap_source_invalid", "Bootstrap source is incomplete.")
    return scripts


def render_lifecycle_override() -> bytes:
    commands = {
        "update-helper-bootstrap": (
            "        exec python3 -B\n"
            "        \"$${KM_VMS_BOOTSTRAP_APP_DIR}/data/update-runtime/bootstrap/current/km-vms-bootstrap.py\"\n"
            "        run-role update-helper-bootstrap\n"
            "        --app-dir \"$${KM_VMS_BOOTSTRAP_APP_DIR}\"\n"
            "        --project-name \"$${KM_VMS_BOOTSTRAP_PROJECT_NAME}\""
        ),
        "setup-helper": (
            "        exec sh\n"
            "        \"$${KM_VMS_SETUP_APP_DIR}/data/update-runtime/bootstrap/current/km-vms-bootstrap-dispatch.sh\"\n"
            "        setup-helper \"$${KM_VMS_SETUP_APP_DIR}\""
        ),
        "update-helper": (
            "        exec python3 -B\n"
            "        /host-app/data/update-runtime/bootstrap/current/km-vms-bootstrap.py\n"
            "        run-role update-helper\n"
            "        --app-dir /host-app\n"
            "        --project-name \"$${KM_VMS_PROJECT_NAME}\""
        ),
    }
    lines = ["# Generated by KM VMS stable bootstrap. Do not edit.", "services:"]
    for service in (*PERSISTENT_SERVICES, *ONE_SHOT_SERVICES):
        restart = "always" if service in PERSISTENT_SERVICES else '"no"'
        lines.extend((f"  {service}:", f"    restart: {restart}"))
        command = commands.get(service)
        if command is not None:
            lines.extend(("    command:", "      - sh", "      - -c", "      - >-", command))
    rendered = "\n".join(lines) + "\n"
    return rendered.encode("utf-8")


def _bundle_payload(bundle_dir: Path) -> dict[str, Any]:
    manifest = read_json(bundle_dir / MANIFEST_NAME)
    assert manifest is not None
    expected = {
        "schema_version",
        "document_type",
        "bundle_id",
        "created_at",
        "files",
        "lifecycle_override_sha256",
    }
    if (
        set(manifest) != expected
        or manifest.get("schema_version") != BOOTSTRAP_SCHEMA_VERSION
        or manifest.get("document_type") != "km_vms_stable_bootstrap"
        or type(manifest.get("bundle_id")) is not str
        or not DIGEST_RE.fullmatch(manifest["bundle_id"])
        or type(manifest.get("created_at")) is not str
        or not manifest["created_at"]
        or type(manifest.get("files")) is not dict
        or set(manifest["files"]) != set(SOURCE_FILES)
        or type(manifest.get("lifecycle_override_sha256")) is not str
        or not DIGEST_RE.fullmatch(manifest["lifecycle_override_sha256"])
    ):
        raise BootstrapError("bootstrap_manifest_invalid", "Bootstrap manifest is invalid.")
    for name, digest in manifest["files"].items():
        if type(digest) is not str or not DIGEST_RE.fullmatch(digest):
            raise BootstrapError("bootstrap_manifest_invalid", "Bootstrap file digest is invalid.")
        if sha256_file(bundle_dir / name) != digest:
            raise BootstrapError("bootstrap_digest_mismatch", "Bootstrap executable changed.")
    lifecycle = bundle_dir / LIFECYCLE_NAME
    if sha256_file(lifecycle) != manifest["lifecycle_override_sha256"]:
        raise BootstrapError("bootstrap_digest_mismatch", "Lifecycle override changed.")
    identity = {
        "schema_version": manifest["schema_version"],
        "files": manifest["files"],
        "lifecycle_override_sha256": manifest["lifecycle_override_sha256"],
    }
    if hashlib.sha256(canonical_json(identity)).hexdigest() != manifest["bundle_id"]:
        raise BootstrapError("bootstrap_manifest_invalid", "Bootstrap identity is contradictory.")
    if bundle_dir.name != manifest["bundle_id"]:
        raise BootstrapError("bootstrap_manifest_invalid", "Bootstrap directory identity differs.")
    return manifest


def validate_current_bundle(app_dir: Path) -> tuple[Path, dict[str, Any]]:
    app_dir = require_app_dir(app_dir)
    root = app_dir / BOOTSTRAP_RELATIVE
    pointer = root / CURRENT_NAME
    try:
        info = pointer.lstat()
    except FileNotFoundError as exc:
        raise BootstrapError("bootstrap_pointer_missing", "Stable bootstrap pointer is missing.") from exc
    if not stat.S_ISLNK(info.st_mode):
        raise BootstrapError("bootstrap_pointer_invalid", "Stable bootstrap pointer is unsafe.")
    target = os.readlink(pointer)
    parts = Path(target).parts
    if len(parts) != 2 or parts[0] != BUNDLES_NAME or not DIGEST_RE.fullmatch(parts[1]):
        raise BootstrapError("bootstrap_pointer_invalid", "Stable bootstrap pointer target is invalid.")
    bundle = root / BUNDLES_NAME / parts[1]
    try:
        resolved = bundle.resolve(strict=True)
    except OSError as exc:
        raise BootstrapError("bootstrap_pointer_invalid", "Stable bootstrap bundle is unavailable.") from exc
    expected = (root / BUNDLES_NAME / parts[1]).resolve(strict=True)
    if resolved != expected or bundle.is_symlink() or not bundle.is_dir():
        raise BootstrapError("bootstrap_pointer_invalid", "Stable bootstrap escaped its bounded root.")
    return resolved, _bundle_payload(resolved)


def install_bundle(app_dir: Path, source_dir: Path) -> dict[str, Any]:
    app_dir = require_app_dir(app_dir)
    source_dir = source_dir.resolve(strict=True)
    scripts = source_scripts_dir(source_dir)
    root = app_dir / BOOTSTRAP_RELATIVE
    bundles = root / BUNDLES_NAME
    if root.is_symlink() or bundles.is_symlink():
        raise BootstrapError("bootstrap_path_invalid", "Bootstrap layout is unsafe.")
    bundles.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = root / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        file_digests: dict[str, str] = {}
        for name in SOURCE_FILES:
            destination = staging / name
            shutil.copy2(scripts / name, destination)
            destination.chmod(0o555)
            file_digests[name] = sha256_file(destination)
        lifecycle = staging / LIFECYCLE_NAME
        lifecycle.write_bytes(render_lifecycle_override())
        lifecycle.chmod(0o444)
        lifecycle_digest = sha256_file(lifecycle)
        identity = {
            "schema_version": BOOTSTRAP_SCHEMA_VERSION,
            "files": file_digests,
            "lifecycle_override_sha256": lifecycle_digest,
        }
        bundle_id = hashlib.sha256(canonical_json(identity)).hexdigest()
        checksums = "".join(
            f"{digest}  {name}\n" for name, digest in sorted(file_digests.items())
        ) + f"{lifecycle_digest}  {LIFECYCLE_NAME}\n"
        (staging / CHECKSUMS_NAME).write_text(checksums, encoding="ascii")
        (staging / CHECKSUMS_NAME).chmod(0o444)
        manifest = {
            "schema_version": BOOTSTRAP_SCHEMA_VERSION,
            "document_type": "km_vms_stable_bootstrap",
            "bundle_id": bundle_id,
            "created_at": utc_now(),
            "files": file_digests,
            "lifecycle_override_sha256": lifecycle_digest,
        }
        atomic_write(staging / MANIFEST_NAME, canonical_json(manifest) + b"\n", 0o444)
        final = bundles / bundle_id
        if final.exists():
            if final.is_symlink() or not final.is_dir():
                raise BootstrapError("bootstrap_bundle_conflict", "Existing bootstrap bundle is unsafe.")
            _bundle_payload(final)
            shutil.rmtree(staging)
        else:
            os.replace(staging, final)
        replacement = root / f".{CURRENT_NAME}.{uuid.uuid4().hex}.next"
        os.symlink(f"{BUNDLES_NAME}/{bundle_id}", replacement)
        os.replace(replacement, root / CURRENT_NAME)
        selected, selected_manifest = validate_current_bundle(app_dir)
        return {
            "schema_version": BOOTSTRAP_SCHEMA_VERSION,
            "bundle_id": bundle_id,
            "bundle_path": str(selected),
            "lifecycle_override": str(selected / LIFECYCLE_NAME),
            "manifest": selected_manifest,
        }
    except Exception:
        if staging.exists() and staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise


def lifecycle_override_path(app_dir: Path) -> Path:
    bundle, _manifest = validate_current_bundle(app_dir)
    return bundle / LIFECYCLE_NAME


def load_slot_engine(bundle_dir: Path | None = None) -> Any:
    script = (bundle_dir or Path(__file__).resolve().parent) / "km-vms-release-slots.py"
    if script.is_symlink() or not script.is_file():
        raise BootstrapError("slot_engine_missing", "Canonical slot engine is unavailable.")
    spec = importlib.util.spec_from_file_location("km_vms_bootstrap_slots", script)
    if spec is None or spec.loader is None:
        raise BootstrapError("slot_engine_missing", "Canonical slot engine cannot be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_project_name(app_dir: Path, supplied: str | None = None) -> str:
    value = str(supplied or "").strip()
    if not value:
        env_file = app_dir / ".env"
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("COMPOSE_PROJECT_NAME="):
                    value = line.split("=", 1)[1].strip()
                    break
        except OSError as exc:
            raise BootstrapError("project_identity_missing", "Compose project identity is unavailable.") from exc
    if not PROJECT_RE.fullmatch(value):
        raise BootstrapError("project_identity_invalid", "Compose project identity is invalid.")
    return value


def _binding_matches(engine: Any, app_dir: Path, binding: dict[str, Any]) -> bool:
    try:
        normalized = engine.validate_activation_slot_binding(binding)
        observed = engine.build_activation_slot_binding(
            app_dir,
            normalized["slot_id"],
            compose_plan_sha256=normalized["compose_plan_sha256"],
            archive_override_sha256=normalized["archive_override_sha256"],
        )
    except Exception:
        return False
    return observed == normalized


def resolve_authority(
    app_dir: Path,
    *,
    project_name: str | None = None,
    repair: bool = False,
) -> tuple[str, Path, dict[str, Any]]:
    app_dir = require_app_dir(app_dir)
    bundle, _bootstrap = validate_current_bundle(app_dir)
    engine = load_slot_engine(bundle)
    try:
        active = engine.read_active_slot(app_dir)
    except Exception as exc:
        active = None
        active_error = exc
    else:
        active_error = None
    try:
        journal = engine.read_activation_journal(app_dir, missing_ok=True)
    except Exception as exc:
        raise BootstrapError(
            getattr(exc, "code", "activation_journal_invalid"),
            "Activation recovery evidence is invalid.",
        ) from exc
    if journal is None:
        if active is not None:
            manifest = engine.validate_slot(
                app_dir / "data/update-runtime/slots" / active[0],
                expected_slot_id=active[0],
            )
            return active[0], active[1], manifest
        code = getattr(active_error, "code", "active_pointer_missing")
        raise BootstrapError(code, "Canonical active release is unavailable.")
    phase = journal["phase"]
    if phase == "blocked":
        raise BootstrapError("activation_blocked", "Activation is blocked and cannot be guessed.")
    if phase in {"completed", "failed_rolled_back"}:
        binding = journal["target"] if phase == "completed" else journal["previous"]
        if not _binding_matches(engine, app_dir, binding):
            raise BootstrapError("activation_binding_invalid", "Terminal slot binding is invalid.")
        if active is not None and active[0] == binding["slot_id"]:
            return active[0], active[1], engine.validate_slot(
                app_dir / "data/update-runtime/slots" / active[0],
                expected_slot_id=active[0],
            )
        if not repair:
            raise BootstrapError(
                "active_pointer_conflict",
                "Canonical active pointer contradicts terminal activation evidence.",
            )
        try:
            engine.atomic_switch_pointer(app_dir, binding["slot_id"])
            engine.publish_installed_slot_projection(
                app_dir,
                binding=binding,
            )
        except Exception as exc:
            raise BootstrapError(
                getattr(exc, "code", "active_pointer_repair_failed"),
                "Canonical active pointer could not be repaired.",
            ) from exc
        active = engine.read_active_slot(app_dir)
        assert active is not None
        return active[0], active[1], engine.validate_slot(
            app_dir / "data/update-runtime/slots" / active[0],
            expected_slot_id=active[0],
        )
    if phase not in NONTERMINAL_ACTIVATION_PHASES:
        raise BootstrapError("activation_phase_invalid", "Activation phase is unsupported.")
    if not repair:
        raise BootstrapError(
            "activation_in_progress",
            "Canonical activation recovery is still in progress.",
        )
    target = journal["target"]
    if not _binding_matches(engine, app_dir, target):
        raise BootstrapError("activation_binding_invalid", "Recovery target binding is invalid.")
    target_source = app_dir / "data/update-runtime/slots" / target["slot_id"] / "source"
    bridge = target_source / "scripts/km-vms-update-helper-bridge.py"
    if bridge.is_symlink() or not bridge.is_file():
        raise BootstrapError("activation_recovery_missing", "Target recovery entry is unavailable.")
    project = read_project_name(app_dir, project_name)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(bridge),
            "resume-activation",
            "--app-dir",
            str(app_dir),
            "--project-name",
            project,
            "--request-id",
            journal["request_id"],
            "--terminal",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=1800,
        check=False,
    )
    if result.returncode != 0:
        raise BootstrapError("activation_recovery_failed", "Existing activation convergence failed.")
    return resolve_authority(app_dir, project_name=project, repair=True)


def materialize_slot_image_override(
    app_dir: Path,
    project_name: str,
) -> tuple[Path, str]:
    """Publish a derived Compose image layer from validated slot evidence."""

    app_dir = require_app_dir(app_dir)
    project_name = read_project_name(app_dir, project_name)
    slot_id, _source, manifest = resolve_authority(
        app_dir,
        project_name=project_name,
        repair=True,
    )
    evidence = manifest.get("image_evidence", {}).get("services")
    compose_services = set(
        manifest.get("compose_evidence", {}).get("services") or []
    )
    if type(evidence) is not dict or not evidence or not compose_services:
        raise BootstrapError(
            "slot_image_evidence_missing",
            "Selected slot image evidence is unavailable.",
        )
    selected: dict[str, str] = {}
    for service, item in sorted(evidence.items()):
        if service not in compose_services:
            continue
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", str(service))
            or type(item) is not dict
        ):
            raise BootstrapError(
                "slot_image_evidence_invalid",
                "Selected slot image evidence is invalid.",
            )
        expected = str(item.get("image_id") or "").lower()
        image_ref = str(item.get("immutable_image_ref") or "")
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", expected)
            or not image_ref
            or len(image_ref) > 240
            or any(character.isspace() or character in "\x00\r\n" for character in image_ref)
        ):
            raise BootstrapError(
                "slot_image_evidence_invalid",
                "Selected slot image evidence is invalid.",
            )
        observed = _docker(
            ["image", "inspect", "--format", "{{.Id}}", image_ref],
            timeout=30,
        ).stdout.strip().lower()
        if observed != expected:
            raise BootstrapError(
                "slot_image_evidence_mismatch",
                "Selected slot immutable image is unavailable or changed.",
            )
        selected[str(service)] = image_ref
    if not selected:
        raise BootstrapError(
            "slot_image_evidence_missing",
            "Selected slot has no usable immutable images.",
        )
    output_root = app_dir / DERIVED_COMPOSE_RELATIVE
    if output_root.is_symlink():
        raise BootstrapError(
            "slot_image_override_invalid",
            "Derived Compose directory is unsafe.",
        )
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_root.chmod(0o700)
    path = output_root / f"{slot_id}-images.yml"
    lines = [
        "# Derived from validated immutable slot image evidence.",
        "services:",
    ]
    for service, image_ref in selected.items():
        lines.extend(
            (
                f"  {service}:",
                f"    image: {json.dumps(image_ref, ensure_ascii=True)}",
            )
        )
    content = ("\n".join(lines) + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise BootstrapError(
                "slot_image_override_invalid",
                "Derived Compose image override is unsafe.",
            )
        if path.read_bytes() != content:
            raise BootstrapError(
                "slot_image_override_conflict",
                "Derived Compose image override contradicts slot evidence.",
            )
    else:
        atomic_write(path, content, 0o400)
    return path, slot_id


def _docker(args: Sequence[str], *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["docker", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError("docker_unavailable", "Docker lifecycle operation is unavailable.") from exc
    if check and result.returncode != 0:
        raise BootstrapError("docker_operation_failed", "Docker lifecycle operation failed.")
    return result


def _service_container_ids(project_name: str, service: str) -> list[str]:
    result = _docker(
        [
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ]
    )
    ids = [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]
    if any(not CONTAINER_RE.fullmatch(item) for item in ids) or len(ids) > 1:
        raise BootstrapError("container_identity_ambiguous", "Compose service identity is ambiguous.")
    return ids


def _restart_policy(container_id: str) -> str:
    value = _docker(
        ["inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", container_id]
    ).stdout.strip()
    if value not in {"no", "always", "unless-stopped", "on-failure"}:
        raise BootstrapError("restart_policy_invalid", "Container restart policy is invalid.")
    return value


def writer_isolation_active(app_dir: Path) -> bool:
    app_dir = require_app_dir(app_dir)
    bundle, _manifest = validate_current_bundle(app_dir)
    engine = load_slot_engine(bundle)
    try:
        journal = engine.read_activation_journal(app_dir, missing_ok=True)
    except Exception as exc:
        raise BootstrapError(
            getattr(exc, "code", "activation_journal_invalid"),
            "Activation isolation evidence is invalid.",
        ) from exc
    if journal is not None:
        schema = journal["schema"]
        if (
            schema["migration_required"]
            and journal["phase"] in FENCED_ACTIVATION_PHASES
        ):
            return True
        if (
            journal["phase"] == "blocked"
            and schema["migration_required"]
            and schema["migration_invoked"]
        ):
            # A terminal block after migration/isolation started is not a
            # proven safe DB outcome.  Keep writers fenced for intervention.
            return True
    restore_request = read_json(
        app_dir / "data/restore-control/restore-request.json",
        missing_ok=True,
    )
    restore_journal = read_json(
        app_dir / "data/restore-control/restore-journal.json",
        missing_ok=True,
    )
    if restore_request is None:
        return False
    expected_request_keys = {
        "schema",
        "operation_id",
        "submission_id",
        "intent",
        "requested_at",
        "updated_at",
        "requested_by",
        "artifact",
        "confirmed",
        "confirmation_phrase",
        "state",
        "claimed_at",
        "terminal",
        "video_archive_scope",
        "migration_auto_apply",
    }
    operation_id = restore_request.get("operation_id")
    state = restore_request.get("state")
    if (
        set(restore_request) != expected_request_keys
        or restore_request.get("schema") != RESTORE_REQUEST_SCHEMA
        or restore_request.get("intent") != "restore_current_database"
        or type(operation_id) is not str
        or not RESTORE_OPERATION_RE.fullmatch(operation_id)
        or state not in {"admitted", "claimed", "terminal"}
        or restore_request.get("confirmed") is not True
        or restore_request.get("confirmation_phrase") != "RESTORE KM VMS"
        or restore_request.get("video_archive_scope") != "excluded"
        or restore_request.get("migration_auto_apply") is not False
    ):
        raise BootstrapError(
            "restore_isolation_evidence_invalid",
            "Restore isolation request evidence is invalid.",
        )
    if state == "admitted":
        return False
    if state == "terminal":
        terminal = restore_request.get("terminal")
        if type(terminal) is not dict or terminal.get("status") not in {
            "completed",
            "blocked",
            "failed_rolled_back",
            "failed_recovery_required",
        }:
            raise BootstrapError(
                "restore_isolation_evidence_invalid",
                "Restore terminal evidence is invalid.",
            )
        return terminal["status"] == "failed_recovery_required"
    if restore_journal is None:
        return False
    expected_journal_keys = {
        "schema_version",
        "operation_id",
        "submission_id",
        "phase",
        "recorded_at",
        "source_artifact_id",
        "pre_restore_backup_id",
        "destructive_started",
        "terminal_result",
        "reason_code",
        "video_archive_modified",
    }
    if (
        set(restore_journal) != expected_journal_keys
        or restore_journal.get("schema_version") != 1
        or restore_journal.get("operation_id") != operation_id
        or restore_journal.get("submission_id")
        != restore_request.get("submission_id")
        or restore_journal.get("phase")
        not in {
            "preflight",
            "pre_restore_backup",
            *FENCED_RESTORE_PHASES,
        }
        or type(restore_journal.get("destructive_started")) is not bool
        or restore_journal.get("terminal_result") is not None
        or restore_journal.get("video_archive_modified") is not False
    ):
        raise BootstrapError(
            "restore_isolation_evidence_invalid",
            "Restore isolation journal evidence is invalid.",
        )
    return restore_journal["phase"] in FENCED_RESTORE_PHASES


def reconcile_restart_policies(
    app_dir: Path,
    project_name: str,
    *,
    writer_fenced: bool | None = None,
) -> dict[str, str]:
    app_dir = require_app_dir(app_dir)
    validate_current_bundle(app_dir)
    project_name = read_project_name(app_dir, project_name)
    fenced = writer_isolation_active(app_dir) if writer_fenced is None else writer_fenced
    expected: dict[str, str] = {}
    for service in PERSISTENT_SERVICES:
        expected[service] = "no" if fenced and service in WRITER_SERVICES else "always"
    expected.update({service: "no" for service in ONE_SHOT_SERVICES})
    observed: dict[str, str] = {}
    for service, desired in expected.items():
        ids = _service_container_ids(project_name, service)
        if not ids:
            continue
        container_id = ids[0]
        current = _restart_policy(container_id)
        if current != desired:
            _docker(["update", f"--restart={desired}", container_id], timeout=120)
        verified = _restart_policy(container_id)
        if verified != desired:
            raise BootstrapError("restart_policy_verify_failed", "Restart policy did not converge.")
        observed[service] = verified
    return observed


def set_writer_fence(app_dir: Path, project_name: str, *, enabled: bool) -> dict[str, str]:
    observed = reconcile_restart_policies(
        app_dir,
        project_name,
        writer_fenced=enabled,
    )
    for service in WRITER_SERVICES:
        if service in observed and observed[service] != ("no" if enabled else "always"):
            raise BootstrapError("writer_fence_verify_failed", "Writer policy fence did not converge.")
    return observed


def ensure_exact_helper(
    app_dir: Path,
    project_name: str,
    slot_id: str,
    source_dir: Path,
    manifest: dict[str, Any],
) -> None:
    helper = manifest.get("image_evidence", {}).get("services", {}).get("update-helper")
    if type(helper) is not dict:
        raise BootstrapError("helper_image_evidence_missing", "Selected helper image evidence is missing.")
    expected_id = str(helper.get("image_id") or "").lower()
    helper_ref = str(helper.get("immutable_image_ref") or "")
    ids = _service_container_ids(project_name, "update-helper")
    if ids:
        observed = _docker(["inspect", "--format", "{{.Image}}", ids[0]]).stdout.strip().lower()
        if observed == expected_id:
            return
    bridge_path = source_dir / "scripts/km-vms-update-helper-bridge.py"
    if bridge_path.is_symlink() or not bridge_path.is_file():
        raise BootstrapError("helper_recovery_missing", "Selected helper recovery entry is unavailable.")
    spec = importlib.util.spec_from_file_location("km_vms_bootstrap_bridge", bridge_path)
    if spec is None or spec.loader is None:
        raise BootstrapError("helper_recovery_missing", "Selected helper recovery cannot be loaded.")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    bundle, _bootstrap = validate_current_bundle(app_dir)
    engine = load_slot_engine(bundle)
    try:
        bridge.recreate_and_verify_helper(
            app_dir=app_dir,
            project_name=project_name,
            helper_image=helper_ref,
            expected_image_id=expected_id,
            target_slot=slot_id,
            engine=engine,
        )
    except Exception as exc:
        raise BootstrapError("helper_image_convergence_failed", "Exact update-helper image did not converge.") from exc


def normalize_update_helper_compose_environment() -> None:
    """Drop host-only or unavailable Compose overrides before legacy helper exec.

    Older release slots may pass the NAS host's absolute Compose path, or the
    historical ``docker-compose`` command, into the helper container.  Neither
    is necessarily present there.  Removing only unavailable overrides lets
    the selected helper's existing Compose discovery use the bundled
    ``docker compose`` plugin without rewriting the immutable slot.
    """

    for name in (
        "KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE",
        "KM_VMS_DOCKER_COMPOSE",
    ):
        value = str(os.environ.get(name) or "").strip()
        if not value:
            continue
        executable = "docker" if value == "docker compose" else value
        if shutil.which(executable) is None:
            os.environ.pop(name, None)


def run_role(app_dir: Path, project_name: str, role: str) -> int:
    app_dir = require_app_dir(app_dir)
    project_name = read_project_name(app_dir, project_name)
    if role == "update-helper-bootstrap":
        slot_id, source, manifest = resolve_authority(
            app_dir,
            project_name=project_name,
            repair=True,
        )
        reconcile_restart_policies(app_dir, project_name)
        ensure_exact_helper(app_dir, project_name, slot_id, source, manifest)
        print("bootstrap_recovery=PASS")
        return 0
    if role != "update-helper":
        raise BootstrapError("bootstrap_role_invalid", "Bootstrap role is invalid.")
    delay = 5
    last_code = ""
    while True:
        try:
            slot_id, source, manifest = resolve_authority(
                app_dir,
                project_name=project_name,
                repair=True,
            )
            reconcile_restart_policies(app_dir, project_name)
            ensure_exact_helper(app_dir, project_name, slot_id, source, manifest)
            script = source / "scripts/km-vms-update-helper.py"
            if script.is_symlink() or not script.is_file():
                raise BootstrapError("helper_script_missing", "Selected update-helper script is unavailable.")
            normalize_update_helper_compose_environment()
            os.environ["KM_VMS_PRODUCT_SOURCE_DIR"] = str(source)
            os.execv(sys.executable, [sys.executable, "-B", str(script)])
        except BootstrapError as exc:
            if exc.code != last_code:
                print(f"bootstrap_degraded={exc.code}", file=sys.stderr, flush=True)
                last_code = exc.code
            time.sleep(delay)
            delay = min(delay * 2, 60)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KM VMS stable bootstrap authority")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install-bundle")
    install.add_argument("--app-dir", required=True)
    install.add_argument("--source-dir", required=True)
    validate = subparsers.add_parser("validate-bundle")
    validate.add_argument("--app-dir", required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--app-dir", required=True)
    resolve.add_argument("--project-name")
    resolve.add_argument("--repair", action="store_true")
    resolve_path = subparsers.add_parser("resolve-path")
    resolve_path.add_argument("--app-dir", required=True)
    resolve_path.add_argument("--project-name")
    resolve_path.add_argument("--repair", action="store_true")
    image_override = subparsers.add_parser("image-override-path")
    image_override.add_argument("--app-dir", required=True)
    image_override.add_argument("--project-name", required=True)
    reconcile = subparsers.add_parser("reconcile-policies")
    reconcile.add_argument("--app-dir", required=True)
    reconcile.add_argument("--project-name", required=True)
    isolation = subparsers.add_parser("writer-isolation")
    isolation.add_argument("--app-dir", required=True)
    fence = subparsers.add_parser("writer-fence")
    fence.add_argument("--app-dir", required=True)
    fence.add_argument("--project-name", required=True)
    fence.add_argument("--enable", action="store_true")
    fence.add_argument("--disable", action="store_true")
    role = subparsers.add_parser("run-role")
    role.add_argument("role", choices=("update-helper-bootstrap", "update-helper"))
    role.add_argument("--app-dir", required=True)
    role.add_argument("--project-name", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "install-bundle":
            result = install_bundle(Path(args.app_dir), Path(args.source_dir))
        elif args.command == "validate-bundle":
            path, manifest = validate_current_bundle(Path(args.app_dir))
            result = {"bundle_path": str(path), "manifest": manifest}
        elif args.command == "resolve":
            slot_id, source, manifest = resolve_authority(
                Path(args.app_dir),
                project_name=args.project_name,
                repair=args.repair,
            )
            result = {"slot_id": slot_id, "source_path": str(source), "kind": manifest["kind"]}
        elif args.command == "resolve-path":
            _slot_id, source, _manifest = resolve_authority(
                Path(args.app_dir),
                project_name=args.project_name,
                repair=args.repair,
            )
            print(str(source))
            return 0
        elif args.command == "image-override-path":
            path, _slot_id = materialize_slot_image_override(
                Path(args.app_dir),
                args.project_name,
            )
            print(str(path))
            return 0
        elif args.command == "reconcile-policies":
            result = reconcile_restart_policies(
                Path(args.app_dir),
                args.project_name,
            )
        elif args.command == "writer-isolation":
            active = writer_isolation_active(Path(args.app_dir))
            print("active" if active else "inactive")
            return 75 if active else 0
        elif args.command == "writer-fence":
            if args.enable == args.disable:
                raise BootstrapError("writer_fence_action_invalid", "Choose exactly one writer-fence action.")
            result = set_writer_fence(
                Path(args.app_dir),
                args.project_name,
                enabled=args.enable,
            )
        elif args.command == "run-role":
            return run_role(Path(args.app_dir), args.project_name, args.role)
        else:
            raise BootstrapError("bootstrap_command_invalid", "Bootstrap command is invalid.")
    except BootstrapError as exc:
        print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
