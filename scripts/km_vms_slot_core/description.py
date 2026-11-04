"""Validate immutable descriptions independently from slot activation."""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any
from .contracts import MANIFEST_SCHEMA_VERSION, EVIDENCE_SCHEMA_VERSION, JOURNAL_SCHEMA_VERSION, SOURCE_DIR_NAME, ADOPTED_REQUIRED_IMAGE_SERVICES, TARGET_REQUIRED_IMAGE_SERVICES, TARGET_BUILT_IMAGE_SERVICES, REQUIRED_ADOPTED_COMPOSE_SERVICES, KNOWN_COMPOSE_SERVICES, COMMIT_RE, DIGEST_RE, REQUEST_RE, VERSION_RE, PROJECT_RE, SERVICE_RE, IMAGE_ID_RE, TRUSTED_SLOT_RE, ADOPTED_SLOT_RE, INITIAL_SLOT_RE, SLOT_RE, MACHINE_CODE_RE, RELEASE_NOTE_LOCALES, RELEASE_NOTE_CONTROL_RE, RELEASE_NOTE_SENSITIVE_RE, ACTIVATION_PHASES, TERMINAL_ACTIVATION_PHASES, SlotError, canonical_json

def _validated_release_note_fields(
    descriptor: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def text(value: Any, *, maximum: int, allow_empty: bool = False) -> str:
        if type(value) is not str:
            raise SlotError(
                "release_descriptor_invalid",
                "Trusted target release notes are invalid.",
            )
        normalized = value.strip()
        if (
            (not normalized and not allow_empty)
            or len(normalized) > maximum
            or RELEASE_NOTE_CONTROL_RE.search(normalized)
            or RELEASE_NOTE_SENSITIVE_RE.search(normalized)
        ):
            raise SlotError(
                "release_descriptor_invalid",
                "Trusted target release notes are invalid.",
            )
        return normalized

    def entries(value: Any) -> list[str]:
        if type(value) is not list or len(value) > 20:
            raise SlotError(
                "release_descriptor_invalid",
                "Trusted target release changelog is invalid.",
            )
        return [text(item, maximum=180) for item in value]

    for key, maximum in (("title", 160), ("summary", 800)):
        if key in descriptor:
            rendered = text(
                descriptor[key],
                maximum=maximum,
                allow_empty=True,
            )
            if rendered:
                result[key] = rendered
    if "changelog" in descriptor:
        result["changelog"] = entries(descriptor["changelog"])
    for key, maximum in (
        ("title_i18n", 160),
        ("summary_i18n", 800),
    ):
        if key not in descriptor:
            continue
        value = descriptor[key]
        if type(value) is not dict or set(value) != RELEASE_NOTE_LOCALES:
            raise SlotError(
                "release_descriptor_invalid",
                "Trusted target release note locales are invalid.",
            )
        result[key] = {
            locale: text(value[locale], maximum=maximum)
            for locale in ("en", "ru", "zh-CN")
        }
    if "changelog_i18n" in descriptor:
        value = descriptor["changelog_i18n"]
        if type(value) is not dict or set(value) != RELEASE_NOTE_LOCALES:
            raise SlotError(
                "release_descriptor_invalid",
                "Trusted target changelog locales are invalid.",
            )
        result["changelog_i18n"] = {
            locale: entries(value[locale])
            for locale in ("en", "ru", "zh-CN")
        }
    return result


def validate_compose_services(value: Any) -> list[str]:
    if (
        type(value) is not list
        or not 1 <= len(value) <= 64
        or any(type(item) is not str or not SERVICE_RE.fullmatch(item) for item in value)
        or value != sorted(set(value))
        or not REQUIRED_ADOPTED_COMPOSE_SERVICES.issubset(value)
        or not set(value).issubset(KNOWN_COMPOSE_SERVICES)
    ):
        raise SlotError(
            "compose_services_invalid",
            "Compose service evidence is incomplete or contains an unknown service.",
        )
    return list(value)


def validate_compose_evidence(value: Any) -> dict[str, Any]:
    expected = {
        "schema_version",
        "project_name",
        "project_directory",
        "captured_plan_sha256",
        "slot_plan_sha256",
        "archive_override_attached",
        "archive_override_sha256",
        "runtime_override_sha256",
        "shared_root_contract",
        "services",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or type(value.get("project_name")) is not str
        or not PROJECT_RE.fullmatch(value["project_name"])
        or value.get("project_directory") != SOURCE_DIR_NAME
        or type(value.get("captured_plan_sha256")) is not str
        or not DIGEST_RE.fullmatch(value["captured_plan_sha256"])
        or type(value.get("slot_plan_sha256")) is not str
        or not DIGEST_RE.fullmatch(value["slot_plan_sha256"])
        or type(value.get("archive_override_attached")) is not bool
        or value.get("shared_root_contract") != "stable_app_dir_v1"
    ):
        raise SlotError("compose_evidence_invalid", "Compose evidence is invalid.")
    validate_compose_services(value.get("services"))
    archive_digest = value.get("archive_override_sha256")
    if value["archive_override_attached"]:
        if type(archive_digest) is not str or not DIGEST_RE.fullmatch(archive_digest):
            raise SlotError("compose_evidence_invalid", "Archive-root Compose evidence is invalid.")
    elif archive_digest is not None:
        raise SlotError("compose_evidence_invalid", "Absent archive override has contradictory evidence.")
    runtime_digest = value.get("runtime_override_sha256")
    if runtime_digest is not None and (
        type(runtime_digest) is not str or not DIGEST_RE.fullmatch(runtime_digest)
    ):
        raise SlotError(
            "compose_evidence_invalid",
            "Slot runtime override evidence is invalid.",
        )
    return json.loads(json.dumps(value))


def validate_image_evidence(value: Any) -> dict[str, Any]:
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "services"}
        or value.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or type(value.get("services")) is not dict
        or len(value["services"]) > 32
    ):
        raise SlotError("image_evidence_invalid", "Immutable image evidence is incomplete.")
    normalized: dict[str, Any] = {"schema_version": 1, "services": {}}
    for service, item in sorted(value["services"].items()):
        if (
            type(service) is not str
            or not SERVICE_RE.fullmatch(service)
            or type(item) is not dict
            or set(item)
            != {"image_id", "source_image_ref", "immutable_image_ref"}
            or type(item.get("image_id")) is not str
            or not IMAGE_ID_RE.fullmatch(item["image_id"])
            or any(
                type(item.get(key)) is not str
                or not 1 <= len(item[key]) <= 240
                or any(char in item[key] for char in "\r\n\x00")
                for key in ("source_image_ref", "immutable_image_ref")
            )
        ):
            raise SlotError("image_evidence_invalid", "Immutable image evidence is invalid.")
        normalized["services"][service] = dict(item)
    return normalized


def validate_health_evidence(value: Any) -> dict[str, Any]:
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema_version",
            "status",
            "api_visible_identity_sha256",
            "core_services",
        }
        or value.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or value.get("status") != "healthy"
        or type(value.get("api_visible_identity_sha256")) is not str
        or not DIGEST_RE.fullmatch(value["api_visible_identity_sha256"])
        or type(value.get("core_services")) is not list
        or sorted(value["core_services"]) != ["api", "nginx", "recorder", "web"]
    ):
        raise SlotError("health_evidence_invalid", "Pre-update core health evidence is invalid.")
    return json.loads(json.dumps(value))


def validate_manifest(value: Any, *, expected_slot_id: str | None = None) -> dict[str, Any]:
    expected = {
        "schema_version",
        "kind",
        "slot_id",
        "declared_identity",
        "official_source_match",
        "source_inventory",
        "compose_evidence",
        "image_evidence",
        "pre_update_health",
        "prepared_at",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or value.get("kind")
        not in {
            "trusted_release",
            "adopted_pre_update_snapshot",
            "initial_install_snapshot",
        }
        or type(value.get("slot_id")) is not str
        or not SLOT_RE.fullmatch(value["slot_id"])
        or (expected_slot_id is not None and value["slot_id"] != expected_slot_id)
        or type(value.get("declared_identity")) is not dict
        or set(value["declared_identity"]) != {"version", "commit"}
        or type(value["declared_identity"].get("version")) is not str
        or not VERSION_RE.fullmatch(value["declared_identity"]["version"])
        or (
            value["declared_identity"].get("commit") is not None
            and (
                type(value["declared_identity"]["commit"]) is not str
                or not COMMIT_RE.fullmatch(
                    value["declared_identity"]["commit"]
                )
            )
        )
        or type(value.get("official_source_match")) is not bool
        or type(value.get("source_inventory")) is not dict
        or set(value["source_inventory"])
        != {"schema_version", "sha256", "entry_count", "file_count", "total_bytes"}
        or value["source_inventory"].get("schema_version") != 1
        or type(value["source_inventory"].get("sha256")) is not str
        or not DIGEST_RE.fullmatch(value["source_inventory"]["sha256"])
        or any(
            type(value["source_inventory"].get(key)) is not int
            or value["source_inventory"][key] < 0
            for key in ("entry_count", "file_count", "total_bytes")
        )
        or type(value.get("prepared_at")) is not str
        or not value["prepared_at"]
    ):
        raise SlotError("slot_manifest_invalid", "Immutable slot manifest is invalid.")
    if value["kind"] == "trusted_release":
        if (
            not TRUSTED_SLOT_RE.fullmatch(value["slot_id"])
            or value["slot_id"].removeprefix("release-")
            != value["declared_identity"]["commit"]
            or value["official_source_match"] is not True
            or value.get("pre_update_health") is not None
        ):
            raise SlotError("slot_manifest_invalid", "Trusted release manifest is contradictory.")
        required_images = TARGET_REQUIRED_IMAGE_SERVICES
    elif value["kind"] == "adopted_pre_update_snapshot":
        if (
            not ADOPTED_SLOT_RE.fullmatch(value["slot_id"])
            or value["slot_id"].removeprefix("adopted-")
            != value["source_inventory"]["sha256"]
            or value["official_source_match"] is not False
        ):
            raise SlotError("slot_manifest_invalid", "Adopted snapshot manifest is contradictory.")
        validate_health_evidence(value.get("pre_update_health"))
        required_images = ADOPTED_REQUIRED_IMAGE_SERVICES
    else:
        if (
            not INITIAL_SLOT_RE.fullmatch(value["slot_id"])
            or value["declared_identity"]["commit"] is not None
            or value["official_source_match"] is not False
            or value.get("pre_update_health") is not None
        ):
            raise SlotError(
                "slot_manifest_invalid",
                "Initial install manifest is contradictory.",
            )
        required_images = TARGET_REQUIRED_IMAGE_SERVICES
    compose = validate_compose_evidence(value.get("compose_evidence"))
    if value["kind"] in {"trusted_release", "initial_install_snapshot"}:
        if compose["runtime_override_sha256"] is not None:
            raise SlotError(
                "slot_manifest_invalid",
                "Non-legacy slot has an unexpected runtime override.",
            )
    elif (
        value["kind"] == "adopted_pre_update_snapshot"
        and compose["runtime_override_sha256"] is None
    ):
        raise SlotError(
            "slot_manifest_invalid",
            "Adopted snapshot lacks its stable-root runtime override evidence.",
        )
    images = validate_image_evidence(value.get("image_evidence"))
    if not required_images.issubset(images["services"]):
        raise SlotError(
            "slot_manifest_invalid",
            "Release slot lacks required immutable image evidence.",
        )
    for service, item in images["services"].items():
        if not item["immutable_image_ref"].endswith(f":{value['slot_id']}"):
            raise SlotError(
                "slot_manifest_invalid",
                "Release slot image evidence is not namespaced by its immutable slot ID.",
            )
        if (
            value["kind"] in {"trusted_release", "initial_install_snapshot"}
            and service in TARGET_BUILT_IMAGE_SERVICES
            and not item["source_image_ref"].endswith(f":{value['slot_id']}")
        ):
            raise SlotError(
                "slot_manifest_invalid",
                "Target build evidence used a mutable image tag.",
            )
    forbidden_roles = {"status", "current", "previous", "candidate", "active"}
    if forbidden_roles & set(value):
        raise SlotError("slot_manifest_role_forbidden", "Slot manifest contains a mutable role.")
    return json.loads(json.dumps(value))


def _payload_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def validate_activation_slot_binding(value: Any) -> dict[str, Any]:
    expected = {
        "slot_id",
        "kind",
        "official_source_match",
        "version",
        "commit",
        "manifest_sha256",
        "inventory_sha256",
        "compose_plan_sha256",
        "archive_override_sha256",
        "image_evidence_sha256",
        "api_identity_sha256",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or type(value.get("slot_id")) is not str
        or not SLOT_RE.fullmatch(value["slot_id"])
        or value.get("kind")
        not in {
            "trusted_release",
            "adopted_pre_update_snapshot",
            "initial_install_snapshot",
        }
        or type(value.get("official_source_match")) is not bool
        or type(value.get("version")) is not str
        or not VERSION_RE.fullmatch(value["version"])
        or (
            value.get("commit") is not None
            and (
                type(value["commit"]) is not str
                or not COMMIT_RE.fullmatch(value["commit"])
            )
        )
        or any(
            type(value.get(key)) is not str
            or not DIGEST_RE.fullmatch(value[key])
            for key in (
                "manifest_sha256",
                "inventory_sha256",
                "compose_plan_sha256",
                "image_evidence_sha256",
                "api_identity_sha256",
            )
        )
        or (
            value.get("archive_override_sha256") is not None
            and (
                type(value["archive_override_sha256"]) is not str
                or not DIGEST_RE.fullmatch(
                    value["archive_override_sha256"]
                )
            )
        )
    ):
        raise SlotError(
            "activation_slot_binding_invalid",
            "Activation slot binding is invalid.",
        )
    if (
        value["kind"] == "trusted_release"
        and (
            not TRUSTED_SLOT_RE.fullmatch(value["slot_id"])
            or value["official_source_match"] is not True
        )
    ) or (
        value["kind"] == "adopted_pre_update_snapshot"
        and (
            not ADOPTED_SLOT_RE.fullmatch(value["slot_id"])
            or value["official_source_match"] is not False
        )
    ) or (
        value["kind"] == "initial_install_snapshot"
        and (
            not INITIAL_SLOT_RE.fullmatch(value["slot_id"])
            or value["official_source_match"] is not False
            or value["commit"] is not None
        )
    ):
        raise SlotError(
            "activation_slot_binding_invalid",
            "Activation slot binding contradicts slot identity.",
        )
    return json.loads(json.dumps(value))


def validate_activation_journal(value: Any) -> dict[str, Any]:
    expected = {
        "schema_version",
        "document_type",
        "request_id",
        "generation",
        "phase",
        "previous",
        "target",
        "schema",
        "pointer_slot_id",
        "target_verified",
        "previous_verified",
        "rollback_trigger",
        "failure_category",
        "created_at",
        "updated_at",
        "terminal_at",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_version") != JOURNAL_SCHEMA_VERSION
        or value.get("document_type") != "release_slot_activation"
        or type(value.get("request_id")) is not str
        or not REQUEST_RE.fullmatch(value["request_id"])
        or type(value.get("generation")) is not int
        or isinstance(value["generation"], bool)
        or value["generation"] < 1
        or value.get("phase") not in ACTIVATION_PHASES
        or type(value.get("schema")) is not dict
        or set(value["schema"])
        != {
            "compatibility",
            "compatibility_sha256",
            "source_version",
            "target_version",
            "migration_required",
            "migration_invoked",
            "migration_completed",
            "migration_attempt_id",
        }
        or value["schema"].get("compatibility") != "compatible"
        or type(value["schema"].get("compatibility_sha256")) is not str
        or not DIGEST_RE.fullmatch(
            value["schema"]["compatibility_sha256"]
        )
        or type(value["schema"].get("source_version")) is not int
        or isinstance(value["schema"]["source_version"], bool)
        or value["schema"]["source_version"] < 0
        or type(value["schema"].get("target_version")) is not int
        or isinstance(value["schema"]["target_version"], bool)
        or value["schema"]["target_version"] < 0
        or value["schema"]["source_version"]
        > value["schema"]["target_version"]
        or type(value["schema"].get("migration_required")) is not bool
        or type(value["schema"].get("migration_invoked")) is not bool
        or type(value["schema"].get("migration_completed")) is not bool
        or (
            value["schema"].get("migration_attempt_id") is not None
            and (
                type(value["schema"]["migration_attempt_id"]) is not str
                or not re.fullmatch(
                    r"^migration-attempt-[0-9a-f]{32}$",
                    value["schema"]["migration_attempt_id"],
                )
            )
        )
        or type(value.get("target_verified")) is not bool
        or type(value.get("previous_verified")) is not bool
        or type(value.get("created_at")) is not str
        or not value["created_at"]
        or type(value.get("updated_at")) is not str
        or not value["updated_at"]
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal is invalid.",
        )
    previous = validate_activation_slot_binding(value.get("previous"))
    target = validate_activation_slot_binding(value.get("target"))
    if (
        previous["slot_id"] == target["slot_id"]
        or target["kind"] != "trusted_release"
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal slot roles are contradictory.",
        )
    pointer_slot_id = value.get("pointer_slot_id")
    if pointer_slot_id is not None and pointer_slot_id not in {
        previous["slot_id"],
        target["slot_id"],
    }:
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal pointer evidence is contradictory.",
        )
    failure_category = value.get("failure_category")
    if failure_category is not None and (
        type(failure_category) is not str
        or not MACHINE_CODE_RE.fullmatch(failure_category)
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal failure category is invalid.",
        )
    rollback_trigger = value.get("rollback_trigger")
    if rollback_trigger is not None and (
        type(rollback_trigger) is not str
        or not MACHINE_CODE_RE.fullmatch(rollback_trigger)
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal rollback trigger is invalid.",
        )
    if rollback_trigger is not None and value["phase"] not in {
        "rolling_back",
        "failed_rolled_back",
        "blocked",
    }:
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal rollback trigger is contradictory.",
        )
    terminal_at = value.get("terminal_at")
    terminal = value["phase"] in TERMINAL_ACTIVATION_PHASES
    if terminal != (type(terminal_at) is str and bool(terminal_at)):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal terminal evidence is contradictory.",
        )
    schema = value["schema"]
    if (
        schema["migration_completed"]
        and (
            not schema["migration_required"]
            or not schema["migration_invoked"]
        )
    ) or (
        not schema["migration_required"]
        and (
            schema["migration_invoked"]
            or schema["migration_completed"]
        )
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal schema evidence is contradictory.",
        )
    if value["target_verified"] and value["phase"] in {
        "target_prepared",
        "quiescing",
        "schema_preparing",
        "activating",
    }:
        raise SlotError(
            "activation_journal_invalid",
            "Target verification evidence is contradictory.",
        )
    if value["previous_verified"] != (
        value["phase"] == "failed_rolled_back"
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Previous verification evidence is contradictory.",
        )
    if value["phase"] == "completed" and (
        not value["target_verified"]
        or pointer_slot_id != target["slot_id"]
        or failure_category is not None
        or rollback_trigger is not None
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Completed activation evidence is contradictory.",
        )
    if value["phase"] == "failed_rolled_back" and (
        pointer_slot_id != previous["slot_id"]
        or failure_category is None
        or rollback_trigger is None
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Rollback terminal evidence is contradictory.",
        )
    if value["phase"] == "blocked" and failure_category is None:
        raise SlotError(
            "activation_journal_invalid",
            "Blocked activation lacks a bounded failure category.",
        )
    normalized = json.loads(json.dumps(value))
    normalized["previous"] = previous
    normalized["target"] = target
    return normalized
