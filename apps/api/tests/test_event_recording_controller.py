"""Recorder behavior at the event boundary, with a deterministic clock."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from recorder_core.event_recording import Episode, event_policy, progress_status
from test_self_recovery_stage1374 import Process, worker


BOOT = 1_800_000_000
POLICY = {"schema_version": 1, "revision": 1, "mode": "events", "source": "camera",
          "profile": "economical", "postrecord_minutes": 1}


def stamp(sec):
    return datetime.fromtimestamp(BOOT + sec, timezone.utc).isoformat()


def source_row(*, phase="healthy", activity=None, sample=1, emergency=False, **options):
    state = {"source": "camera", "source_generation": "this-subscription", "policy_revision": 1,
             "channel": {"generation": "this-subscription", "phase": phase, "sample_at": stamp(sample), "policy_revision": 1}}
    if activity is not None:
        state["last_activity_at"] = stamp(activity)
    values = dict(id=1, name="Fixture camera", storage_folder_name="fixture", enabled=True, events_enabled=True,
                  protocol="onvif", recording_mode="always", rtsp_main_url="rtsp://example.invalid/wide",
                  rtsp_sub_url="rtsp://example.invalid/assigned", default_record_stream="sub", default_live_stream="main", rtsp_transport="tcp",
                  segment_minutes=1, recording_policy=POLICY, recording_signal_state=state, event_low_disk=False,
                  event_recording_emergency_enabled=emergency, event_receiver_current=True, status="idle")
    return SimpleNamespace(**{**values, **options})


def reconcile(episode, row, sec, *, blocked=False, clock=None):
    return episode.reconcile(row, row.recording_policy, now=BOOT + sec, clock=sec if clock is None else clock,
                             started=BOOT, blocked=blocked)


def test_repeats_extend_one_episode_without_notices_or_source_false_ends():
    episode = Episode()
    row = source_row()
    assert reconcile(episode, row, 2).request is None
    row.recording_signal_state["last_activity_at"] = stamp(3)
    assert reconcile(episode, row, 4).episode_until == BOOT + 63
    row.recording_signal_state["last_activity_at"] = stamp(33)
    decision = reconcile(episode, row, 34)
    assert decision.request == "event" and decision.extended and decision.episode_until == BOOT + 93
    row.recording_signal_state["source_confirmed"] = False
    row.event_receiver_current = False
    assert reconcile(episode, row, 92).request == "event"
    assert reconcile(episode, row, 93).request is None


def test_boot_policy_and_lens_generation_discard_stale_activity():
    assert reconcile(Episode(), source_row(activity=-10), 3).request is None
    row = source_row(activity=1)
    row.recording_signal_state["channel"]["generation"] = "replacement"
    assert reconcile(Episode(), row, 3).request is None
    row = source_row(activity=3)
    row.event_receiver_current = False
    assert reconcile(Episode(), row, 4).request is None
    episode = Episode()
    assert reconcile(episode, source_row(activity=3), 5).request == "event"
    row = source_row(activity=3, recording_policy={**POLICY, "revision": 2})
    assert reconcile(episode, row, 6).request is None


@pytest.mark.parametrize("phase", ["failed", "exhausted", "recovering"])
def test_emergency_permission_is_independent_from_healthy_signals_and_blockers(phase):
    episode = Episode()
    row = source_row(phase=phase, emergency=True)
    assert reconcile(episode, row, 2).request == "emergency"
    row.event_recording_emergency_enabled = False
    assert reconcile(episode, row, 3).request is None
    row.recording_signal_state["last_activity_at"] = stamp(4)
    assert reconcile(episode, row, 5).request == "event"
    row.event_recording_emergency_enabled = True
    assert reconcile(episode, row, 6, blocked=True).request is None
    row.enabled = False
    assert reconcile(episode, row, 7).request is None
    row.enabled = True
    row.events_enabled = False
    assert reconcile(episode, row, 8).source_status == "inactive"
    assert reconcile(episode, row, 8).request is None
    assert reconcile(episode, source_row(sample=8, emergency=True), 9).request is None


def test_confirmed_source_stall_is_distinguished_from_silent_progress():
    episode = Episode()
    row = source_row(emergency=True, event_receiver_current=False)
    assert reconcile(episode, row, 2).source_status == "connecting"
    assert reconcile(episode, row, 151).request is None
    assert reconcile(episode, row, 152).request is None
    assert reconcile(episode, row, 162).request == "emergency"
    quiet_reply = source_row(sample=164, emergency=True)
    assert reconcile(episode, quiet_reply, 165).source_status == "recovering"
    assert reconcile(episode, source_row(sample=224, emergency=True), 225).request == "emergency"
    assert reconcile(episode, source_row(sample=284, emergency=True), 285).request is None


def test_receiver_handoff_does_not_start_emergency_or_replay_old_activity():
    episode = Episode()
    stale_activity = source_row(sample=-3, activity=-4, emergency=True)
    first = reconcile(episode, stale_activity, 1)
    assert first.source_status == "healthy" and first.request is None
    gap = source_row(sample=1, emergency=True, event_receiver_current=False)
    assert reconcile(episode, gap, 151).request is None
    assert reconcile(episode, gap, 153).request is None
    new = source_row(sample=156, phase="connecting", emergency=True)
    new.recording_signal_state["channel"]["generation"] = "new"
    assert reconcile(episode, new, 157).request is None
    assert reconcile(episode, source_row(sample=158, activity=158, emergency=True), 159).request == "event"


def test_progress_contract_for_ai_needs_completion_but_no_detection():
    assert progress_status(expected=False, since=0, completed_at=None, now=500, timeout=30) == "inactive"
    assert progress_status(expected=True, since=490, completed_at=None, now=500, timeout=30) == "connecting"
    assert progress_status(expected=True, since=450, completed_at=499, now=500, timeout=30) == "healthy"
    assert progress_status(expected=True, since=450, completed_at=469, now=500, timeout=30) == "stalled"


def test_quick_pause_resume_cutoff_does_not_replay_the_last_pulse():
    episode = Episode()
    row = source_row(activity=1)
    assert reconcile(episode, row, 2).request == "event"
    row.recording_signal_state["recording_cutoff_at"] = stamp(3)
    assert reconcile(episode, row, 4).request is None
    row.recording_signal_state["last_activity_at"] = stamp(5)
    assert reconcile(episode, row, 6).request == "event"


def test_main_loop_reuses_writer_and_calls_existing_finalization(worker, monkeypatch):
    seconds = [2]
    row = source_row()
    session = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchall=lambda: [row]), close=lambda: None)
    monkeypatch.setattr(worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(worker, "RECORDER_STARTED_AT", BOOT)
    monkeypatch.setattr(worker, "time", SimpleNamespace(time=lambda: BOOT + seconds[0], monotonic=lambda: seconds[0]))
    monkeypatch.setattr(worker, "read_recording_format_choice", lambda: ("mkv", "after_segment", None))
    monkeypatch.setattr(worker, "read_recording_suspended_by_low_disk", lambda: False)
    monkeypatch.setattr(worker, "retention_ready", lambda *a: True)
    monkeypatch.setattr(worker, "sync_segment_metadata_for_job", lambda *a: None)
    monkeypatch.setattr(worker, "handle_stale_current_segment", lambda *a: False)
    monkeypatch.setattr(worker, "publish_event_control", Mock())
    finalized = Mock()
    monkeypatch.setattr(worker, "finalize_segments_for_job", finalized)
    process = Process()

    def start(current):
        job = worker.jobs[current.id]
        assert worker.choose_input_url(current) == current.rtsp_sub_url
        job.process = process
        job.set_state(worker.RecorderState.RECORDING)

    starts = Mock(side_effect=start)
    monkeypatch.setattr(worker, "start_camera", starts)
    worker.sync_cameras()
    starts.assert_not_called()
    row.recording_signal_state["last_activity_at"] = stamp(3)
    seconds[0] = 4
    worker.sync_cameras()
    row.recording_signal_state["last_activity_at"] = stamp(23)
    seconds[0] = 24
    worker.sync_cameras()
    starts.assert_called_once()
    assert not process.terminated
    seconds[0] = 64
    worker.sync_cameras()
    assert not process.terminated
    seconds[0] = 83
    worker.sync_cameras()
    finalized.assert_called_once()
    assert process.terminated and worker.jobs[1].state == "idle"
    seconds[0] = 84
    worker.sync_cameras()
    starts.assert_called_once()
    finalized.assert_called_once()


@pytest.mark.parametrize("profile", ["economical", "balanced", "full_quality"])
def test_event_profile_buffer_uses_assigned_roles_and_releases_on_pause(worker, monkeypatch, profile):
    inputs = Mock()
    monkeypatch.setattr(worker, "BufferedCamera", inputs)
    policy = {**POLICY, "profile": profile, "prebuffer_seconds": 0 if profile == "economical" else 30}
    row = source_row(recording_policy=policy)
    job = worker.get_or_create_job(row)
    assert event_policy(row) == policy
    worker.reconcile_event_buffer(row, job, policy, allowed=True)
    worker.reconcile_event_buffer(row, job, policy, allowed=True)
    inputs.assert_called_once_with(worker.event_buffer_budget, row.id, row.rtsp_sub_url, "sub",
                                   row.rtsp_main_url, "main", "tcp", profile, policy["prebuffer_seconds"])
    row.default_live_stream = "sub"
    worker.reconcile_event_buffer(row, job, policy, allowed=True)
    assert inputs.call_count == 2 and inputs.return_value.close.call_count == 1
    assert inputs.call_args.args[4:6] == (row.rtsp_sub_url, "sub")
    worker.reconcile_event_buffer(row, job, policy, allowed=False)
    assert job.event_buffer is None and inputs.return_value.close.call_count == 2


def test_unrecognized_persisted_profile_does_not_break_recorder_poll():
    assert event_policy(source_row(recording_policy={**POLICY, "profile": ["balanced"]})) is None
