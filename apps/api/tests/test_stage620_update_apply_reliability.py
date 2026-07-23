import json
import importlib.util
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps/api"))

from app.core.config import settings
from app.core.version import APP_VERSION
from app.services import update_apply as update_apply_module
from app.services.update_apply import read_update_apply_status
from app.services.update_check import read_installed_update_state


def load_update_helper_module():
    spec = importlib.util.spec_from_file_location("km_vms_update_helper_stage620", ROOT / "scripts/km-vms-update-helper.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_stage620_dockerfile_bounds_jellyfin_ffmpeg_network_install():
    text = (ROOT / "apps/api/Dockerfile").read_text(encoding="utf-8")

    assert "curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \\\n      | gpg" not in text
    assert "repo.jellyfin.org/jellyfin_team.gpg.key -o \"$jellyfin_key\"" in text
    assert "-4 --http1.1 --connect-timeout" in text
    assert "--max-time" in text
    assert "--retry " in text
    assert "--retry-delay" in text
    assert "--retry-all-errors" in text
    assert "[ -s \"$jellyfin_key\" ]" in text
    assert "Acquire::ForceIPv4=true" in text
    assert "Acquire::Retries=4" in text
    assert "Acquire::http::Timeout=30" in text
    assert "Acquire::https::Timeout=30" in text
    assert "jellyfin-ffmpeg7" in text
    assert "ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg" in text
    assert "ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe" in text


def test_stage620_update_script_writes_sanitized_progress_side_channel():
    text = (ROOT / "scripts/update.sh").read_text(encoding="utf-8")

    assert "KM_VMS_UPDATE_PROGRESS_FILE" in text
    for phase in ["acquire_source", "extracting", "validating_source", "overlay", "compose_config", "rebuilding", "health_check", "commit_verification"]:
        assert phase in text
    assert "write_helper_progress" in text
    assert "raw" not in text.lower()


def test_stage620_helper_uses_popen_polling_and_progress_heartbeat():
    text = (ROOT / "scripts/km-vms-update-helper.py").read_text(encoding="utf-8")

    assert "subprocess.Popen" in text
    assert "stderr=subprocess.PIPE" not in text
    assert "NamedTemporaryFile" in text
    assert "read_stderr_tail" in text
    assert "subprocess.run(common" not in text
    assert "KM_VMS_UPDATE_PROGRESS_FILE" in text
    assert "last_progress_age_seconds" not in text
    assert "read_progress" in text
    assert "steps_for" in text


def test_stage621_helper_large_stderr_does_not_deadlock_and_returns_tail(tmp_path, monkeypatch):
    helper = load_update_helper_module()
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(helper, "STATUS_FILE", control / "update-status.json")
    monkeypatch.setattr(helper, "PROGRESS_FILE", control / "update-progress.json")
    monkeypatch.setattr(helper, "POLL_SECONDS", 0.01)
    request = {
        "request_id": "stage621-large-stderr",
        "requested_at": "2026-06-28T00:00:00Z",
        "source": {"commit": "a" * 40, "repo": "owner/repo", "ref": "main", "apply_ref": "a" * 40},
    }

    result = helper.run_child_with_progress(
        [sys.executable, "-c", "import sys; sys.stderr.write('x' * 2000000); sys.exit(7)"],
        request,
        tmp_path,
        os.environ.copy(),
        timeout_seconds=10,
        default_step="rebuilding",
        status_value="applying",
    )

    assert result.returncode == 7
    assert 0 < len(result.stderr) <= 1200
    assert set(result.stderr) == {"x"}


def test_stage621_helper_timeout_is_bounded_and_safe(tmp_path, monkeypatch):
    helper = load_update_helper_module()
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(helper, "STATUS_FILE", control / "update-status.json")
    monkeypatch.setattr(helper, "PROGRESS_FILE", control / "update-progress.json")
    monkeypatch.setattr(helper, "POLL_SECONDS", 0.01)
    request = {
        "request_id": "stage621-timeout",
        "requested_at": "2026-06-28T00:00:00Z",
        "source": {"commit": "a" * 40, "repo": "owner/repo", "ref": "main", "apply_ref": "a" * 40},
    }

    try:
        helper.run_child_with_progress(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            request,
            tmp_path,
            os.environ.copy(),
            timeout_seconds=0.05,
            default_step="rebuilding",
            status_value="applying",
        )
    except helper.HelperError as exc:
        assert exc.category == "apply_timeout"
        assert exc.phase == "rebuilding"
    else:
        raise AssertionError("timeout was expected")


def test_stage621_timeout_uses_last_known_progress_step(tmp_path, monkeypatch):
    helper = load_update_helper_module()
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(helper, "STATUS_FILE", control / "update-status.json")
    monkeypatch.setattr(helper, "PROGRESS_FILE", control / "update-progress.json")
    monkeypatch.setattr(helper, "POLL_SECONDS", 0.01)
    request = {
        "request_id": "stage621-progress-timeout",
        "requested_at": "2026-06-28T00:00:00Z",
        "source": {"commit": "a" * 40, "repo": "owner/repo", "ref": "main", "apply_ref": "a" * 40},
    }
    (control / "update-progress.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request["request_id"],
                "status": "running",
                "phase": "rebuild_recreate",
                "current_step": "rebuilding",
                "updated_at": "2026-06-28T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    try:
        helper.run_child_with_progress(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            request,
            tmp_path,
            os.environ.copy(),
            timeout_seconds=0.05,
            default_step="acquire_source",
            status_value="applying",
        )
    except helper.HelperError as exc:
        assert exc.category == "apply_timeout"
        assert exc.phase == "rebuilding"
        mapped = {step["name"]: step["status"] for step in helper.failed_steps(exc.category, exc.phase)}
        assert mapped["rebuilding"] == "failed"
        assert mapped["acquire_source"] == "completed"
    else:
        raise AssertionError("timeout was expected")


def test_stage620_helper_classifies_jellyfin_and_build_network_failures(tmp_path):
    (tmp_path / ".km-vms-update.json").write_text(
        json.dumps({"schema_version": 1, "status": "failed", "failed_phase": "rebuild_recreate", "error_message": "repo.jellyfin.org jellyfin_team.gpg.key timed out"}),
        encoding="utf-8",
    )

    helper = load_update_helper_module()
    exc = helper.classify_apply_failure(tmp_path, "curl timeout while downloading Jellyfin key")

    assert exc.category == "jellyfin_ffmpeg_repo_unavailable"
    assert "Jellyfin FFmpeg" in str(exc)
    assert "VPN" not in str(exc)


def test_stage621_failure_timelines_and_operator_actions_are_product_safe():
    helper = load_update_helper_module()

    def statuses(category, phase=None):
        return {step["name"]: step["status"] for step in helper.failed_steps(category, phase)}

    for category in ["jellyfin_ffmpeg_repo_unavailable", "build_network_dependency_failed", "docker_build_failed"]:
        mapped = statuses(category)
        assert mapped["compose_config"] == "completed"
        assert mapped["rebuilding"] == "failed"
        assert mapped["health_check"] == "pending"
    assert statuses("compose_config_failed")["compose_config"] == "failed"
    assert statuses("health_check_failed")["health_check"] == "failed"
    assert statuses("commit_mismatch")["commit_verification"] == "failed"
    assert statuses("metadata_invalid")["commit_verification"] == "failed"
    assert statuses("apply_timeout", "rebuilding")["rebuilding"] == "failed"

    action = helper.error_payload("jellyfin_ffmpeg_repo_unavailable", "failed")["operator_action"]
    assert "VPN" not in action
    assert "WireGuard" not in action
    assert "stage" not in action.lower()
    assert "github_pat_" not in action


def test_stage621_update_apply_request_id_uses_neutral_prefix():
    text = (ROOT / "apps/api/app/services/update_apply.py").read_text(encoding="utf-8")

    assert 'request_id = "update-" + uuid.uuid4().hex' in text
    assert 'request_id = "stage609-" + uuid.uuid4().hex' not in text


def test_stage622_helper_writes_bounded_sanitized_apply_history(tmp_path, monkeypatch):
    helper = load_update_helper_module()
    history = tmp_path / "update-apply-history.json"
    monkeypatch.setattr(helper, "APPLY_HISTORY_FILE", history)

    for index in range(12):
        helper.append_apply_history(
            {
                "request_id": f"update-{index}",
                "status": "completed",
                "phase": "completed",
                "started_at": "2026-07-02T00:00:00Z",
                "updated_at": "2026-07-02T00:01:00Z",
                "expected_commit": "a" * 40,
                "installed_commit": "a" * 40,
                "commit_verified": True,
                "source": {"kind": "github-tarball", "repo": "owner/repo", "ref": "v0.7.4", "commit": "a" * 40, "apply_ref": "a" * 40},
                "steps": [{"name": "commit_verification", "status": "completed"}],
            }
        )

    payload = json.loads(history.read_text(encoding="utf-8"))

    assert payload["max_items"] == 10
    assert len(payload["items"]) == 10
    assert payload["items"][0]["request_id"] == "update-2"
    assert payload["items"][-1]["history_detail_status"] == "step_timestamps_unavailable"
    assert "github_pat_" not in history.read_text(encoding="utf-8")


def test_stage622_api_exposes_last_apply_summary_without_fake_step_times(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.mkdir()
    (control / "update-apply-history.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "max_items": 10,
                "items": [
                    {
                        "request_id": "update-" + "6" * 32,
                        "status": "completed",
                        "phase": "completed",
                        "started_at": "2026-07-02T00:00:00Z",
                        "finished_at": "2026-07-02T00:04:00Z",
                        "updated_at": "2026-07-02T00:04:00Z",
                        "expected_commit": "a" * 40,
                        "installed_commit": "a" * 40,
                        "commit_verified": True,
                        "steps": [{"name": "commit_verification", "status": "completed"}],
                        "history_detail_status": "step_timestamps_unavailable",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "update_control_root", str(control))

    status = read_update_apply_status()

    assert status["status"] == "idle"
    assert status["apply_history"]["available"] is True
    assert status["last_apply_summary"]["request_id"] == "update-" + "6" * 32
    assert status["last_apply_summary"]["history_detail_status"] == "step_timestamps_unavailable"
    assert "time_label" not in status["last_apply_summary"]["steps"][0]


def test_stage620_api_status_derives_stale_running_status(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.mkdir()
    stale_time = (datetime.utcnow() - timedelta(seconds=600)).isoformat() + "Z"
    (control / "update-status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": "stage620",
                "status": "rebuilding",
                "phase": "rebuild_recreate",
                "current_step": "rebuilding",
                "started_at": stale_time,
                "updated_at": stale_time,
                "steps": [{"name": "rebuilding", "status": "running"}],
                "expected_commit": "a" * 40,
                "commit_verified": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "update_control_root", str(control))

    status = read_update_apply_status()

    assert status["status"] == "rebuilding"
    assert status["effective_status"] == "stalled"
    assert status["is_stale"] is True
    assert status["last_progress_age_seconds"] >= status["stale_after_seconds"]


def test_stage620_precompose_release_identity_is_not_verified_success(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / ".km-vms-release.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "KM VMS",
                "version": "0.7.4",
                "commit_sha": "a" * 40,
                "metadata_status": "precompose",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))

    installed = read_installed_update_state(app_root=app_root)

    assert installed.status == "identity_incomplete"
    assert installed.identity_validity == "precompose"
    assert any(item.code == "release_identity_precompose" for item in installed.warnings)


def test_stage620_scripts_compile_and_release_descriptor_targets_current_release():
    subprocess.run(["sh", "-n", "scripts/update.sh"], cwd=ROOT, check=True)
    fd, cfile = tempfile.mkstemp(suffix=".pyc")
    os.close(fd)
    try:
        subprocess.run(["python", "-m", "py_compile", "-q", "scripts/km-vms-update-helper.py"], cwd=ROOT, env={**os.environ, "PYTHONPYCACHEPREFIX": str(Path(cfile).parent)}, check=True)
    finally:
        Path(cfile).unlink(missing_ok=True)
    descriptor = json.loads((ROOT / "release/km-vms-release.json").read_text(encoding="utf-8"))

    assert descriptor["version"] == APP_VERSION
    assert descriptor["tag"] == f"v{APP_VERSION}"
    assert descriptor["source_ref"] == f"v{APP_VERSION}"
