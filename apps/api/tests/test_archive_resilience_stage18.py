import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.core.config import settings
from app.models.recording import ArchiveRoot
from app.services.archive_root_provisioning import RESULT, REQUEST, provision_status, queue_provision
from app.services.camera_archive_placement import archive_target_status, storage_route_evidence
from recorder_core.archive_destination import Destination, select_route, writer_disk_problem


def disk(root, volume, dev, free, error=None):
    return Destination(root, volume, Path(f"/test/{root}"), free, dev, error)


def test_failover_uses_largest_available_bytes_on_a_different_volume_and_stays_there():
    primary = disk("home", "physical-a", 1, 0, "camera_archive_root_unavailable")
    same = disk("another-folder", "physical-a", 1, 10**15)
    largest = disk("largest", "physical-b", 2, 2**40)
    smaller = disk("smaller", "physical-c", 3, 2**30)
    read_only = disk("read-only", "physical-d", 4, 2**50, "camera_archive_root_read_only")
    all_roots = {item.root_id: item for item in (primary, same, largest, smaller, read_only)}
    assert select_route(primary, all_roots, None, allow_fallback=False, now=0).target is None
    assert select_route(primary, {"same": same}, None, allow_fallback=True, now=0).target is None
    route = select_route(primary, all_roots, None, allow_fallback=True, now=0)
    assert route.fallback and route.target == largest
    all_roots[smaller.root_id] = disk("smaller", "physical-c", 3, 2**45)
    assert select_route(primary, all_roots, route, allow_fallback=True, now=10).target == largest
    assert select_route(primary, all_roots, route, allow_fallback=False, now=11).target is None


def test_failover_waits_for_stable_recovery_and_uses_another_target_if_the_alternate_fails():
    primary = disk("a", "vol1", 1, 0, "camera_archive_root_unavailable")
    healthy = disk("a", "vol1", 1, 9)
    first = disk("b", "vol2", 2, 20)
    second = disk("c", "vol3", 3, 10)
    targets = {item.root_id: item for item in (primary, first, second)}
    route = select_route(primary, targets, None, allow_fallback=True, now=0)
    assert route.target == first
    assert select_route(primary, {"a": primary, "c": second}, route, allow_fallback=True, now=5).target == second
    targets["a"] = healthy
    route = select_route(healthy, targets, route, allow_fallback=True, now=10)
    assert route.target == first and route.healthy_since == 10
    assert select_route(healthy, targets, route, allow_fallback=True, now=39).target == first
    assert select_route(healthy, targets, route, allow_fallback=True, now=40).target == healthy


def test_network_failure_is_not_a_storage_fault():
    assert writer_disk_problem("RTSP: connection timed out") is None
    assert writer_disk_problem("camera RTSP 401 permission denied") is None
    assert writer_disk_problem("Error muxing packet: Input/output error") == "camera_archive_root_unavailable"
    assert writer_disk_problem("Error writing trailer: No space left on device") == "camera_archive_root_low_disk"


def test_archive_provision_is_not_visible_as_ready_until_verified_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_install_control", str(tmp_path / "control"))
    folder = tmp_path / "v1" / "test"
    (folder / "kmvms" / "recordings").mkdir(parents=True)
    monkeypatch.setattr("app.services.camera_archive_placement.archive_root_runtime_mount_path", lambda root: folder)
    root = ArchiveRoot(id="root_00000000000000000001", root_path=str(folder), label="Test Folder",
                       physical_identity="fixture-physical-volume")
    assert queue_provision(root, folder.parent)["status"] == "provisioning"
    req_file = Path(settings.storage_install_control) / REQUEST
    fields = dict(line.split("=", 1) for line in req_file.read_text().splitlines())
    assert not archive_target_status(root, "default")["writable"]
    result = Path(settings.storage_install_control) / RESULT
    result.write_text(json.dumps({"request_id": "wrong", "root_id": root.id, "status": "completed"}))
    assert provision_status(root.id)["status"] == "provisioning"
    result.write_text(json.dumps({"request_id": fields["request_id"], "root_id": root.id, "status": "completed"}))
    assert provision_status(root.id)["status"] == "completed"
    assert archive_target_status(root, "default")["state"] in {"ready", "low_space"}


def test_prepare_root_creates_only_the_approved_namespace_and_is_repeatable(tmp_path):
    script = Path(__file__).resolve().parents[3] / "scripts" / "km-vms-storage-candidate-validate.sh"
    folder = tmp_path / "Safe-folder"
    args = ("sh", str(script), "--folder-name", folder.name, "--prepare-archive", "/Volume1/Safe-folder")
    env = {**os.environ, "KM_VMS_SELECTED_MOUNT_CONTAINER": str(tmp_path)}
    subprocess.run(args, env=env, check=True, capture_output=True)
    subprocess.run(args, env=env, check=True, capture_output=True)
    assert (folder / "kmvms" / "recordings").is_dir()
    marker = json.loads((folder / ".km-vms-storage-root.json").read_text())
    assert marker["selected_host_path"] == "/Volume1/Safe-folder"
    assert marker["container_archive_path"] == "/storage/archive"
    other = tmp_path / "unrelated"
    other.mkdir()
    (other / "keep.txt").write_text("not owned")
    protected = subprocess.run(("sh", str(script), "--folder-name", "unrelated", "--prepare-archive", "/Volume1/unrelated"),
                               env=env, capture_output=True)
    assert protected.returncode and not (other / "kmvms").exists() and (other / "keep.txt").read_text() == "not owned"


def test_status_never_claims_recording_from_an_old_or_unconfirmed_writer():
    camera = SimpleNamespace(enabled=True, recording_storage_state={
        "instance": "a", "job_id": "writer", "actual_root_id": "tmp", "effective_root_id": "tmp",
        "primary_root_id": "physical-a", "status": "writing", "reason": "camera_archive_root_unavailable",
    })
    proof = {"heartbeat_current": True, "current_recorder_instance_id": "b", "confirmed_recording": True, "job_id": "writer"}
    assert storage_route_evidence(camera, proof) is None
    proof["current_recorder_instance_id"] = "a"
    assert storage_route_evidence(camera, proof)["actual_root_id"] == "tmp"
    proof["job_id"] = "new-writer"
    assert storage_route_evidence(camera, proof)["status"] == "waiting"
