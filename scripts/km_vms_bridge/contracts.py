"""Bridge rules and failures, independent of installation state."""
from __future__ import annotations
import re
from datetime import datetime, timezone

MAX_CONTROL_BYTES = 64 * 1024

MAX_LEGACY_ADMISSION_BYTES = 512 * 1024

MAX_LEGACY_ADMISSION_ENTRIES = 256

DEFAULT_TIMEOUT_SECONDS = 7800

MIN_TIMEOUT_SECONDS = 30

MAX_TIMEOUT_SECONDS = 9000

DEFAULT_POLL_SECONDS = 2

PERMISSION_GATE_TIMEOUT_SECONDS = 300

REQUEST_ID_RE = re.compile(
    r"^(?:update|stage609|terminal)-[0-9a-f]{32}$",
    re.IGNORECASE,
)

PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")

HELPER_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}:[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")

COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

SLOT_ID_RE = re.compile(
    r"^(?:release-[0-9a-f]{40}|adopted-[0-9a-f]{64}|initial-[0-9a-f]{64})$",
    re.IGNORECASE,
)

ARCHIVE_RUNTIME_TARGET_RE = re.compile(
    r"^/storage/archive-roots/[A-Za-z0-9_.-]{1,180}$"
)

SUBMISSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

LEGACY_REQUIRED_IMAGE_SERVICES = (
    "api",
    "recorder",
    "web",
    "nginx",
    "update-helper",
)

OPTIONAL_PERSISTENT_IMAGE_SERVICES = (
    "update-status-reader",
    "update-retry-admission",
)

TARGET_BUILD_SERVICES = (
    "schema-update",
    "api",
    "recorder",
    "web",
    "update-helper",
    "update-status-reader",
)

TARGET_EVIDENCE_SERVICES = (
    *LEGACY_REQUIRED_IMAGE_SERVICES,
    *OPTIONAL_PERSISTENT_IMAGE_SERVICES,
    "schema-update",
)

ACTIVATION_RUNTIME_SERVICES = (
    "update-retry-admission",
    "update-status-reader",
    "api",
    "recorder",
    "web",
    "nginx",
    "setup-helper",
)

CORE_RUNTIME_SERVICES = ("api", "recorder", "web", "nginx")

REQUEST_SCOPED_COMPOSE_EVIDENCE_FIELDS = frozenset(
    {
        "captured_plan_sha256",
        "slot_plan_sha256",
    }
)

REQUEST_ID_COMPOSE_TOKEN = "${KM_VMS_UPDATE_CONTROL_REQUEST_ID}"

LEGACY_HISTORICAL_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "requested_at",
    "requested_by",
    "intent",
    "source",
    "confirmed",
    "preflight_required",
    "status_path",
}

LEGACY_SNAPSHOT_REQUEST_FIELDS = LEGACY_HISTORICAL_REQUEST_FIELDS | {
    "apply_candidate",
}

SCHEMA_RETRY_REQUEST_FIELDS = LEGACY_HISTORICAL_REQUEST_FIELDS | {
    "retry_of_request_id",
    "migration_attempt_id",
}

CURRENT_SINGLE_REQUEST_FIELDS = {
    "schema_version",
    "document_type",
    "request_id",
    "submission_id",
    "requested_at",
    "updated_at",
    "requested_by",
    "intent",
    "source",
    "apply_candidate",
    "confirmed",
    "preflight_required",
    "status_path",
    "state",
    "claimed_at",
    "terminal",
    "audit_event_id",
}

NORMALIZED_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "submission_id",
    "requested_at",
    "requested_by",
    "intent",
    "source",
    "apply_candidate",
    "confirmed",
    "preflight_required",
    "status_path",
}

UPDATE_LINEAGE_FILENAME = "km-vms-update-lineage.json"

UPDATE_LINEAGE_MAX_BYTES = 128 * 1024

CURRENT_PRODUCT_DB_SCHEMA_VERSION = 10

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
    "preparing",
    "staging",
    "activating",
    "reconnecting",
    "rolling_back",
}

TERMINAL_STATUSES = {
    "completed",
    "failed",
    "failed_rolled_back",
    "cancelled",
    "blocked",
}

INACTIVE_STATUSES = {"idle", "unknown"}

KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES | INACTIVE_STATUSES

RECEIPT_STATUSES = {"waiting_for_terminal", "recreating", "completed", "failed"}

UNBOUND_STATUS_ALLOWED = {"idle", "unknown", "blocked"}


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
