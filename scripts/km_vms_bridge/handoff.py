"""Handoff and helper scheduling under explicit authority callbacks."""
from __future__ import annotations
import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from .contracts import COMMIT_SHA_RE, BridgeError, utcnow

from .requests import require_app_dir, require_request_id, require_project_name


@dataclass(frozen=True)
class BridgeHandoffExecutor:
    _run_permission_contract: Callable
    bind_legacy_adopted_slot_as_active: Callable
    capture_installed_source_identity: Callable
    capture_slot_runtime_binding: Callable
    load_slot_engine: Callable
    load_source_bootstrap: Callable
    normalize_archive_roots_override: Callable
    prepare_legacy_adopted_slot: Callable
    run_command: Callable
    inspected_container_image: Callable

    def _permission_gate_supports_contract(self, gate: Path) -> bool:
        if gate.is_symlink() or not gate.is_file():
            raise BridgeError(
                "permission_gate_missing",
                "Installed permission authority is unavailable.",
            )
        try:
            return "--contract" in gate.read_text(
                encoding="utf-8",
                errors="strict",
            )
        except (OSError, UnicodeError) as exc:
            raise BridgeError(
                "permission_gate_invalid",
                "Installed permission authority cannot be inspected.",
            ) from exc

    def _installed_entry_supports_project_handoff(self,
        app_dir: Path,
    ) -> bool:
        pointer = app_dir / "data/update-runtime/active"
        update_script = app_dir / "scripts/update.sh"
        try:
            if pointer.is_symlink():
                target = os.readlink(pointer)
                if re.fullmatch(
                    r"slots/(?:release-[0-9a-f]{40}|adopted-[0-9a-f]{64}|initial-[0-9a-f]{64})/source",
                    target,
                ):
                    update_script = app_dir / "data/update-runtime" / target / "scripts/update.sh"
            if update_script.is_symlink() or not update_script.is_file():
                return False
            text = update_script.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            return False
        return (
            "resolved_project_name=" in text
            and "KM_VMS_PROJECT_NAME=\"$PROJECT_NAME\"" in text
        )

    def classify_installed_update_contract(self,
        app_dir: Path,
        active: tuple[str, Path] | None,
        *,
        engine: Any,
    ) -> str:
        projection_path = (
            app_dir
            / "data/update-runtime/installed-projection/installed-slot.json"
        )
        if active is None:
            if projection_path.exists() or projection_path.is_symlink():
                raise BridgeError(
                    "installed_contract_unknown",
                    "Legacy root contradicts installed-slot projection evidence.",
                )
            return "legacy_root"

        slot_id, active_source = active
        gate = active_source / "scripts/km-vms-permission-gate.sh"
        family = (
            "current_contract_slot"
            if self._permission_gate_supports_contract(gate)
            else "pre_contract_slot"
        )
        if projection_path.exists() or projection_path.is_symlink():
            try:
                projection = engine.validate_installed_slot_projection(app_dir)
            except Exception as exc:
                raise BridgeError(
                    getattr(exc, "code", "installed_projection_invalid"),
                    "Installed-slot projection contradicts the active slot.",
                ) from exc
            if projection.get("slot_id") != slot_id:
                raise BridgeError(
                    "installed_projection_invalid",
                    "Installed-slot projection contradicts the active slot.",
                )
        elif family == "current_contract_slot":
            raise BridgeError(
                "installed_projection_missing",
                "Current update contract requires installed-slot projection evidence.",
            )
        return family

    def install_stable_bootstrap_for_handoff(self,
        app_dir: Path,
        target_source_dir: Path,
        *,
        engine: Any,
        active_source: Path | None,
        contract_family: str,
    ) -> Any:
        """Perform the bounded pre-emergency-to-stable-bootstrap migration."""

        target_gate = target_source_dir / "scripts/km-vms-permission-gate.sh"
        self._run_permission_contract(
            target_gate,
            target_source_dir,
            contract="source",
            action="check",
        )
        if contract_family == "current_contract_slot" and active_source is not None:
            current_gate = active_source / "scripts/km-vms-permission-gate.sh"
            self._run_permission_contract(
                current_gate,
                active_source,
                contract="source",
                action="check",
                compatibility=True,
            )
        elif contract_family == "pre_contract_slot" and active_source is not None:
            # The immutable slot manifest/source identity is the bounded authority
            # for this family.  Its historical gate may require owner-write access,
            # so invoking it would either fail or force mutation of immutable source.
            self._permission_gate_supports_contract(
                active_source / "scripts/km-vms-permission-gate.sh"
            )
        elif contract_family == "legacy_root" and active_source is None:
            legacy_gate = app_dir / "scripts/km-vms-permission-gate.sh"
            self._run_permission_contract(
                legacy_gate,
                app_dir,
                contract="legacy",
                action="check",
                compatibility=True,
            )
        else:
            raise BridgeError(
                "installed_contract_unknown",
                "Installed update contract cannot be classified safely.",
            )
        try:
            engine.ensure_layout(app_dir)
        except Exception as exc:
            raise BridgeError(
                getattr(exc, "code", "slot_layout_invalid"),
                "Canonical slot layout could not be prepared.",
            ) from exc
        self._run_permission_contract(
            target_gate,
            app_dir,
            contract="stable-prebootstrap",
            action="fix",
        )
        bootstrap = self.load_source_bootstrap(target_source_dir)
        try:
            bootstrap.install_bundle(app_dir, target_source_dir)
        except Exception as exc:
            raise BridgeError(
                getattr(exc, "code", "stable_bootstrap_install_failed"),
                "Stable bootstrap migration failed.",
            ) from exc
        return bootstrap

    def handoff(self, args: argparse.Namespace) -> int:
        """Bind one admitted request to its exact current release."""

        app_dir = require_app_dir(args.app_dir)
        target_source_input = Path(args.target_source_dir)
        try:
            target_source_input.lstat()
            target_source_dir = target_source_input.resolve(strict=True)
        except OSError as exc:
            raise BridgeError(
                "target_source_invalid",
                "The staged target source is unavailable or incomplete.",
            ) from exc
        if (
            target_source_input.is_symlink()
            or not target_source_dir.is_dir()
            or target_source_dir.is_symlink()
            or not (
                target_source_dir / "release/km-vms-release.json"
            ).is_file()
        ):
            raise BridgeError(
                "target_source_invalid",
                "The staged target source is unavailable or incomplete.",
            )
        request_id = require_request_id(args.request_id)
        supplied_project_name = (
            str(getattr(args, "project_name", None) or "").strip()
            or os.getenv("KM_VMS_PROJECT_NAME", "").strip()
        )
        if (
            not supplied_project_name
            and not self._installed_entry_supports_project_handoff(app_dir)
        ):
            raise BridgeError(
                "project_identity_recovery_required",
                "The frozen installed updater cannot receive a resolved Compose project identity before its remaining Compose calls.",
            )
        target_bootstrap = self.load_source_bootstrap(target_source_dir)
        try:
            project_name = require_project_name(
                target_bootstrap.read_project_name(
                    app_dir,
                    supplied_project_name or None,
                )
            )
        except Exception as exc:
            raise BridgeError(
                getattr(exc, "code", "project_identity_invalid"),
                "Compose project identity could not be resolved safely.",
            ) from exc
        engine = self.load_slot_engine(target_source_dir)
        try:
            active = engine.read_active_slot(app_dir)
        except Exception as exc:
            raise BridgeError(
                getattr(exc, "code", "active_pointer_invalid"),
                "Active release pointer is invalid.",
            ) from exc
        contract_family = self.classify_installed_update_contract(
            app_dir,
            active,
            engine=engine,
        )
        bootstrap = self.install_stable_bootstrap_for_handoff(
            app_dir,
            target_source_dir,
            engine=engine,
            active_source=(active[1] if active is not None else None),
            contract_family=contract_family,
        )
        archive_override_changed = self.normalize_archive_roots_override(app_dir)
        terminal_request: dict[str, Any] | None = None
        if bool(getattr(args, "terminal", False)):
            trusted_commit = str(
                getattr(args, "trusted_commit", None) or ""
            ).lower()
            declared_version = str(
                getattr(args, "declared_version", None) or ""
            )
            if (
                not COMMIT_SHA_RE.fullmatch(trusted_commit)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}",
                    declared_version,
                )
            ):
                raise BridgeError(
                    "terminal_request_invalid",
                    "Terminal update target identity is invalid.",
                )
            terminal_request = {
                "schema_version": 1,
                "request_id": request_id,
                "requested_at": utcnow(),
                "intent": "apply_update",
                "confirmed": True,
                "source": {
                    "version": declared_version,
                    "commit": trusted_commit,
                },
            }
        if active is not None:
            previous_slot_id, active_source = active
            installed_identity = self.capture_installed_source_identity(
                app_dir,
                request_id=request_id,
                target_source_dir=target_source_dir,
                installed_source_dir=active_source,
                request_override=terminal_request,
            )
            previous_binding = self.capture_slot_runtime_binding(
                app_dir,
                project_name,
                previous_slot_id,
                engine=engine,
                require_http=True,
                require_helper_image=True,
            )
            slot_result = previous_slot_id
            handoff_kind = "active_slot"
        else:
            installed_identity = self.capture_installed_source_identity(
                app_dir,
                request_id=request_id,
                target_source_dir=target_source_dir,
                request_override=terminal_request,
            )
            slot_result = self.prepare_legacy_adopted_slot(
                app_dir=app_dir,
                target_source_dir=target_source_dir,
                request_id=request_id,
                installed_identity=installed_identity,
                project_name=project_name,
            )
            self.bind_legacy_adopted_slot_as_active(
                app_dir=app_dir,
                slot_id=slot_result,
                engine=engine,
            )
            previous_binding = self.capture_slot_runtime_binding(
                app_dir,
                project_name,
                slot_result,
                engine=engine,
                require_http=True,
                require_helper_image=True,
            )
            handoff_kind = "legacy_adoption"
        try:
            engine.publish_installed_slot_projection(
                app_dir,
                binding=previous_binding,
            )
            self._run_permission_contract(
                target_source_dir / "scripts/km-vms-permission-gate.sh",
                app_dir,
                contract="stable-runtime",
                action="fix",
            )
            bootstrap.reconcile_restart_policies(
                app_dir,
                project_name,
            )
        except Exception as exc:
            raise BridgeError(
                getattr(exc, "code", "stable_runtime_migration_failed"),
                "Stable runtime migration did not converge.",
            ) from exc
        print("schema_handoff=PASS")
        print(f"schema_handoff_request_id={request_id}")
        print(
            "archive_roots_override="
            + ("normalized" if archive_override_changed else "unchanged")
        )
        print(f"handoff_kind={handoff_kind}")
        print(f"update_contract_family={contract_family}")
        print(f"resolved_project_name={project_name}")
        print(
            "activation_owner="
            + ("terminal_lock" if terminal_request is not None else "update_helper")
        )
        print(f"previous_slot={slot_result}")
        if handoff_kind == "legacy_adoption":
            print(f"adopted_slot={slot_result}")
        return 0

    def schedule_refresh(self,
        *,
        app_dir: Path,
        project_name: str,
        helper_image: str,
        request_id: str,
        expected_image_id: str,
        timeout_seconds: int,
        bridge_script: Path | None = None,
        target_slot: str | None = None,
    ) -> str:
        coordinator_name = f"km-vms-helper-refresh-{request_id.removeprefix('update-').lower()}"
        existing_image_id = self.inspected_container_image(coordinator_name)
        if existing_image_id is not None:
            if existing_image_id != expected_image_id:
                raise BridgeError("coordinator_image_mismatch", "Existing helper refresh coordinator uses another image.")
            return "already_scheduled"

        script_path = bridge_script or (
            app_dir / "scripts/km-vms-update-helper-bridge.py"
        )
        if (
            not script_path.is_absolute()
            or script_path.is_symlink()
            or not script_path.is_file()
            or not script_path.resolve().is_relative_to(app_dir.resolve())
        ):
            raise BridgeError(
                "coordinator_bridge_invalid",
                "Helper refresh bridge is unavailable or unsafe.",
            )
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
            "-B",
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
        if target_slot is not None:
            command.extend(["--target-slot", target_slot])
        result = self.run_command(
            command,
            timeout=60,
            error_code="coordinator_start_failed",
            error_message="Cannot start the detached update-helper refresh coordinator.",
            check=False,
        )
        if result.returncode == 0:
            return "scheduled"

        # A concurrent bootstrap may have won the atomic Docker container-name race.
        existing_image_id = self.inspected_container_image(coordinator_name)
        if existing_image_id == expected_image_id:
            return "already_scheduled"
        raise BridgeError("coordinator_start_failed", "Cannot start the detached update-helper refresh coordinator.")
