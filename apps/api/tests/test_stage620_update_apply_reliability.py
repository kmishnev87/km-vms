import json
import inspect
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
    text = inspect.getsource(load_update_helper_module().run_child_with_progress)

    assert "subprocess.Popen" in text
    assert "stderr=subprocess.PIPE" not in text
    assert "NamedTemporaryFile" in text
    assert "read_stderr_tail" in text
    assert "subprocess.run(common" not in text
    assert "POLL_SECONDS" in text
    assert "read_progress" in text
    assert "steps_for" in text


def test_stage620_public_progress_projection_is_bounded_and_consistent():
    for percent in (0, 42, 100):
        assert update_apply_module._safe_progress(
            {
                "progress_percent": percent,
                "progress_current": percent,
                "progress_total": 100,
                "progress_unit": "bytes",
            }
        )["progress_percent"] == percent

    for payload in (
        {"progress_percent": -1, "progress_current": 0, "progress_total": 100, "progress_unit": "bytes"},
        {"progress_percent": 101, "progress_current": 100, "progress_total": 100, "progress_unit": "bytes"},
        {"progress_percent": float("nan"), "progress_current": 1, "progress_total": 2, "progress_unit": "bytes"},
        {"progress_percent": 50, "progress_current": 42, "progress_total": 100, "progress_unit": "bytes"},
    ):
        sanitized = update_apply_module._safe_progress(payload)
        assert sanitized["progress_percent"] is None
        assert sanitized["progress_total"] is None


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
        "source": {"commit": "a" * 40, "repo": "owner/repo", "ref": "main", "apply_ref": "a" * 40, "version":"0.8.29", "channel":"stable", "source_type":"github_tarball"},
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
        "source": {"commit": "a" * 40, "repo": "owner/repo", "ref": "main", "apply_ref": "a" * 40, "version":"0.8.29", "channel":"stable", "source_type":"github_tarball"},
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
        "request_id": "update-" + "8" * 32,
        "requested_at": "2026-06-28T00:00:00Z",
        "source": {"commit": "a" * 40, "repo": "owner/repo", "ref": "main", "apply_ref": "a" * 40, "version":"0.8.29", "channel":"stable", "source_type":"github_tarball"},
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
        mapped = {step["name"]: step["status"] for step in helper.steps_for(exc.phase, failed=True)}
        assert mapped["applying"] == "failed"
        assert mapped["preflight"] == "completed"
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
    assert "FFmpeg repository" in str(exc)
    assert "VPN" not in str(exc)


def test_stage621_failure_timelines_and_operator_actions_are_product_safe():
    helper = load_update_helper_module()

    def statuses(category, phase=None):
        step = phase or ("health_check" if category == "health_check_failed" else "commit_verification" if category in {"commit_mismatch", "metadata_invalid"} else "rebuilding")
        return {item["name"]: item["status"] for item in helper.steps_for(step, failed=True)}

    for category in [
        "jellyfin_ffmpeg_repo_unavailable",
        "build_network_dependency_failed",
        "docker_build_failed",
        "schema_update_failed",
    ]:
        mapped = statuses(category)
        assert mapped["preflight"] == "completed"
        assert mapped["applying"] == "failed"
        assert mapped["health_check"] == "pending"
    assert statuses("compose_config_failed")["applying"] == "failed"
    assert statuses("health_check_failed")["health_check"] == "failed"
    assert statuses("commit_mismatch")["commit_verification"] == "failed"
    assert statuses("metadata_invalid")["commit_verification"] == "failed"
    assert statuses("apply_timeout", "rebuilding")["applying"] == "failed"

    action = helper.error_payload("jellyfin_ffmpeg_repo_unavailable")["operator_action"]
    assert "VPN" not in action
    assert "WireGuard" not in action
    assert "stage" not in action.lower()
    assert "github_pat_" not in action


def test_stage621_update_apply_request_id_uses_neutral_prefix():
    value = update_apply_module._request_id('00000000-0000-4000-8000-000000000001','a'*40)
    assert value.startswith('update-') and len(value)==39
    assert update_apply_module._request_id('00000000-0000-4000-8000-000000000001','a'*40)==value
    assert update_apply_module._request_id('00000000-0000-4000-8000-000000000002','a'*40)!=value


def test_stage622_helper_writes_bounded_sanitized_apply_history(tmp_path, monkeypatch):
    helper = load_update_helper_module()
    history = tmp_path / "update-apply-history.json"
    monkeypatch.setattr(helper, "APPLY_HISTORY_FILE", history)

    for index in range(12):
        helper.append_last_history(
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

    assert payload["max_items"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["request_id"] == "update-11"
    assert "time_label" not in payload["items"][0]["steps"][0]
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
    monkeypatch.setattr(update_apply_module, '_helper_lease_active', lambda: True)
    control = tmp_path / "control"
    control.mkdir()
    stale_time = (datetime.utcnow() - timedelta(seconds=600)).isoformat() + "Z"
    (control / "update-status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": "update-" + "7" * 32,
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
