from types import SimpleNamespace as NS
import json

import pytest

from app.services import camera_stream_dimensions as store
from app.routers import camera_connection_helpers as proofs


@pytest.fixture
def camera(tmp_path, monkeypatch):
    monkeypatch.setattr(store.settings, "storage_previews", str(tmp_path))
    monkeypatch.setattr(proofs, "ONVIF_PROBE_PROOFS", {})
    monkeypatch.setattr(proofs, "RTSP_TEST_PROOFS", {})
    return NS(id=71, name="lens", protocol="onvif", host="camera.invalid", port=80,
              username="private-user", rtsp_host="camera.invalid", rtsp_port=554,
              rtsp_transport="tcp", rtsp_main_url="/main-secret", rtsp_sub_url="/sub-secret",
              onvif_path="/onvif/device_service", onvif_channel_id="source-2",
              onvif_profile_token="opaque-main", onvif_sub_profile_token="opaque-sub")


def test_dimensions_persist_per_role_without_credentials_or_device_requests(camera):
    assert store.remember_dimensions(camera,"main",{"resolution":"3840x2160"})
    assert store.remember_dimensions(camera,"sub",{"video":{"width":640,"height":480}})
    assert store.read_dimensions(camera)=={"main":{"width":3840,"height":2160},"sub":{"width":640,"height":480}}
    text=store._path(camera,"sub").read_text()
    for secret in (camera.username,camera.host,camera.rtsp_sub_url,camera.onvif_sub_profile_token):
        assert secret not in text
    before=store._path(camera,"sub").stat().st_mtime_ns
    store.remember_dimensions(camera,"sub",{"width":640,"height":480})
    assert store._path(camera,"sub").stat().st_mtime_ns==before


@pytest.mark.parametrize("value", [None, [], {}, {"width":True,"height":1080},
    {"width":0,"height":1080},{"width":float("nan"),"height":1},
    {"width":float("inf"),"height":1},{"width":1920,"height":-1},
    {"resolution":"1920x1080garbage"},{"width":65537,"height":1080}])
def test_invalid_metadata_is_inert(camera,value):
    assert not store.remember_dimensions(camera,"main",value)
    assert store.read_dimensions(camera)=={}


def test_connection_profile_and_lens_changes_invalidate_only_matching_dimensions(camera):
    for role in ("main","sub"): store.remember_dimensions(camera,role,{"width":1920,"height":1080})
    camera.name="renamed"
    assert len(store.read_dimensions(camera))==2
    camera.onvif_sub_profile_token="other-lens-sub"
    assert set(store.read_dimensions(camera))=={"main"}
    camera.host="other-device.invalid"
    assert store.read_dimensions(camera)=={}


def test_corrupt_oversized_or_linked_cache_is_not_used_and_io_failure_is_nonfatal(camera,tmp_path):
    store.remember_dimensions(camera,"sub",{"width":640,"height":480})
    path=store._path(camera,"sub")
    path.write_text("[]")
    assert store.read_dimensions(camera)=={}
    path.write_text("x"*3000)
    assert store.read_dimensions(camera)=={}
    path.unlink()
    victim=tmp_path/"unrelated.txt"; victim.write_text("keep")
    path.symlink_to(victim)
    assert store.read_dimensions(camera)=={}
    assert victim.read_text()=="keep"


def test_onvif_dimensions_match_saved_tokens_and_ignore_unsaved_drafts(camera):
    creds={key:getattr(camera,key) for key in ("host","port","username","onvif_path")}
    profiles=[{"token":"opaque-main","video":{"width":3840,"height":2160}},
              {"token":"opaque-sub","current":{"resolution":"640x480"}},
              {"token":"other-lens","video":{"width":999,"height":999}}]
    store.remember_onvif_dimensions(camera,{**creds,"host":"unsaved.invalid"},profiles)
    assert store.read_dimensions(camera)=={}
    store.remember_onvif_dimensions(camera,creds,profiles)
    assert store.read_dimensions(camera)["sub"]=={"width":640,"height":480}


def test_verified_onboarding_proofs_supply_dimensions_without_trusting_client_fields(camera):
    payload={**vars(camera),"password":"synthetic-only"}
    payload["main_validation_token"]=proofs.register_rtsp_test_proof(payload,"main",video={"width":3840,"height":2160})
    payload["sub_validation_token"]=proofs.register_rtsp_test_proof(payload,"sub",video={"width":640,"height":480})
    payload["stream_dimensions"]={"main":{"width":1,"height":1}}
    proofs.persist_verified_stream_dimensions(camera,payload)
    assert store.read_dimensions(camera)["main"]=={"width":3840,"height":2160}
    camera.rtsp_sub_url=payload["rtsp_sub_url"]="/different"
    proofs.persist_verified_stream_dimensions(camera,payload)
    assert "sub" not in store.read_dimensions(camera)


def test_onvif_proof_metadata_is_profile_and_path_bound(camera):
    payload={**vars(camera),"password":"synthetic-only"}
    payload["onvif_probe_token"]=proofs.register_onvif_probe_proof(payload,profiles=[
        {"token":camera.onvif_profile_token,"stream_path":"/main-secret","video":{"width":3840,"height":2160}},
        {"token":camera.onvif_sub_profile_token,"stream_path":"/another-lens","video":{"width":640,"height":480}}])
    proofs.persist_verified_stream_dimensions(camera,payload)
    assert set(store.read_dimensions(camera))=={"main"}
