"""Repeat-audit regressions for redaction and canonical camera connections."""
import io
import json
import zipfile
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi import HTTPException

from app.core.sanitization import SENSITIVE_QUERY_KEYS, redact_text
from app.core.security import encrypt_text, decrypt_text
from app.routers import cameras, camera_onvif_routes
from app.routers.camera_connection_helpers import (
    get_camera_credentials, resolve_test_connection_payload, validation_fingerprint,
    register_onvif_probe_proof, resolve_camera_connection_payload, ONVIF_PROBE_PROOFS,
)
from app.routers.settings import write_json
from app.schemas.camera import CameraResponse, CameraUpdate, safe_rtsp_management_value, restore_rtsp_management_value
from test_self_recovery_stage1374 import worker
from test_storage_recording_contract import db, add_camera, FakeRequest, actor


@pytest.mark.parametrize("key", sorted(SENSITIVE_QUERY_KEYS) + ["PaSsWoRd", "%70assword", "AUTH", "%61uth"])
def test_api_recorder_and_diagnostic_zip_share_secret_query_contract(key, worker):
    secret = "dummy-query-sensitive"
    uri = f"rtsp://dummy-user:dummy-pass@camera.invalid/main?channel=7&{key}={secret}&subtype=0"
    api_error, recorder_error = redact_text(uri), worker.redact_text(uri)
    assert api_error == recorder_error
    assert secret not in api_error and "dummy-pass" not in api_error
    assert "channel=7" in api_error and "subtype=0" in api_error
    assert redact_text(api_error) == api_error
    bundle_bytes = io.BytesIO()
    with zipfile.ZipFile(bundle_bytes, "w") as bundle:
        write_json(bundle, "cameras/cameras.json", [{"rtsp_main_url": uri, "last_error": f"Failed {uri}"}])
    with zipfile.ZipFile(bundle_bytes) as bundle:
        text = bundle.read("cameras/cameras.json").decode()
    assert secret not in text and "dummy-pass" not in text


def test_old_camera_error_is_sanitized_on_api_read_and_new_recorder_tail(worker, db):
    row = add_camera(db)
    uri = "rtsp://dummy:dummy-user-secret@camera.invalid/main?password=dummy-query-secret&signature=dummy-signature"
    row.last_error = f"Connection failed: {uri}"
    db.commit()
    payload = CameraResponse.model_validate(row).model_dump(mode="json")
    assert "dummy-query-secret" not in json.dumps(payload)
    assert "dummy-signature" not in json.dumps(payload)
    job = worker.RecordingJob(row.id, row.name, row.storage_folder_name, "always", True)
    job.process = SimpleNamespace(stderr=io.StringIO(f"Failed to open {uri}\n"))
    worker.read_stream_tail(job)
    error = worker.stderr_tail_text(job)
    assert "dummy-query-secret" not in error and "dummy-user-secret" not in error
    assert row.last_error.endswith("signature=dummy-signature"), "read protection must not rewrite stored history"


@pytest.mark.parametrize("key", ["refresh_token", "media_token", "credentials", "authorization", "Password", "%61uth"])
def test_editable_projection_and_restore_use_same_secret_key_contract(key):
    stored = f"rtsp://dummy:dummy-pass@camera.invalid/main?{key}=dummy-query-secret&channel=2"
    public = safe_rtsp_management_value(stored)
    assert "dummy-query-secret" not in public and "channel=2" in public
    assert restore_rtsp_management_value(public, stored) == stored


@pytest.mark.parametrize("username", ["", None, "new-user"])
@pytest.mark.parametrize("sub", ["", None])
def test_real_test_route_and_update_agree_on_explicit_clear(db, monkeypatch, username, sub):
    row = add_camera(db)
    row.username = "old-user"
    row.password_encrypted = encrypt_text("dummy-stored-password")
    row.rtsp_main_url = "rtsp://old-user:dummy-stored-password@127.0.0.1:554/main?auth=dummy-path-secret"
    row.rtsp_sub_url = "rtsp://old-user:dummy-stored-password@127.0.0.1:554/sub"
    row.default_live_stream = row.default_record_stream = "main"
    row.onvif_path = "/old-endpoint"
    db.commit()
    sent = []
    def probe(command, **kwargs):
        sent.append(command[-1])
        return SimpleNamespace(returncode=0, stdout='{"streams":[{"codec_type":"video","codec_name":"h264"}]}')
    monkeypatch.setattr(cameras.subprocess, "run", probe)
    monkeypatch.setattr(cameras, "create_event", lambda **kwargs: None)
    monkeypatch.setattr(cameras, "attach_test_preview_to_camera", lambda *args: None)
    change = {"username": username, "rtsp_sub_url": sub, "onvif_path": "", "password": ""}
    test_payload = {"camera_id": row.id, **change, "stream_role": "main", "include_preview": False}
    resolved = resolve_test_connection_payload(db, test_payload)
    tested_fingerprint = validation_fingerprint(resolved)
    result = cameras.test_camera(test_payload, db=db, current_user=actor())
    assert "old-user" not in sent[-1]
    assert "dummy-path-secret" in sent[-1], "redacted paths must restore only on the server"
    saved = cameras.update_camera(
        row.id, CameraUpdate(**change, main_validation_token=result["validation_token"]),
        request=FakeRequest(), db=db, current_user=actor(),
    )
    assert saved.username == username and saved.rtsp_sub_url is None and saved.onvif_path == ""
    assert saved.rtsp_main_url == sent[-1]
    assert decrypt_text(saved.password_encrypted) == "dummy-stored-password"
    final = resolve_test_connection_payload(db, {"camera_id": saved.id})
    assert tested_fingerprint == validation_fingerprint(final)


def test_missing_fields_keep_values_but_explicit_null_or_empty_do_not(db):
    row = add_camera(db)
    row.username = "old-user"
    row.password_encrypted = encrypt_text("dummy-password")
    row.rtsp_sub_url = "/sub"
    row.onvif_path = "/custom"
    row.rtsp_host = "configured.invalid"
    db.commit()
    retained = get_camera_credentials(db, {"camera_id": row.id})
    assert retained["username"] == "old-user"
    assert retained["rtsp_host"] == "configured.invalid"
    for value in ("", None):
        cleared = get_camera_credentials(db, {"camera_id": row.id, "username": value, "onvif_path": value, "password": value})
        assert cleared["username"] == value and cleared["onvif_path"] == value
        assert cleared["password"] == "dummy-password"
        resolved = resolve_test_connection_payload(db, {"camera_id": row.id, "rtsp_sub_url": value})
        assert resolved["rtsp_sub_url"] is None


def test_cleared_sub_cannot_test_old_sub_or_bypass_save_proof(db, monkeypatch):
    row = add_camera(db)
    row.rtsp_main_url, row.rtsp_sub_url = "/main", "/sub"
    db.commit()
    monkeypatch.setattr(cameras.subprocess, "run", lambda *args, **kwargs: pytest.fail("cleared Sub must never be probed"))
    with pytest.raises(HTTPException) as error:
        cameras.test_camera({"camera_id": row.id, "rtsp_sub_url": "", "stream_role": "sub"}, db=db, current_user=actor())
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        cameras.update_camera(row.id, CameraUpdate(username="changed"), FakeRequest(), db=db, current_user=actor())
    assert error.value.status_code == 409


@pytest.mark.parametrize("route_name, service_name", [
    ("onvif_profile_config", "get_onvif_profile_config"),
    ("update_onvif_profile_route", "update_onvif_profile"),
])
@pytest.mark.parametrize("saved", [False, True])
def test_device_settings_consumes_draft_endpoint_not_saved_or_default(db, monkeypatch, route_name, service_name, saved):
    row = add_camera(db) if saved else None
    if row:
        row.onvif_path = "/old-endpoint"
        db.commit()
    seen = []
    monkeypatch.setattr(camera_onvif_routes, service_name, lambda **kwargs: seen.append(kwargs.get("onvif_path")) or {})
    handler = getattr(camera_onvif_routes, route_name)
    handler({"camera_id": row.id if row else None, "host": "camera.invalid", "port": 80,
             "username": "dummy-user", "password": "dummy-password", "profile_token": "main",
             "onvif_path": "/draft-endpoint"}, db=db, current_user=actor())
    assert seen == ["/draft-endpoint"]


@pytest.mark.parametrize("role", ["main", "sub"])
@pytest.mark.parametrize("origin", ["missing", "redacted", "explicit", "empty", "null", "other-channel"])
def test_fresh_onvif_query_secret_precedes_stored_fallback_but_not_explicit_input(role, origin):
    key = f"rtsp_{role}_url"
    profile_key = "onvif_profile_token" if role == "main" else "onvif_sub_profile_token"
    old = "/stream?channel=1&auth=dummy-old-secret"
    fresh = "/stream?channel=1&auth=dummy-fresh-secret"
    camera = SimpleNamespace(protocol="onvif", host="camera.invalid", port=80,
        rtsp_host="camera.invalid", rtsp_port=554, username="dummy", password_encrypted=encrypt_text("dummy-password"),
        **{key: old, profile_key: "selected"})
    payload = {"protocol": "onvif", "host": "camera.invalid", "port": 80, "rtsp_host": "camera.invalid", "rtsp_port": 554,
        "username": "dummy", "password": "dummy-password", profile_key: "selected"}
    token = register_onvif_probe_proof(payload, profiles=[{"token": "selected", "stream_path": fresh}])
    payload["onvif_probe_token"] = token
    values = {"redacted": safe_rtsp_management_value(old), "explicit": "/stream?channel=1&auth=dummy-explicit-secret",
              "empty": "", "null": None, "other-channel": "/stream?channel=2&auth=redacted"}
    if origin != "missing":
        payload[key] = values[origin]
    result = resolve_camera_connection_payload(payload, camera)[key]
    if origin in {"empty", "null"}:
        assert result is None
    elif origin == "explicit":
        assert "dummy-explicit-secret" in result
    elif origin == "other-channel":
        assert "dummy-fresh-secret" not in result
    else:
        assert "dummy-fresh-secret" in result and "dummy-old-secret" not in result
    ONVIF_PROBE_PROOFS[token]["created_at"] = 0
    expired = resolve_camera_connection_payload(payload, camera)[key]
    if origin in {"missing", "redacted"}:
        assert "dummy-old-secret" in expired and "dummy-fresh-secret" not in expired


def test_rotated_onvif_secret_real_test_then_partial_update_keeps_exact_proof(db, monkeypatch):
    row = add_camera(db)
    row.protocol, row.host, row.port = "onvif", "127.0.0.1", 80
    row.rtsp_host, row.rtsp_port = "127.0.0.1", 554
    row.username, row.password_encrypted = "dummy", encrypt_text("dummy-password")
    row.rtsp_main_url, row.rtsp_sub_url = "/stream?channel=1&auth=dummy-old", None
    row.onvif_profile_token = "selected"
    row.default_live_stream = row.default_record_stream = "main"
    db.commit()
    management = resolve_test_connection_payload(db, {"camera_id": row.id})
    token = register_onvif_probe_proof(management, profiles=[{"token": "selected", "stream_path": "/stream?channel=1&auth=dummy-fresh"}])
    sent = []
    def probe(command, **kwargs):
        sent.append(command[-1])
        return SimpleNamespace(returncode=0, stdout='{"streams":[{"codec_type":"video","codec_name":"h264"}]}')
    monkeypatch.setattr(cameras.subprocess, "run", probe)
    monkeypatch.setattr(cameras, "create_event", lambda **kwargs: None)
    monkeypatch.setattr(cameras, "attach_test_preview_to_camera", lambda *args: None)
    tested = cameras.test_camera({"camera_id": row.id, "onvif_probe_token": token,
        "rtsp_main_url": safe_rtsp_management_value(row.rtsp_main_url), "stream_role": "main", "include_preview": False}, db=db, current_user=actor())
    assert "dummy-fresh" in sent[-1]
    saved = cameras.update_camera(row.id, CameraUpdate(onvif_probe_token=token,
        main_validation_token=tested["validation_token"]), FakeRequest(), db=db, current_user=actor())
    assert saved.rtsp_main_url == sent[-1]


@pytest.mark.parametrize("submitted,expected", [
    ("auth=manual&auth=redacted", "auth=manual&auth=second"),
    ("auth=redacted&auth=manual", "auth=first&auth=manual"),
    ("auth=redacted&auth=redacted", "auth=first&auth=second"),
])
def test_repeated_sensitive_query_keys_preserve_corresponding_occurrence(submitted, expected):
    assert restore_rtsp_management_value(f"/stream?{submitted}", "/stream?auth=first&auth=second") == f"/stream?{expected}"
