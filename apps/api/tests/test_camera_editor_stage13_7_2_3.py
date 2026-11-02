import os
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import settings
import app.routers.cameras as cameras_module
from app.routers.cameras import attach_test_preview_to_camera, test_preview_destination as preview_destination
from app.routers.previews import cleanup_camera_test_previews
from app.services.camera_previews import TEST_PREVIEW_TTL_SECONDS, discard_test_previews, purge_expired_test_previews
from app.services.onvif_service import _profile_source_groups, _profile_stream_metadata, _suggest_profiles


def profile(token, channel, subtype, *, width=None, height=None, source=None):
    return {
        "token": token, "name": token,
        "stream_path": f"/cam/realmonitor?channel={channel}&subtype={subtype}",
        "rtsp_ready": True, "video_source_token": source,
        "video": {"width": width, "height": height},
    }


def test_opaque_profile_names_cannot_reverse_known_main_sub_paths():
    items = [profile("Profile001", 1, 1), profile("Profile000", 1, 0)]
    assert _suggest_profiles(items) == ("Profile000", "Profile001")
    assert _profile_source_groups(items)[0]["recommendation_basis"] == "stream_path"


def test_six_profiles_are_grouped_by_lens_before_quality_comparison():
    items = [
        profile("c1-main", 1, 0, width=3840, height=2160),
        profile("c1-sub1", 1, 1, width=704, height=576),
        profile("c1-sub2", 1, 2, width=1280, height=720),
        profile("c2-main", 2, 0, width=7680, height=4320),
        profile("c2-sub1", 2, 1, width=640, height=360),
        profile("c2-sub2", 2, 2, width=320, height=180),
    ]
    # Global min/max would incorrectly combine c2-main with a different lens.
    assert _suggest_profiles(items) == ("c1-main", "c1-sub1")
    groups = _profile_source_groups(items)
    assert len(groups) == 2
    assert groups[1]["main_profile_token"] == "c2-main"
    assert groups[1]["sub_profile_token"] == "c2-sub2"
    assert groups[0]["profile_tokens"] == ["c1-main", "c1-sub1", "c1-sub2"]


def test_missing_video_configuration_still_uses_channel_and_stream_role():
    items = [profile(f"p{channel}{subtype}", channel, subtype) for channel in (1, 2) for subtype in (0, 1, 2)]
    assert _suggest_profiles(items) == ("p10", "p11")
    groups = _profile_source_groups(items)
    assert groups[1]["main_profile_token"] == "p20"
    assert groups[1]["sub_profile_token"] == "p21"


def test_unknown_roles_do_not_use_lexical_profile_names_as_a_recommendation():
    items = [{"token": "first", "name": "Profile000"}, {"token": "second", "name": "Profile999"}]
    assert _suggest_profiles(items) == ("first", None)
    assert _profile_source_groups(items)[0]["recommendation_basis"] == "unavailable"


def test_single_stream_does_not_invent_a_second_stream():
    assert _suggest_profiles([profile("single", 1, 0)]) == ("single", None)
    assert _suggest_profiles([]) == (None, None)
    assert _suggest_profiles([{**profile("missing", 1, 0), "rtsp_ready": False}]) == (None, None)


def test_source_identity_is_available_without_vendor_path_hints():
    items = [
        {"token": "a", "name": "MainStream", "video_source_token": "sensor-a"},
        {"token": "b", "name": "SubStream1", "video_source_token": "sensor-a"},
        {"token": "c", "name": "MainStream", "video_source_token": "sensor-b"},
    ]
    assert _suggest_profiles(items) == ("a", "b")
    assert len(_profile_source_groups(items)) == 2
    assert _profile_stream_metadata({"stream_path": "/Streaming/Channels/202"})["source_id"] == "channel:2"
    assert _profile_stream_metadata({"stream_path": "/Streaming/Channels/202"})["stream_kind"] == "sub"


@pytest.fixture
def previews(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_previews", str(tmp_path / "previews"))
    root = tmp_path / "previews"
    (root / "camera-tests").mkdir(parents=True)
    (root / "camera-previews").mkdir()
    return root


def test_edit_checks_use_separate_frames_until_explicit_save(previews):
    saved = settings.camera_preview_path(42)
    saved.write_bytes(b"saved-camera-frame")
    main_path, main_url, main_token = preview_destination({"camera_id": 42})
    sub_path, sub_url, sub_token = preview_destination({"camera_id": 42})
    assert main_path != sub_path != saved
    assert main_url != sub_url
    assert main_token != sub_token
    main_path.write_bytes(b"main-test-frame")
    sub_path.write_bytes(b"sub-test-frame")
    assert saved.read_bytes() == b"saved-camera-frame"
    attach_test_preview_to_camera(main_token, 42)
    assert saved.read_bytes() == b"main-test-frame"
    assert not main_path.exists()
    assert sub_path.read_bytes() == b"sub-test-frame"
    assert discard_test_previews([main_token, sub_token]) == 1
    assert saved.read_bytes() == b"main-test-frame"


def test_cancel_cleanup_is_idempotent_and_cannot_delete_saved_frames(previews):
    settings.camera_preview_path(42).write_bytes(b"saved")
    settings.camera_test_preview_path("owned-frame").write_bytes(b"test")
    actor = SimpleNamespace(role="owner")
    assert cleanup_camera_test_previews({"tokens": ["owned-frame"]}, actor) == {"removed": 1}
    assert cleanup_camera_test_previews({"tokens": ["owned-frame"]}, actor) == {"removed": 0}
    with pytest.raises(HTTPException) as exc:
        cleanup_camera_test_previews({"tokens": ["../camera-previews/42"]}, actor)
    assert exc.value.status_code == 422
    assert settings.camera_preview_path(42).read_bytes() == b"saved"


def test_expired_orphan_cleanup_preserves_current_frames_and_symlinks(previews):
    now = time.time()
    old = settings.camera_test_preview_path("abandoned")
    fresh = settings.camera_test_preview_path("current")
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    os.utime(old, (now - TEST_PREVIEW_TTL_SECONDS - 1, now - TEST_PREVIEW_TTL_SECONDS - 1))
    saved = settings.camera_preview_path(42)
    saved.write_bytes(b"saved")
    link = settings.camera_test_preview_path("symlink")
    link.symlink_to(saved)
    assert purge_expired_test_previews(now) == 1
    assert not old.exists()
    assert fresh.read_bytes() == b"fresh"
    assert discard_test_previews(["symlink"]) == 0
    assert link.is_symlink()
    assert saved.read_bytes() == b"saved"


def test_cleanup_refuses_a_symlinked_test_directory(previews, tmp_path):
    external = tmp_path / "outside"
    external.mkdir()
    (external / "frame.jpg").write_bytes(b"outside")
    (previews / "camera-tests").rmdir()
    (previews / "camera-tests").symlink_to(external, target_is_directory=True)
    assert discard_test_previews(["frame"]) == 0
    assert (external / "frame.jpg").read_bytes() == b"outside"


@pytest.mark.parametrize("metadata_only", [True, False])
def test_metadata_reads_and_frameless_checks_do_not_capture_jpegs(monkeypatch, metadata_only):
    observed = {"proofs": 0}

    def fake_run(command, **kwargs):
        assert command[0] == "ffprobe"
        assert kwargs["timeout"] == 20
        return SimpleNamespace(returncode=0, stdout='{"streams":[{"codec_type":"video","codec_name":"hevc","width":2048,"height":928,"r_frame_rate":"25/1"}]}')

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata-only / frameless checks must not allocate or capture previews")

    def proof(*args):
        observed["proofs"] += 1
        return "synthetic-proof"

    monkeypatch.setattr(cameras_module.subprocess, "run", fake_run)
    monkeypatch.setattr(cameras_module, "test_preview_destination", forbidden)
    monkeypatch.setattr(cameras_module, "capture_camera_preview", forbidden)
    monkeypatch.setattr(cameras_module, "register_rtsp_test_proof", proof)
    result = cameras_module.test_camera({
        "protocol": "rtsp", "host": "camera.example.test", "port": 554,
        "username": "synthetic", "password": "synthetic-password", "rtsp_main_url": "/sub2",
        "stream_role": "main", "metadata_only": metadata_only, "include_preview": False,
    }, db=SimpleNamespace(), current_user=SimpleNamespace(role="owner"))
    assert result["video"]["width"] == 2048
    assert result["video"]["height"] == 928
    assert result["metadata_only"] is metadata_only
    assert result["preview_ok"] is False
    assert result["preview_url"] is result["preview_token"] is result["preview_message"] is None
    assert observed["proofs"] == (0 if metadata_only else 1)
    assert result["validation_token"] == (None if metadata_only else "synthetic-proof")
