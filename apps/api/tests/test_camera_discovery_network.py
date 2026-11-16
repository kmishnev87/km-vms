from types import SimpleNamespace
import ipaddress
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.services import camera_discovery_network as network
from app.services.camera_discovery_worker import DiscoveryJobs
from app.routers import camera_discovery_routes as routes
from app.routers.deps import get_current_user
from app.services.camera_discovery_client import DiscoveryRequestError, DiscoveryUnavailable


def service(urls, *, scopes=None, types=None):
    return SimpleNamespace(getXAddrs=lambda: urls, getScopes=lambda: scopes or [], getTypes=lambda: types or [network.ONVIF_NS + ":Device"])


@pytest.mark.parametrize("value", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "::1", "224.0.0.1", "host.invalid", "0.0.0.0"])
def test_only_explicit_private_ipv4_ranges(value):
    with pytest.raises(ValueError):
        network.parse_ranges([value])


def test_ranges_are_bounded_before_host_enumeration_and_deduplicated():
    for value in ("10.0.0.0/8", "192.168.0.0/16"):
        with pytest.raises(ValueError, match="network_too_large"):
            network.parse_ranges([value])
    nets, hosts = network.parse_ranges(["192.168.10.0/24", "192.168.10.1/32"])
    assert len(hosts) == 254
    assert len(nets) == 2
    assert network.parse_ranges(["100.64.0.4"])[1] == ["100.64.0.4"]
    with pytest.raises(ValueError, match="too_many_hosts"):
        network.parse_ranges([f"192.168.{index}.0/24" for index in range(5)])


@pytest.mark.parametrize("ports", [[0], [65536], [True], [1.5], ["80"], [], list(range(1, 14))])
def test_port_validation(ports):
    with pytest.raises(ValueError):
        network.parse_ports(ports, network.DEFAULT_ONVIF_PORTS)


def test_many_devices_not_truncated_to_twenty_and_ids_unique():
    rows = network.normalize_services([service([f"http://192.168.10.{index}/onvif/device_service"]) for index in range(1, 52)])
    assert len(rows) == 51
    assert len({row["id"] for row in rows}) == 51


def test_multiple_paths_are_merged_without_colliding_row_keys():
    rows = network.normalize_services([service(["http://192.168.10.2/a", "http://192.168.10.2/b", "http://192.168.10.2/a"])])
    assert len(rows) == 1
    assert len(rows[0]["onvif_endpoints"]) == 2


def test_printer_and_malformed_reply_do_not_hide_good_camera():
    rows = network.normalize_services([
        service(["http://192.168.10.3/printer"], types=["http://schemas.xmlsoap.org/ws/2006/02/devprof:Device"]),
        service(["http://192.168.10.4:bad/onvif", "http://192.168.10.5/onvif/device_service"]),
    ])
    assert [row["host"] for row in rows] == ["192.168.10.5"]


def test_discovery_urls_never_export_userinfo_query_or_fragment():
    assert network.onvif_candidate("http://user:password@192.168.10.5/onvif") is None
    assert network.onvif_candidate("ftp://192.168.10.5/onvif") is None
    row = network.onvif_candidate("http://192.168.10.5/onvif?credential=secret#private")
    assert row["xaddr_path"] == "/onvif"
    assert "secret" not in str(row)


def test_protocol_evidence_merges_without_losing_onvif_port():
    onvif = network.onvif_candidate("http://192.168.10.5:2020/onvif")
    rtsp = network.candidate("192.168.10.5", "address_probe")
    rtsp.update(protocols=["rtsp"], rtsp_ports=[8554], port=8554)
    row = network.merge_candidate(rtsp, onvif)
    assert row["port"] == 2020
    assert set(row["protocols"]) == {"rtsp", "onvif"}
    assert row["rtsp_ports"] == [8554]


def test_unicast_reply_correlated_and_confined_to_selected_network():
    data = b'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="urn:address" xmlns:d="urn:discovery" xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><s:Header><a:RelatesTo>test-id</a:RelatesTo></s:Header><s:Body><d:ProbeMatches><d:ProbeMatch><d:Types>tds:Device</d:Types><d:Scopes>onvif://www.onvif.org/name/Virtual%20camera</d:Scopes><d:XAddrs>http://192.168.10.5/onvif http://192.168.11.5/onvif</d:XAddrs></d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>'''
    nets = [ipaddress.ip_network("192.168.10.0/24")]
    rows = network.parse_probe_matches(data, "test-id", nets)
    assert len(rows) == 1 and rows[0]["name"] == "Virtual camera"
    assert network.parse_probe_matches(data, "unrelated-id", nets) == []
    assert network.parse_probe_matches(b"<!DOCTYPE a>" + data, "test-id", nets) == []


def test_cancelled_address_search_does_not_open_connections(monkeypatch):
    monkeypatch.setattr(network, "probe_onvif_address", lambda *_: pytest.fail("unexpected camera I/O"))
    monkeypatch.setattr(network, "probe_rtsp_address", lambda *_: pytest.fail("unexpected camera I/O"))
    network.address_discovery(["192.168.10.5"], [80], [554], ["onvif", "rtsp"], lambda: True, lambda row: None, lambda n: None)


def test_jobs_scope_capacity_and_cancellation(monkeypatch):
    manager = DiscoveryJobs()
    hold = threading.Event()
    monkeypatch.setattr(manager, "_run", lambda *args: hold.wait(.1))
    first = manager.start("1", {"local": True, "networks": []})
    second = manager.start("2", {"local": True, "networks": []})
    with pytest.raises(KeyError):
        manager.snapshot("2", first["job_id"])
    with pytest.raises(RuntimeError, match="discovery_busy"):
        manager.start("1", {"local": True, "networks": []})
    with pytest.raises(RuntimeError, match="discovery_busy"):
        manager.start("3", {"local": True, "networks": []})
    assert manager.snapshot("1", first["job_id"], cancel=True)["cancel_requested"]
    assert not manager.snapshot("2", second["job_id"])["cancel_requested"]
    hold.set()


def test_failure_of_local_discovery_does_not_abort_address_fallback(monkeypatch):
    import app.services.camera_discovery_worker as worker
    monkeypatch.setattr(worker, "multicast_discovery", lambda *args: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(worker, "unicast_discovery", lambda *args: None)
    def fake_addresses(hosts, onvif, rtsp, protocols, cancelled, publish, progress):
        publish(network.onvif_candidate("http://192.168.10.5/onvif")); progress(1)
    monkeypatch.setattr(worker, "address_discovery", fake_addresses)
    manager = DiscoveryJobs()
    job = manager.start("1", {"local": True, "networks": ["192.168.10.5"]})
    end = time.monotonic() + 2
    while time.monotonic() < end:
        job = manager.snapshot("1", job["job_id"])
        if job["status"] != "running": break
        time.sleep(.01)
    assert job["status"] == "partial"
    assert len(job["candidates"]) == 1
    assert job["checked_hosts"] == 1


@pytest.mark.parametrize("role,allowed", [("owner", True), ("admin", True), ("operator", False), ("viewer", False)])
def test_discovery_api_permissions_and_owner_binding(monkeypatch, role, allowed):
    app = FastAPI(); app.include_router(routes.router, prefix="/cameras")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=17, username="test", role=role, is_active=True)
    captured = []
    monkeypatch.setattr(routes, "discovery_request", lambda path, body: captured.append((path, body)) or {"ok": True})
    with TestClient(app) as client:
        response = client.post("/cameras/discovery/jobs", json={"networks": ["192.168.10.5"]})
    assert response.status_code == (200 if allowed else 403)
    if allowed: assert captured[0][1]["owner"] == "17"
    else: assert captured == []


def test_request_cannot_override_actor_and_worker_errors_are_distinct(monkeypatch):
    app = FastAPI(); app.include_router(routes.router, prefix="/cameras")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=17, username="test", role="owner", is_active=True)
    with TestClient(app) as client:
        assert client.post("/cameras/discovery/jobs", json={"owner": "99"}).status_code == 422
        monkeypatch.setattr(routes, "discovery_request", lambda *args: (_ for _ in ()).throw(DiscoveryUnavailable()))
        response = client.get("/cameras/discovery/networks")
        assert response.status_code == 503 and response.json()["detail"]["code"] == "discovery_unavailable"
        monkeypatch.setattr(routes, "discovery_request", lambda *args: (_ for _ in ()).throw(DiscoveryRequestError(404, "discovery_not_found")))
        assert client.get("/cameras/discovery/jobs/unknown").status_code == 404
