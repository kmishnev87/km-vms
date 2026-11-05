import importlib.util
import io
from pathlib import Path
import sys
import time
import zipfile

from sqlalchemy import event

from app.models.recording import RecordingSegment
from app.routers import recordings, settings
from test_storage_recording_contract import db
from test_stage14_repeat_archive_reads import seed_history


def helper():
    path = Path(__file__).resolve().parents[3] / "scripts/km_vms_helper_handlers/diagnostics.py"
    spec = importlib.util.spec_from_file_location("audit5_log_collector", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_log_collector_uses_own_project_and_fixed_service_allowlist(monkeypatch):
    module = helper()
    commands = []
    def command(args, **kwargs):
        commands.append(args)
        if args[1] == "ps":
            return "a"*12+"\tapi\n"+"b"*12+"\trecorder\n"+"c"*12+"\tunrelated\n", 0, False
        return "synthetic log\n", 0, False
    monkeypatch.setattr(module, "_bounded_command", command)
    result = module.collect_logs("custom-lab-project", "extended")
    assert "label=com.docker.compose.project=custom-lab-project" in commands[0]
    assert len(commands) == 3
    assert result["api"]["status"] == "available"
    assert result["web"]["status"] == "unavailable"
    assert all(args[1] in {"ps", "logs"} for args in commands)
    assert all("unrelated" not in " ".join(args) for args in commands)


def test_real_pipe_output_has_byte_and_time_bound():
    module = helper()
    text, code, truncated = module._bounded_command([sys.executable, "-c", "print('x' * 1000000)"], limit=1024, deadline=time.monotonic()+3)
    assert len(text.encode()) == 1024 and truncated and code is None


def test_log_archive_marks_partial_coverage_and_redacts(monkeypatch):
    monkeypatch.setattr(settings, "collect_diagnostic_logs", lambda mode: {
        "api": {"status": "partial", "text": "Authorization: Bearer synthetic-fixture-secret\n"}})
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as bundle:
        coverage = settings.write_docker_logs(bundle, "normal")
    assert coverage["status"] == "partial"
    with zipfile.ZipFile(target) as bundle:
        assert len(bundle.namelist()) == 6
        assert b"synthetic-fixture-secret" not in bundle.read("docker/vms-api.log")


def test_recording_diagnostics_keeps_full_totals_but_only_probes_50(db, monkeypatch):
    seed_history(db, 10000)
    loaded, probes = [], []
    listener = lambda row, ctx: loaded.append(row.id)
    event.listen(RecordingSegment, "load", listener)
    monkeypatch.setattr(recordings, "segment_file_resolution", lambda row: (probes.append(row.id) or Path("dummy"), True, None))
    try:
        result = settings.recordings_diagnostics(db)
    finally:
        event.remove(RecordingSegment, "load", listener)
    assert result["ok"] and result["count"] == 10000
    assert len(result["latest"]) == len(loaded) == len(probes) == 50
    assert result["physical_verification"]["full_archive_checked"] is False


def test_shared_metadata_snapshot_does_not_turn_unknown_files_into_zero():
    from app.services.recorder_diagnostics import _storage_state
    snapshot = {"scan_mode": "not_run_recurring_status", "owned_archive": {
        "kmvms_owned_existing_file_count": None, "evidence_status": "metadata_only"}}
    result = _storage_state(snapshot)
    assert result["owned_archive"]["existing_file_count"] is None
    assert result["owned_archive"]["evidence_status"] == "metadata_only"
    snapshot["owned_archive"]["kmvms_owned_existing_file_count"] = 0
    assert _storage_state(snapshot)["owned_archive"]["existing_file_count"] == 0
