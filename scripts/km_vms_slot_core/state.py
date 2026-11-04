"""One owner of the active pointer, activation journal and projection checks."""
from __future__ import annotations
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from .contracts import JOURNAL_SCHEMA_VERSION, INSTALLED_PROJECTION_RELATIVE, SOURCE_DIR_NAME, DIGEST_RE, SLOT_RE, MACHINE_CODE_RE, ACTIVATION_PHASES, TERMINAL_ACTIVATION_PHASES, ACTIVATION_TRANSITIONS, SlotError, utc_now

from .description import (
    _payload_digest, validate_activation_slot_binding, validate_activation_journal,
)


@dataclass(frozen=True)
class SlotStateStore:
    ensure_layout: Callable
    resolve_app_dir: Callable
    require_slot_id: Callable
    require_request_id: Callable
    validate_slot: Callable
    _converge_active_slot_legacy_python_cache: Callable
    _fsync_directory: Callable
    read_json: Callable
    atomic_write_json: Callable

    def validate_installed_slot_projection(self,
        app_dir: Path,
        *,
        require_active: bool = True,
    ) -> dict[str, Any]:
        app_dir = self.resolve_app_dir(app_dir)
        path = app_dir / INSTALLED_PROJECTION_RELATIVE
        payload = self.read_json(path)
        assert payload is not None
        expected = {
            "schema_version",
            "document_type",
            "slot_id",
            "kind",
            "identity_mode",
            "version",
            "commit",
            "inventory_sha256",
            "manifest_sha256",
            "active_slot_id",
            "binding",
            "published_at",
        }
        if (
            set(payload) != expected
            or payload.get("schema_version") != 1
            or payload.get("document_type") != "installed_slot_projection"
            or type(payload.get("slot_id")) is not str
            or not SLOT_RE.fullmatch(payload["slot_id"])
            or payload.get("active_slot_id") != payload["slot_id"]
            or type(payload.get("published_at")) is not str
            or not payload["published_at"]
        ):
            raise SlotError(
                "installed_projection_invalid",
                "Installed-slot projection is invalid.",
            )
        binding = validate_activation_slot_binding(payload.get("binding"))
        if binding["slot_id"] != payload["slot_id"]:
            raise SlotError(
                "installed_projection_invalid",
                "Installed-slot projection binding is contradictory.",
            )
        manifest = self.validate_slot(
            self.ensure_layout(app_dir)["slots"] / payload["slot_id"],
            expected_slot_id=payload["slot_id"],
        )
        expected_mode = (
            "inventory_bound"
            if manifest["kind"] == "initial_install_snapshot"
            else "trusted_release"
            if manifest["kind"] == "trusted_release"
            else "adopted_inventory"
        )
        if (
            payload["kind"] != manifest["kind"]
            or payload["identity_mode"] != expected_mode
            or payload["version"] != manifest["declared_identity"]["version"]
            or payload["commit"] != manifest["declared_identity"]["commit"]
            or payload["inventory_sha256"] != manifest["source_inventory"]["sha256"]
            or payload["manifest_sha256"] != _payload_digest(manifest)
            or binding["manifest_sha256"] != payload["manifest_sha256"]
            or binding["inventory_sha256"] != payload["inventory_sha256"]
        ):
            raise SlotError(
                "installed_projection_stale",
                "Installed-slot projection no longer matches its immutable slot.",
            )
        if require_active:
            active = self.read_active_slot(app_dir)
            if active is None or active[0] != payload["slot_id"]:
                raise SlotError(
                    "installed_projection_stale",
                    "Installed-slot projection does not match the active pointer.",
                )
        return json.loads(json.dumps(payload))

    def read_active_slot(self, app_dir: Path) -> tuple[str, Path] | None:
        app_dir = self.resolve_app_dir(app_dir)
        layout = self.ensure_layout(app_dir)
        pointer = layout["active"]
        try:
            info = pointer.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISLNK(info.st_mode):
            raise SlotError("active_pointer_invalid", "Active release pointer is not a symlink.")
        target = os.readlink(pointer)
        parts = Path(target).parts
        if len(parts) != 3 or parts[0] != "slots" or parts[2] != SOURCE_DIR_NAME:
            raise SlotError("active_pointer_invalid", "Active release pointer target is invalid.")
        slot_id = self.require_slot_id(parts[1])
        source = layout["slots"] / slot_id / SOURCE_DIR_NAME
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise SlotError("active_pointer_invalid", "Active release pointer target is unavailable.") from exc
        expected = (layout["slots"] / slot_id / SOURCE_DIR_NAME).resolve(strict=True)
        if resolved != expected:
            raise SlotError("active_pointer_invalid", "Active release pointer escaped its slot.")
        slot_root = layout["slots"] / slot_id
        self._converge_active_slot_legacy_python_cache(
            slot_root,
            expected_slot_id=slot_id,
        )
        self.validate_slot(slot_root, expected_slot_id=slot_id)
        return slot_id, resolved

    def atomic_switch_pointer(self, app_dir: Path, slot_id: str) -> Path:
        """Atomically select one already-validated immutable release slot."""

        app_dir = self.resolve_app_dir(app_dir)
        layout = self.ensure_layout(app_dir)
        slot_id = self.require_slot_id(slot_id)
        self.validate_slot(layout["slots"] / slot_id, expected_slot_id=slot_id)
        replacement = layout["runtime"] / f".active.{uuid.uuid4().hex}.next"
        relative_target = f"slots/{slot_id}/{SOURCE_DIR_NAME}"
        try:
            os.symlink(relative_target, replacement)
            if os.readlink(replacement) != relative_target:
                raise SlotError("active_pointer_prepare_failed", "Active pointer replacement verification failed.")
            self._fsync_directory(layout["runtime"])
            os.replace(replacement, layout["active"])
            self._fsync_directory(layout["runtime"])
        except SlotError:
            replacement.unlink(missing_ok=True)
            raise
        except OSError as exc:
            replacement.unlink(missing_ok=True)
            raise SlotError("active_pointer_switch_failed", "Active release pointer could not be switched atomically.") from exc
        active = self.read_active_slot(app_dir)
        if active is None or active[0] != slot_id:
            raise SlotError("active_pointer_switch_failed", "Active release pointer verification failed.")
        return active[1]

    def read_activation_journal(self,
        app_dir: Path,
        *,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        app_dir = self.resolve_app_dir(app_dir)
        payload = self.read_json(
            self.ensure_layout(app_dir)["journal"],
            missing_ok=missing_ok,
        )
        if payload is None:
            return None
        return validate_activation_journal(payload)

    def initialize_activation_journal(self,
        app_dir: Path,
        *,
        request_id: str,
        previous: dict[str, Any],
        target: dict[str, Any],
        compatibility_sha256: str,
        source_schema_version: int,
        target_schema_version: int,
        migration_required: bool,
        migration_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        app_dir = self.resolve_app_dir(app_dir)
        request_id = self.require_request_id(request_id)
        previous = validate_activation_slot_binding(previous)
        target = validate_activation_slot_binding(target)
        layout = self.ensure_layout(app_dir)
        existing = self.read_activation_journal(app_dir, missing_ok=True)
        if existing is not None:
            if existing["request_id"] == request_id:
                if (
                    existing["previous"] != previous
                    or existing["target"] != target
                    or existing["schema"]["compatibility_sha256"]
                    != compatibility_sha256
                    or existing["schema"]["source_version"]
                    != source_schema_version
                    or existing["schema"]["target_version"]
                    != target_schema_version
                    or existing["schema"]["migration_required"]
                    is not migration_required
                    or existing["schema"]["migration_attempt_id"]
                    != migration_attempt_id
                ):
                    raise SlotError(
                        "activation_journal_conflict",
                        "Existing activation journal contradicts this request.",
                    )
                return existing
            if existing["phase"] not in TERMINAL_ACTIVATION_PHASES:
                raise SlotError(
                    "activation_in_progress",
                    "Another release activation is still active.",
                )
            generation = existing["generation"] + 1
        else:
            generation = 1
        if (
            type(compatibility_sha256) is not str
            or not DIGEST_RE.fullmatch(compatibility_sha256)
            or type(source_schema_version) is not int
            or isinstance(source_schema_version, bool)
            or source_schema_version < 0
            or type(target_schema_version) is not int
            or isinstance(target_schema_version, bool)
            or target_schema_version < source_schema_version
        ):
            raise SlotError(
                "activation_schema_compatibility_invalid",
                "Schema compatibility evidence is invalid.",
            )
        active = self.read_active_slot(app_dir)
        if active is not None and active[0] != previous["slot_id"]:
            raise SlotError(
                "activation_pointer_conflict",
                "Active pointer does not match the captured previous slot.",
            )
        if active is None and previous["kind"] != "adopted_pre_update_snapshot":
            raise SlotError(
                "activation_pointer_missing",
                "A trusted previous slot requires an active pointer.",
            )
        now = utc_now()
        journal = validate_activation_journal(
            {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "document_type": "release_slot_activation",
                "request_id": request_id,
                "generation": generation,
                "phase": "target_prepared",
                "previous": previous,
                "target": target,
                "schema": {
                    "compatibility": "compatible",
                    "compatibility_sha256": compatibility_sha256,
                    "source_version": source_schema_version,
                    "target_version": target_schema_version,
                    "migration_required": migration_required,
                    "migration_invoked": False,
                    "migration_completed": False,
                    "migration_attempt_id": migration_attempt_id,
                },
                "pointer_slot_id": active[0] if active is not None else None,
                "target_verified": False,
                "previous_verified": False,
                "rollback_trigger": None,
                "failure_category": None,
                "created_at": now,
                "updated_at": now,
                "terminal_at": None,
            }
        )
        self.atomic_write_json(layout["journal"], journal)
        return journal

    def transition_activation_journal(self,
        app_dir: Path,
        *,
        request_id: str,
        phase: str,
        pointer_slot_id: str | None = None,
        record_pointer: bool = False,
        migration_invoked: bool | None = None,
        migration_completed: bool | None = None,
        target_verified: bool | None = None,
        previous_verified: bool | None = None,
        rollback_trigger: str | None = None,
        failure_category: str | None = None,
        rollback_database_verified: bool = False,
    ) -> dict[str, Any]:
        app_dir = self.resolve_app_dir(app_dir)
        request_id = self.require_request_id(request_id)
        current = self.read_activation_journal(app_dir)
        assert current is not None
        if current["request_id"] != request_id:
            raise SlotError(
                "activation_request_conflict",
                "Activation journal belongs to another request.",
            )
        if phase not in ACTIVATION_PHASES:
            raise SlotError(
                "activation_transition_invalid",
                "Activation phase is invalid.",
            )
        if (phase == "rolling_back" and current["phase"] in {"target_prepared", "quiescing", "schema_preparing", "blocked"}
            and rollback_database_verified is not True):
            raise SlotError("activation_rollback_proof_required", "Recovery must verify the previous database contract before rollback.")
        if (
            phase != current["phase"]
            and phase not in ACTIVATION_TRANSITIONS[current["phase"]]
        ):
            raise SlotError(
                "activation_transition_invalid",
                "Activation phase transition is not legal.",
            )
        updated = json.loads(json.dumps(current))
        updated["phase"] = phase
        if record_pointer:
            updated["pointer_slot_id"] = (
                self.require_slot_id(pointer_slot_id)
                if pointer_slot_id is not None
                else None
            )
        schema = updated["schema"]
        if migration_invoked is not None:
            if schema["migration_invoked"] and not migration_invoked:
                raise SlotError(
                    "activation_transition_invalid",
                    "Schema invocation evidence cannot move backwards.",
                )
            schema["migration_invoked"] = migration_invoked
        if migration_completed is not None:
            if schema["migration_completed"] and not migration_completed:
                raise SlotError(
                    "activation_transition_invalid",
                    "Schema completion evidence cannot move backwards.",
                )
            schema["migration_completed"] = migration_completed
        if target_verified is not None:
            if updated["target_verified"] and not target_verified:
                raise SlotError(
                    "activation_transition_invalid",
                    "Target verification evidence cannot move backwards.",
                )
            updated["target_verified"] = target_verified
        if previous_verified is not None:
            if updated["previous_verified"] and not previous_verified:
                raise SlotError(
                    "activation_transition_invalid",
                    "Previous verification evidence cannot move backwards.",
                )
            updated["previous_verified"] = previous_verified
        if failure_category is not None:
            if not MACHINE_CODE_RE.fullmatch(failure_category):
                raise SlotError(
                    "activation_transition_invalid",
                    "Activation failure category is invalid.",
                )
            if (
                updated["failure_category"] is not None
                and updated["failure_category"] != failure_category
            ):
                raise SlotError(
                    "activation_transition_invalid",
                    "Activation failure evidence cannot be replaced.",
                )
            updated["failure_category"] = failure_category
        if rollback_trigger is not None:
            if not MACHINE_CODE_RE.fullmatch(rollback_trigger):
                raise SlotError(
                    "activation_transition_invalid",
                    "Activation rollback trigger is invalid.",
                )
            if (
                updated["rollback_trigger"] is not None
                and updated["rollback_trigger"] != rollback_trigger
            ):
                raise SlotError(
                    "activation_transition_invalid",
                    "Activation rollback trigger cannot be replaced.",
                )
            updated["rollback_trigger"] = rollback_trigger
        if phase == "completed":
            updated["failure_category"] = None
            updated["rollback_trigger"] = None
        now = utc_now()
        updated["updated_at"] = now
        updated["terminal_at"] = (
            now if phase in TERMINAL_ACTIVATION_PHASES else None
        )
        validated = validate_activation_journal(updated)
        active = self.read_active_slot(app_dir)
        observed_pointer = active[0] if active is not None else None
        if validated["pointer_slot_id"] != observed_pointer:
            raise SlotError(
                "activation_pointer_conflict",
                "Activation journal pointer evidence does not match the active pointer.",
            )
        self.atomic_write_json(self.ensure_layout(app_dir)["journal"], validated)
        return validated

    def protected_slot_ids(self, app_dir: Path) -> set[str]:
        app_dir = self.resolve_app_dir(app_dir)
        layout = self.ensure_layout(app_dir)
        protected: set[str] = set()
        active = self.read_active_slot(app_dir)
        if active:
            protected.add(active[0])
        journal = self.read_activation_journal(app_dir, missing_ok=True)
        if journal is not None:
            protected.add(journal["previous"]["slot_id"])
            protected.add(journal["target"]["slot_id"])
        return protected
