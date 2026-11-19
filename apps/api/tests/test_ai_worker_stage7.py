"""The local detector can drop or fail independently of media and archive control."""
import time
from types import SimpleNamespace

from recorder_core.ai_supervisor import AiSupervisor, Frame, Source, AI_KINDS, RETRY_DELAYS, FRAME_GRACE, MAX_QUEUE_BYTES, MAX_PACKET_BYTES
from recorder_core.event_recording import Episode, event_policy


def detached():
    manager = AiSupervisor()
    now = time.monotonic()
    manager.sources[1] = Source("edit-1", AI_KINDS, now, stream="preview:1")
    manager.backend = {"backend": "cpu"}
    return manager


def frame(captured, stream="preview:1", generation="edit-1", age=0):
    return Frame(1, generation, stream, "h264", b"", b"test", time.monotonic() - age, captured)


def response(*kinds, scene=False):
    return {"phase": "sample", "objects": [{"kind": kind} for kind in kinds], "scene_change": scene}


def test_idle_does_not_create_worker_and_a_zero_object_result_is_healthy():
    manager = AiSupervisor(lambda _: (_ for _ in ()).throw(AssertionError("not selected")))
    manager.configure({}, gpu_allowed=True)
    assert manager.thread is None
    manager = detached()
    manager._accept(frame(100), response())
    assert manager.observe(1, "edit-1").phase == "healthy"
    assert manager.observe(1, "edit-1").activity is None
    manager.sources[1].completed -= FRAME_GRACE + 1
    assert manager.observe(1, "edit-1").phase == "stalled"


def test_confirmed_kind_extends_recording_and_resets_at_feed_transition():
    manager = detached()
    manager._accept(frame(100), response("person"))
    assert manager.observe(1, "edit-1").activity is None
    manager._accept(frame(101), response("person"))
    assert manager.observe(1, "edit-1").activity == 101
    manager.select_stream(1, "edit-1", "record:2")
    manager._accept(frame(103), response("person", scene=True))
    manager._accept(frame(104, "record:2", generation="edit-0"), response(scene=True))
    manager._accept(frame(105, "record:2", age=12), response(scene=True))
    assert manager.observe(1, "edit-1").activity == 101
    manager._accept(frame(106, "record:2"), response("person"))
    assert manager.observe(1, "edit-1").activity == 101
    manager._accept(frame(107, "record:2"), response("person"))
    assert manager.observe(1, "edit-1").activity == 107


def test_queue_pressure_and_fairness_do_not_grow_an_ai_backlog():
    class Packet:
        size = MAX_PACKET_BYTES
        stream = SimpleNamespace(codec_context=SimpleNamespace(name="h264", extradata=b""))

        def __bytes__(self):
            return bytes(self.size)

    manager = detached()
    for i in range(1, 10):
        manager.sources[i] = Source("edit-1", AI_KINDS, time.monotonic())
        manager.offer(i, "edit-1", "same", Packet())
    assert manager.queue_bytes <= MAX_QUEUE_BYTES
    assert [manager._take(time.monotonic()).camera for _ in range(4)] == [1, 2, 3, 4]
    assert manager._take(time.monotonic()) is None
    assert manager.queue_bytes == 0


def test_attempt_budget_is_finite_and_rejects_a_replayed_manual_request():
    manager = detached()
    for seconds in RETRY_DELAYS:
        manager._failure("analysis_timeout")
        assert manager.next_at - manager.failed_at == seconds
    manager._failure("analysis_timeout")
    view = manager.observe(1, "edit-1")
    assert view.phase == "exhausted" and view.info["incident"]["next_attempt_at"] is None
    token = view.info["retry_token"]
    assert not manager.retry(token)
    manager.failed_at -= 31
    assert manager.retry(token)
    assert not manager.retry(token)


def test_common_worker_announces_fault_exhaustion_and_stable_recovery_once(monkeypatch):
    import recorder_core.ai_supervisor as module
    manager = detached()
    manager.sources[2] = Source("edit-1", AI_KINDS, time.monotonic())
    for _ in range(len(RETRY_DELAYS) + 3):
        manager._failure("analysis_timeout")
    reports = manager.drain_transitions()
    assert [kind for kind, _ in reports] == ["failed", "exhausted"]
    assert reports[0][1]["incident"] == reports[1][1]["incident"]
    assert reports[0][1]["camera_ids"] == [1, 2]
    assert manager.drain_transitions() == ()
    monkeypatch.setattr(module, "RECOVERY_PROGRESS", 0)
    manager._accept(frame(time.time()), response())
    manager._accept(frame(time.time()), response())
    recovered, = manager.drain_transitions()
    assert recovered[0] == "recovered" and recovered[1]["incident"] == reports[0][1]["incident"]
    assert manager.failures == 0 and manager.next_at is None


def test_ai_event_deadline_emergency_opt_in_and_continuous_failure_independence():
    policy = {"schema_version": 1, "source": "ai", "revision": 1, "mode": "events", "profile": "balanced",
              "prebuffer_seconds": 30, "postrecord_minutes": 1, "event_kinds": ["person"]}
    row = SimpleNamespace(recording_policy=policy, recording_signal_state={}, protocol="rtsp", events_enabled=False,
                          enabled=True, event_recording_emergency_enabled=False)
    assert event_policy(row) == policy
    episode = Episode()

    def tick(at, source="healthy", activity=None, blocked=False):
        return episode.reconcile(row, row.recording_policy, now=at, clock=at, started=100, blocked=blocked,
                                 ai=SimpleNamespace(phase=source, activity=activity))

    assert tick(103).request is None
    assert tick(105, activity=104).episode_until == 164
    assert tick(106, activity=105).extended
    assert tick(110, "failed").request == "event"  # Already admitted activity survives an outage.
    assert tick(166, "failed").request is None
    row.event_recording_emergency_enabled = True
    assert tick(167, "failed").request == "emergency"
    assert tick(168, "failed", blocked=True).request is None
    row.enabled = False
    assert tick(169, "failed").request is None
    row.enabled = True
    row.recording_policy = {**policy, "mode": "continuous", "profile": "none", "revision": 2}
    assert tick(170, "failed").request == "continuous"
    assert tick(171, "failed", blocked=True).request is None
