"""Slot description contracts. No pointer or filesystem mutation."""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNTIME_SCHEMA_VERSION = 1

MANIFEST_SCHEMA_VERSION = 1

CANDIDATE_SCHEMA_VERSION = 1

EVIDENCE_SCHEMA_VERSION = 1

JOURNAL_SCHEMA_VERSION = 1

RUNTIME_RELATIVE = Path("data/update-runtime")

SLOTS_RELATIVE = RUNTIME_RELATIVE / "slots"

STAGING_RELATIVE = RUNTIME_RELATIVE / "staging"

ACTIVE_RELATIVE = RUNTIME_RELATIVE / "active"

JOURNAL_RELATIVE = Path("data/update-control/activation-journal.json")

INITIAL_MATERIALIZATION_RELATIVE = Path(
    "data/install-control/initial-slot-materialization.json"
)

INSTALLED_PROJECTION_RELATIVE = (
    RUNTIME_RELATIVE / "installed-projection/installed-slot.json"
)

MANIFEST_NAME = "slot-manifest.json"

CANDIDATE_NAME = "candidate-state.json"

RUNTIME_OVERRIDE_NAME = "docker-compose.runtime-override.yml"

SOURCE_DIR_NAME = "source"

PRODUCT_PATHS = (
    "apps",
    "deploy",
    "docs",
    "release",
    "scripts",
    "docker-compose.yml",
    "docker-compose.pytest.yml",
    ".dockerignore",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    ".km-vms-release.json",
    ".km-vms-source.json",
)

TRUSTED_REQUIRED_SOURCE_PATHS = (
    "apps/api",
    "apps/web",
    "apps/recorder",
    "apps/update-helper",
    "deploy/nginx/default.conf",
    "release/km-vms-release.json",
    "scripts/install.sh",
    "scripts/km-vms-bootstrap.py",
    "scripts/km-vms-bootstrap-dispatch.sh",
    "scripts/km-vms-compose-common.sh",
    "scripts/km-vms-restart.sh",
    "scripts/km-vms-storage-apply.sh",
    "scripts/km-vms-setup-activation-helper.sh",
    "scripts/km-vms-permission-gate.sh",
    "scripts/km-vms-release-identity.py",
    "scripts/km-vms-update-helper-bridge.py",
    "scripts/km-vms-release-slots.py",
    "docker-compose.yml",
)

LEGACY_REQUIRED_SOURCE_PATHS = (
    "apps/api",
    "apps/web",
    "apps/recorder",
    "apps/update-helper",
    "deploy/nginx/default.conf",
    "release/km-vms-release.json",
    "scripts/install.sh",
    "scripts/km-vms-compose-common.sh",
    "scripts/update.sh",
    "docker-compose.yml",
)

ADOPTED_REQUIRED_IMAGE_SERVICES = frozenset(
    {
        "api",
        "recorder",
        "web",
        "nginx",
        "update-helper",
    }
)

TARGET_REQUIRED_IMAGE_SERVICES = ADOPTED_REQUIRED_IMAGE_SERVICES | frozenset(
    {
        "schema-update",
        "update-status-reader",
        "update-retry-admission",
    }
)

TARGET_BUILT_IMAGE_SERVICES = frozenset(
    {
        "schema-update",
        "api",
        "recorder",
        "web",
        "update-helper",
        "update-status-reader",
        "update-retry-admission",
    }
)

REQUIRED_ADOPTED_COMPOSE_SERVICES = ADOPTED_REQUIRED_IMAGE_SERVICES | frozenset(
    {"postgres", "redis"}
)

KNOWN_COMPOSE_SERVICES = frozenset(
    {
        "postgres",
        "redis",
        "update-helper-bootstrap",
        "update-status-reader",
        "update-retry-admission",
        "schema-update",
        "api",
        "setup-helper",
        "update-helper",
        "recorder",
        "web",
        "nginx",
    }
)

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

REQUEST_RE = re.compile(r"^(?:update|stage609|terminal)-[0-9a-f]{32}$")

VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")

PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

SERVICE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")

IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

TRUSTED_SLOT_RE = re.compile(r"^release-[0-9a-f]{40}$")

ADOPTED_SLOT_RE = re.compile(r"^adopted-[0-9a-f]{64}$")

INITIAL_SLOT_RE = re.compile(r"^initial-[0-9a-f]{64}$")

SLOT_RE = re.compile(
    r"^(?:release-[0-9a-f]{40}|adopted-[0-9a-f]{64}|initial-[0-9a-f]{64})$"
)

MACHINE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")

RELEASE_NOTE_LOCALES = frozenset({"en", "ru", "zh-CN"})

RELEASE_NOTE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

RELEASE_NOTE_SENSITIVE_RE = re.compile(
    r"(github_pat_|ghp_|Bearer\s+|rtsp://[^@\s]+@|"
    r"postgresql://[^:\s]+:[^@\s]+@|"
    r"-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)

ACTIVATION_PHASES = frozenset(
    {
        "target_prepared",
        "quiescing",
        "schema_preparing",
        "activating",
        "verifying_target",
        "committing_target",
        "rolling_back",
        "completed",
        "failed_rolled_back",
        "blocked",
    }
)

TERMINAL_ACTIVATION_PHASES = frozenset(
    {"completed", "failed_rolled_back", "blocked"}
)

ACTIVATION_TRANSITIONS = {
    "target_prepared": {
        "quiescing",
        "schema_preparing",
        "activating",
        "rolling_back",
        "blocked",
    },
    "quiescing": {"schema_preparing", "activating", "rolling_back", "blocked"},
    "schema_preparing": {"activating", "rolling_back", "blocked"},
    "activating": {"verifying_target", "rolling_back", "blocked"},
    "verifying_target": {
        "committing_target",
        "rolling_back",
        "blocked",
    },
    "committing_target": {"completed", "rolling_back", "blocked"},
    "rolling_back": {"failed_rolled_back", "blocked"},
    "completed": set(),
    "failed_rolled_back": set(),
    "blocked": {"rolling_back"},
}

MAX_INVENTORY_ENTRIES = 200_000

MAX_INVENTORY_BYTES = 4 * 1024 * 1024 * 1024

MAX_JSON_BYTES = 1024 * 1024

MAX_LEGACY_PYTHON_CACHE_ENTRIES = 10_000


class SlotError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(payload: Any) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise SlotError("json_invalid", "Evidence cannot be serialized safely.") from exc
