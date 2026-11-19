"""Pure linked graph checks, independent of camera/RTSP and PostgreSQL."""
from types import SimpleNamespace

from recorder_core.event_recording import Decision, Episode
from recorder_core.linked_recording import link_status, linked_policy, origin_intent, resolve_links


def camera(cid, target=None, **options):
    defaults = dict(id=cid, enabled=True, status="enabled", deleted_at=None, protocol="rtsp",
                    recording_mode="always", events_enabled=False, recording_signal_state=None)
    if target is not None:
        defaults["recording_policy"] = {"mode": "linked", "source": "none", "schema_version": 1,
                                        "revision": 1, "profile": "balanced",
                                        "linked_camera_id": target, "prebuffer_seconds": 30}
    else:
        defaults["recording_policy"] = None
    return SimpleNamespace(**{**defaults, **options})


def test_linked_fanout_and_chain_do_not_need_an_active_writer():
    root = camera(85, status="error")
    a, b, c = camera(1, 85), camera(2, 1), camera(3, 85)
    resolved = resolve_links([c, b, a, root], {root.id: origin_intent(root, suspended=False)})
    assert all(item.mode == "continuous" and item.origin_id == root.id for item in resolved.values())
    assert all(item.decision.request == "continuous" and item.allows_start(100) for item in resolved.values())
    assert len({item.generation for item in resolved.values()}) == 3

    a.enabled = False
    next_resolved = resolve_links([c, b, a, root], {root.id: origin_intent(root, suspended=False)})
    assert next_resolved[2].decision.source_status == "source_paused"
    assert not next_resolved[2].allows_start(100)
    assert b.enabled is True and next_resolved[2].generation != resolved[2].generation
    assert next_resolved[3] == resolved[3]


def test_linked_event_reuses_source_deadline_but_fences_old_generation():
    master = camera(7, protocol="onvif", events_enabled=True)
    target = camera(20, 7)
    episode = Episode(deadline=30, episode_started_at=432.0)
    root = origin_intent(master, suspended=False, decision=Decision("event", "healthy", 500), episode=episode)
    linked = resolve_links([target, master], {7: root})[20]
    assert linked.warm and linked.started_at == 432.0 and linked.allows_start(29.99)
    assert not linked.allows_start(30)

    episode.deadline = 40
    extended_root = origin_intent(master, suspended=False, decision=Decision("event", "healthy", 510, True), episode=episode)
    renewed = resolve_links([target, master], {7: extended_root})[20]
    assert renewed.generation == linked.generation  # no reopen on a repeat
    assert renewed.decision.extended and renewed.decision.episode_until == 510

    master.recording_signal_state = {"recording_cutoff_at": "2026-11-19T01:00:00+00:00"}
    stopped = origin_intent(master, suspended=False, decision=Decision(None, "healthy"), episode=episode)
    wait = resolve_links([target, master], {7: stopped})[20]
    assert wait.generation != linked.generation and link_status(wait) == "waiting_linked_source"


def test_missing_deleted_and_invalid_or_cyclic_links_never_fall_back_to_always():
    root = camera(50)
    deleted = camera(8, deleted_at="2026-11-19")
    malformed = camera(9, 50)
    malformed.recording_policy["linked_camera_id"] = True
    assert linked_policy(malformed) is None
    rows = [camera(1, 2), camera(2, 1), camera(3, 2), camera(4, 4), camera(5, 1000),
            camera(6, 8), camera(7, 9), deleted, malformed, camera(10, 50), root]
    origins = {item.id: origin_intent(item, suspended=False) for item in (deleted, root)}
    values = resolve_links(rows, origins)
    assert all(values[i].decision.source_status == "link_cycle" for i in (1, 2, 3, 4))
    assert all(values[i].decision.source_status == "source_unavailable" for i in (5, 6))
    assert values[7].decision.source_status == "link_invalid"
    assert all(link_status(values[i]) == "linked_source_unavailable" for i in (1, 2, 3, 4, 5, 6, 7, 9))
    assert values[10].decision.request == "continuous"


def test_long_link_chain_is_iterative_and_only_propagates_permitted_emergency():
    root = camera(1, protocol="onvif", events_enabled=True)
    rows = [camera(i, i - 1) for i in range(2, 1351)][::-1] + [root]
    for request, suspended, expected in (("emergency", False, "emergency"), (None, False, None),
                                         ("emergency", True, None)):
        intent = origin_intent(root, suspended=suspended, decision=Decision(request, "failed"), episode=Episode())
        tail = resolve_links(rows, {1: intent})[1350]
        assert tail.decision.request == expected
        assert tail.origin_id == root.id
