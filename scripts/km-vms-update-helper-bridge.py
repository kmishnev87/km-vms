#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


MAX_CONTROL_BYTES = 64 * 1024
MAX_JSON_NESTING_DEPTH = 32
DEFAULT_TIMEOUT_SECONDS = 7800
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 9000
DEFAULT_POLL_SECONDS = 2
PERMISSION_GATE_TIMEOUT_SECONDS = 300

REQUEST_ID_RE = re.compile(r"^update-[0-9a-f]{32}$", re.IGNORECASE)
PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
HELPER_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}:[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

ACTIVE_STATUSES = {
    "queued",
    "starting_helper",
    "preflight",
    "acquire_source",
    "downloading",
    "extracting",
    "validating_source",
    "overlay",
    "applying",
    "compose_config",
    "rebuilding",
    "restarting",
    "health_check",
    "commit_verification",
}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "blocked"}
INACTIVE_STATUSES = {"idle", "unknown"}
KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES | INACTIVE_STATUSES
RECEIPT_STATUSES = {"waiting_for_terminal", "recreating", "completed", "failed"}
UNBOUND_STATUS_ALLOWED = {"idle", "unknown", "blocked"}


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _DuplicateJsonKey(ValueError):
    pass


class _JsonNestingTooDeep(ValueError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(value)


def validate_json_nesting(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_NESTING_DEPTH:
        raise _JsonNestingTooDeep()
    if isinstance(value, dict):
        for child in value.values():
            validate_json_nesting(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate_json_nesting(child, depth + 1)


def read_json_object(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            raise BridgeError("control_file_invalid", "Update control data is not a regular file.")
        if not path.exists():
            if missing_ok:
                return None
            raise BridgeError("control_file_missing", "Required update control data is missing.")
        if not path.is_file() or path.is_symlink():
            raise BridgeError("control_file_invalid", "Update control data is not a regular file.")
        if path.stat().st_size > MAX_CONTROL_BYTES:
            raise BridgeError("control_file_too_large", "Update control data exceeds its size limit.")
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_object_pairs,
            parse_constant=reject_nonfinite_json_constant,
        )
        validate_json_nesting(payload)
    except BridgeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise BridgeError("control_file_invalid", "Update control data is unavailable or invalid.") from exc
    if not isinstance(payload, dict):
        raise BridgeError("control_file_invalid", "Update control data must be a JSON object.")
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(rendered.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise BridgeError("receipt_too_large", "Update-helper refresh receipt exceeds its size limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            os.chmod(tmp, 0o600)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise BridgeError("receipt_write_failed", "Cannot persist update-helper refresh receipt.") from exc


def read_status(path: Path) -> tuple[str | None, str, dict[str, Any]] | None:
    payload = read_json_object(path, missing_ok=True)
    if payload is None:
        return None
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
        raise BridgeError("status_schema_invalid", "Update status has an unsupported schema version.")
    request_id = payload.get("request_id")
    status = payload.get("status")
    if not isinstance(status, str) or status not in KNOWN_STATUSES:
        raise BridgeError("status_value_invalid", "Update status has an unsupported top-level state.")
    if request_id is None and status in UNBOUND_STATUS_ALLOWED:
        return None, status, payload
    if isinstance(request_id, str) and REQUEST_ID_RE.fullmatch(request_id):
        return request_id, status, payload
    if status in TERMINAL_STATUSES or status in INACTIVE_STATUSES:
        # A pre-canonical historical terminal record is inert. It must not
        # prevent a normal restart after this bridge is first installed, but
        # it can never be accepted by a coordinator waiting for a canonical
        # active request because its returned identity is deliberately None.
        return None, status, payload
    raise BridgeError("status_request_invalid", "Update status has no canonical request identity.")


def validate_completed_status(payload: dict[str, Any], request_id: str) -> None:
    if payload.get("request_id") != request_id or payload.get("status") != "completed":
        raise BridgeError("terminal_status_invalid", "Terminal update completion is not bound to the expected request.")
    expected_commit = payload.get("expected_commit")
    installed_commit = payload.get("installed_commit")
    if (
        payload.get("commit_verified") is not True
        or not isinstance(expected_commit, str)
        or not COMMIT_SHA_RE.fullmatch(expected_commit)
        or not isinstance(installed_commit, str)
        or not COMMIT_SHA_RE.fullmatch(installed_commit)
        or expected_commit.lower() != installed_commit.lower()
    ):
        raise BridgeError("terminal_commit_invalid", "Terminal update completion lacks matching verified commit evidence.")
    if not isinstance(payload.get("finished_at"), str) or not payload.get("finished_at"):
        raise BridgeError("terminal_timestamp_invalid", "Terminal update completion lacks a finish timestamp.")


def require_request_id(value: str) -> str:
    if not REQUEST_ID_RE.fullmatch(value):
        raise BridgeError("request_id_invalid", "A canonical update request id is required.")
    return value


def require_project_name(value: str) -> str:
    if not PROJECT_NAME_RE.fullmatch(value):
        raise BridgeError("project_name_invalid", "A safe Docker Compose project name is required.")
    return value


def require_helper_image(value: str) -> str:
    if not HELPER_IMAGE_RE.fullmatch(value):
        raise BridgeError("helper_image_invalid", "A safe target update-helper image reference is required.")
    return value


def require_image_id(value: str) -> str:
    normalized = value.strip().lower()
    if not IMAGE_ID_RE.fullmatch(normalized):
        raise BridgeError("image_id_invalid", "Target update-helper image identity is invalid.")
    return normalized


def require_timeout(value: int) -> int:
    if value < MIN_TIMEOUT_SECONDS or value > MAX_TIMEOUT_SECONDS:
        raise BridgeError(
            "timeout_invalid",
            f"Refresh timeout must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS} seconds.",
        )
    return value


def require_app_dir(value: str) -> Path:
    if not value or "\x00" in value or "\n" in value or "\r" in value or ":" in value:
        raise BridgeError("app_dir_invalid", "A safe absolute host app directory is required.")
    app_dir = Path(value)
    if not app_dir.is_absolute() or not app_dir.is_dir():
        raise BridgeError("app_dir_invalid", "The host app directory is not mounted or is not absolute.")
    for relative in (
        "docker-compose.yml",
        ".env",
        "scripts/km-vms-permission-gate.sh",
        "scripts/km-vms-update-helper-bridge.py",
    ):
        candidate = app_dir / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise BridgeError("app_dir_incomplete", "The host app directory is missing required bridge files.")
    return app_dir


def run_command(
    args: Sequence[str],
    *,
    timeout: int,
    error_code: str,
    error_message: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(args),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError(error_code, error_message) from exc
    if check and result.returncode != 0:
        raise BridgeError(error_code, error_message)
    return result


def ensure_docker_runtime() -> None:
    run_command(
        ["docker", "version"],
        timeout=20,
        error_code="docker_unavailable",
        error_message="Docker daemon is unavailable to the update-helper bridge.",
    )
    run_command(
        ["docker", "compose", "version"],
        timeout=20,
        error_code="compose_unavailable",
        error_message="Docker Compose is unavailable to the update-helper bridge.",
    )


def docker_image_id(image: str) -> str:
    result = run_command(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        timeout=30,
        error_code="helper_image_missing",
        error_message="The target update-helper image was not prepared.",
    )
    return require_image_id(result.stdout.strip())


def inspected_container_image(name_or_id: str) -> str | None:
    result = run_command(
        ["docker", "inspect", "--format", "{{.Image}}", name_or_id],
        timeout=20,
        error_code="container_inspect_failed",
        error_message="Cannot inspect the update-helper bridge container.",
        check=False,
    )
    if result.returncode != 0:
        return None
    return require_image_id(result.stdout.strip())


def write_receipt(
    receipt_file: Path,
    *,
    request_id: str,
    status: str,
    expected_image_id: str,
    message: str,
    helper_container_id: str | None = None,
) -> None:
    atomic_write_json(
        receipt_file,
        {
            "schema_version": 1,
            "request_id": request_id,
            "status": status,
            "expected_image_id": expected_image_id,
            "helper_container_id": helper_container_id,
            "updated_at": utcnow(),
            "message": message,
        },
    )


def validate_receipt_binding(receipt_file: Path, request_id: str, expected_image_id: str) -> None:
    payload = read_json_object(receipt_file, missing_ok=True)
    if payload is None or payload.get("request_id") != request_id:
        return
    status = payload.get("status")
    if status not in RECEIPT_STATUSES:
        raise BridgeError("receipt_status_invalid", "Existing helper refresh receipt has an invalid status.")
    if payload.get("expected_image_id") != expected_image_id:
        raise BridgeError("receipt_image_mismatch", "Existing helper refresh receipt names another target image.")
    if status == "completed":
        raise BridgeError("receipt_state_contradiction", "Helper refresh is completed while the same update remains active.")


def run_target_permission_gate(app_dir: Path) -> None:
    gate = app_dir / "scripts/km-vms-permission-gate.sh"
    for action, error_code, error_message in (
        (
            "--fix",
            "target_permission_fix_failed",
            "Target product permissions could not be normalized after the legacy overlay.",
        ),
        (
            "--check",
            "target_permission_check_failed",
            "Target product permissions failed the post-overlay verification.",
        ),
    ):
        result = run_command(
            ["sh", str(gate), action, "--app-dir", str(app_dir)],
            timeout=PERMISSION_GATE_TIMEOUT_SECONDS,
            error_code=error_code,
            error_message=error_message,
            check=False,
        )
        if result.returncode != 0:
            raise BridgeError(error_code, error_message)
        output_lines = set(result.stdout.splitlines())
        required_lines = {
            "permission_gate=PASS",
            f"permission_action={action.removeprefix('--')}",
            f"permission_app_dir={app_dir}",
            "permission_contract=target",
        }
        if not required_lines.issubset(output_lines):
            raise BridgeError(
                "target_permission_result_invalid",
                "Target product permission gate returned incomplete success evidence.",
            )


def schedule_refresh(
    *,
    app_dir: Path,
    project_name: str,
    helper_image: str,
    request_id: str,
    expected_image_id: str,
    timeout_seconds: int,
) -> str:
    coordinator_name = f"km-vms-helper-refresh-{request_id.removeprefix('update-').lower()}"
    existing_image_id = inspected_container_image(coordinator_name)
    if existing_image_id is not None:
        if existing_image_id != expected_image_id:
            raise BridgeError("coordinator_image_mismatch", "Existing helper refresh coordinator uses another image.")
        return "already_scheduled"

    script_path = app_dir / "scripts/km-vms-update-helper-bridge.py"
    command = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        coordinator_name,
        "-v",
        f"{app_dir}:{app_dir}",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-w",
        str(app_dir),
        "--entrypoint",
        "python3",
        expected_image_id,
        str(script_path),
        "refresh",
        "--app-dir",
        str(app_dir),
        "--project-name",
        project_name,
        "--helper-image",
        helper_image,
        "--request-id",
        request_id,
        "--expected-image-id",
        expected_image_id,
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    result = run_command(
        command,
        timeout=60,
        error_code="coordinator_start_failed",
        error_message="Cannot start the detached update-helper refresh coordinator.",
        check=False,
    )
    if result.returncode == 0:
        return "scheduled"

    # A concurrent bootstrap may have won the atomic Docker container-name race.
    existing_image_id = inspected_container_image(coordinator_name)
    if existing_image_id == expected_image_id:
        return "already_scheduled"
    raise BridgeError("coordinator_start_failed", "Cannot start the detached update-helper refresh coordinator.")


def bootstrap(args: argparse.Namespace) -> int:
    app_dir = require_app_dir(args.app_dir or os.getenv("KM_VMS_BOOTSTRAP_APP_DIR", ""))
    project_name = require_project_name(
        args.project_name or os.getenv("KM_VMS_BOOTSTRAP_PROJECT_NAME", "")
    )
    helper_image = require_helper_image(
        args.helper_image or os.getenv("KM_VMS_BOOTSTRAP_HELPER_IMAGE", "")
    )
    required_request_id = (
        require_request_id(args.require_request_id) if args.require_request_id else None
    )
    raw_timeout = args.timeout_seconds
    if raw_timeout is None:
        try:
            raw_timeout = int(
                os.getenv("KM_VMS_BOOTSTRAP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
            )
        except ValueError as exc:
            raise BridgeError("timeout_invalid", "Refresh timeout must be an integer.") from exc
    timeout_seconds = require_timeout(raw_timeout)
    ensure_docker_runtime()

    control_dir = app_dir / "data/update-control"
    status_record = read_status(control_dir / "update-status.json")
    if status_record is None:
        if required_request_id:
            raise BridgeError("active_status_missing", "The expected active update status is missing.")
        print("update_helper_bootstrap=NO_ACTIVE_UPDATE")
        return 0

    request_id, status, _payload = status_record
    if status not in ACTIVE_STATUSES:
        if required_request_id:
            raise BridgeError("active_status_missing", "The expected update is not active.")
        print("update_helper_bootstrap=NO_ACTIVE_UPDATE")
        return 0
    if required_request_id and request_id != required_request_id:
        raise BridgeError("active_request_mismatch", "The active update does not match the required request.")
    if request_id is None:
        raise BridgeError("status_request_invalid", "Active update status has no canonical request identity.")

    run_target_permission_gate(app_dir)
    expected_image_id = docker_image_id(helper_image)
    receipt_file = control_dir / "update-helper-refresh.json"
    validate_receipt_binding(receipt_file, request_id, expected_image_id)

    result = schedule_refresh(
        app_dir=app_dir,
        project_name=project_name,
        helper_image=helper_image,
        request_id=request_id,
        expected_image_id=expected_image_id,
        timeout_seconds=timeout_seconds,
    )
    print("permission_gate=PASS")
    print("permission_contract=target")
    print("update_helper_bootstrap=PASS" if result == "scheduled" else "update_helper_bootstrap=ALREADY_SCHEDULED")
    print(f"update_helper_request_id={request_id}")
    print(f"update_helper_expected_image_id={expected_image_id}")
    return 0


def wait_for_terminal_status(
    status_file: Path,
    *,
    request_id: str,
    timeout_seconds: int,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = read_status(status_file)
        if record is None:
            time.sleep(poll_seconds)
            continue
        observed_request_id, status, payload = record
        if observed_request_id != request_id:
            raise BridgeError("status_request_mismatch", "Update status changed to another request during helper handoff.")
        if status == "completed":
            validate_completed_status(payload, request_id)
            return payload
        if status in TERMINAL_STATUSES:
            raise BridgeError("update_not_completed", "The current update reached a non-success terminal state.")
        if status in INACTIVE_STATUSES:
            raise BridgeError("update_status_inactive", "The current update became inactive before terminal completion.")
        time.sleep(poll_seconds)
    raise BridgeError("terminal_wait_timeout", "Timed out waiting for terminal update completion.")


def wait_for_helper_lease_release(control_dir: Path, timeout_seconds: int = 60) -> None:
    lease_file = control_dir / "update-helper-claim.lock"
    deadline = time.monotonic() + timeout_seconds
    try:
        with lease_file.open("a+", encoding="utf-8") as stream:
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(1)
                    continue
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return
    except OSError as exc:
        raise BridgeError("helper_lease_unavailable", "Cannot inspect the active update-helper lease.") from exc
    raise BridgeError("helper_lease_timeout", "The active update-helper did not release its execution lease.")


def compose_base(app_dir: Path, project_name: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(app_dir / ".env"),
        "-f",
        str(app_dir / "docker-compose.yml"),
        "-p",
        project_name,
    ]


def recreate_and_verify_helper(
    *,
    app_dir: Path,
    project_name: str,
    helper_image: str,
    expected_image_id: str,
) -> str:
    if docker_image_id(helper_image) != expected_image_id:
        raise BridgeError("prepared_image_changed", "The prepared update-helper image changed before activation.")
    compose = compose_base(app_dir, project_name)
    run_command(
        [*compose, "up", "-d", "--no-deps", "--force-recreate", "update-helper"],
        timeout=300,
        error_code="helper_recreate_failed",
        error_message="Docker Compose could not recreate update-helper.",
    )
    result = run_command(
        [*compose, "ps", "-q", "update-helper"],
        timeout=30,
        error_code="helper_identity_missing",
        error_message="Recreated update-helper container identity is unavailable.",
    )
    container_id = result.stdout.strip().lower()
    if not CONTAINER_ID_RE.fullmatch(container_id):
        raise BridgeError("helper_identity_missing", "Recreated update-helper container identity is unavailable.")

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        running = run_command(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
            timeout=20,
            error_code="helper_inspect_failed",
            error_message="Cannot inspect recreated update-helper.",
            check=False,
        )
        if running.returncode == 0 and running.stdout.strip() == "true":
            break
        time.sleep(1)
    else:
        raise BridgeError("helper_not_running", "Recreated update-helper did not reach running state.")

    if inspected_container_image(container_id) != expected_image_id:
        raise BridgeError("helper_image_mismatch", "Recreated update-helper is not using the prepared target image.")
    run_command(
        [
            "docker",
            "exec",
            container_id,
            "sh",
            "-c",
            "command -v getfacl >/dev/null 2>&1 && getfacl --version >/dev/null 2>&1",
        ],
        timeout=30,
        error_code="helper_acl_runtime_missing",
        error_message="Recreated update-helper does not provide working getfacl.",
    )
    running = run_command(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
        timeout=20,
        error_code="helper_inspect_failed",
        error_message="Cannot inspect recreated update-helper.",
    )
    if running.stdout.strip() != "true":
        raise BridgeError("helper_not_running", "Recreated update-helper is not running after runtime verification.")
    return container_id


def refresh(args: argparse.Namespace) -> int:
    app_dir = require_app_dir(args.app_dir)
    project_name = require_project_name(args.project_name)
    helper_image = require_helper_image(args.helper_image)
    request_id = require_request_id(args.request_id)
    expected_image_id = require_image_id(args.expected_image_id)
    timeout_seconds = require_timeout(args.timeout_seconds)
    control_dir = app_dir / "data/update-control"
    receipt_file = control_dir / "update-helper-refresh.json"
    write_receipt(
        receipt_file,
        request_id=request_id,
        status="waiting_for_terminal",
        expected_image_id=expected_image_id,
        message="Waiting for exact terminal update completion.",
    )
    try:
        ensure_docker_runtime()
        wait_for_terminal_status(
            control_dir / "update-status.json",
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        wait_for_helper_lease_release(control_dir)
        write_receipt(
            receipt_file,
            request_id=request_id,
            status="recreating",
            expected_image_id=expected_image_id,
            message="Activating the prepared target update-helper image.",
        )
        container_id = recreate_and_verify_helper(
            app_dir=app_dir,
            project_name=project_name,
            helper_image=helper_image,
            expected_image_id=expected_image_id,
        )
        write_receipt(
            receipt_file,
            request_id=request_id,
            status="completed",
            expected_image_id=expected_image_id,
            helper_container_id=container_id,
            message="update-helper was recreated and its ACL runtime was verified.",
        )
    except BridgeError as exc:
        try:
            write_receipt(
                receipt_file,
                request_id=request_id,
                status="failed",
                expected_image_id=expected_image_id,
                message=exc.code,
            )
        except BridgeError:
            pass
        raise

    print("update_helper_refresh=PASS")
    print(f"update_helper_request_id={request_id}")
    print(f"update_helper_container_id={container_id}")
    print(f"update_helper_image_id={expected_image_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KM VMS update-helper legacy transition bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap", help="schedule a refresh for an active update")
    bootstrap_parser.add_argument("--app-dir")
    bootstrap_parser.add_argument("--project-name")
    bootstrap_parser.add_argument("--helper-image")
    bootstrap_parser.add_argument("--require-request-id")
    bootstrap_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
    )
    bootstrap_parser.set_defaults(handler=bootstrap)

    refresh_parser = subparsers.add_parser("refresh", help="wait for completion and recreate update-helper")
    refresh_parser.add_argument("--app-dir", required=True)
    refresh_parser.add_argument("--project-name", required=True)
    refresh_parser.add_argument("--helper-image", required=True)
    refresh_parser.add_argument("--request-id", required=True)
    refresh_parser.add_argument("--expected-image-id", required=True)
    refresh_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    refresh_parser.set_defaults(handler=refresh)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except BridgeError as exc:
        print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
