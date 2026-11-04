from types import SimpleNamespace

import pytest

from app.core.security import encrypt_text
from app.routers import camera_onvif_routes as routes
from app.services import onvif_service as service
from test_storage_recording_contract import db, add_camera


def test_custom_endpoint_is_used_for_first_sdk_device_service(monkeypatch):
    addresses = []
    device = SimpleNamespace(GetServices=lambda *args: [], GetCapabilities=lambda *args: SimpleNamespace())
    def make_device(cam):
        addresses.append(cam.get_definition("devicemgmt")[0])
        return device
    monkeypatch.setattr(service.ONVIFCamera, "create_devicemgmt_service", make_device)
    service._prepare_camera("camera.test", 8080, "test", "test-password", "/vendor/device_service")
    assert addresses == ["http://camera.test:8080/vendor/device_service"]
    addresses.clear()
    service._prepare_camera("camera.test", 80, "test", "test-password")
    assert addresses == ["http://camera.test:80/onvif/device_service"]


@pytest.mark.parametrize("path", ["https://other.test/device", "//other.test/device", "/device\nInjected", "/device#fragment"])
def test_endpoint_cannot_replace_camera_origin(path):
    with pytest.raises(ValueError, match="invalid_onvif_path"):
        service.normalize_onvif_path(path)


@pytest.mark.parametrize("route_name, service_name", [
    ("onvif_profiles", "fetch_onvif_profiles"), ("onvif_probe", "probe_onvif_device"),
    ("onvif_profile_config", "get_onvif_profile_config"), ("update_onvif_profile_route", "update_onvif_profile"),
])
def test_management_operations_use_saved_or_explicit_endpoint(db, monkeypatch, route_name, service_name):
    camera = add_camera(db)
    camera.protocol = "onvif"
    camera.username = "test"
    camera.password_encrypted = encrypt_text("test-password")
    camera.onvif_path = "/vendor/device_service"
    db.commit()
    received = []
    monkeypatch.setattr(routes, service_name, lambda **kwargs: received.append(kwargs.get("onvif_path")) or {"profiles": []})
    handler = getattr(routes, route_name)
    handler({"camera_id": camera.id, "profile_token": "main"}, db=db, current_user=SimpleNamespace())
    handler({"camera_id": camera.id, "profile_token": "main", "onvif_path": ""}, db=db, current_user=SimpleNamespace())
    assert received == ["/vendor/device_service", None]
