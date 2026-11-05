"""Remove unused arbitrary-path surface; internal setup probe remains safe."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.main import app
from app.services import system_settings as service


def test_unused_arbitrary_path_route_is_not_exposed(tmp_path):
    assert all(item.path != "/settings/storage/validate" for item in ENDPOINT_PERMISSIONS)
    target = tmp_path / "must-not-create"
    response = TestClient(app).post("/settings/storage/validate", json={
        "storage_path": str(target), "create": True})
    assert response.status_code == 404
    assert not target.exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_internal_probe_does_not_modify_existing_file_or_symlink(tmp_path, symlink):
    protected = tmp_path / "protected"
    protected.write_bytes(b"keep exactly")
    old_probe = tmp_path / ".km_vms_write_test"
    if symlink:
        old_probe.symlink_to(protected)
    else:
        old_probe.write_bytes(b"existing probe is not ours")
    baseline = {p.name for p in tmp_path.iterdir()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.validate_storage_path(str(tmp_path)), range(8)))
    assert all(row["ok"] and row["writable"] and row["free_bytes"] > 0 for row in results)
    assert protected.read_bytes() == b"keep exactly"
    assert old_probe.read_bytes() == (b"keep exactly" if symlink else b"existing probe is not ours")
    assert old_probe.is_symlink() == symlink
    assert {p.name for p in tmp_path.iterdir()} == baseline


def test_internal_create_missing_and_empty_paths(tmp_path):
    missing = tmp_path / "internal-root"
    assert not service.validate_storage_path(str(missing), create=False)["ok"]
    assert not missing.exists()
    result = service.validate_storage_path(str(missing), create=True)
    assert result["ok"] and result["created"]
    assert list(missing.iterdir()) == []
    for empty in (None, "", "  "):
        result = service.validate_storage_path(empty)
        assert not result["ok"] and result["error"] == "storage_path is required"


def test_probe_is_closed_and_cleaned_if_write_fails(tmp_path, monkeypatch):
    original = service.tempfile.TemporaryFile
    opened = []

    class FailingWrite:
        def __enter__(self):
            self.file = original(dir=tmp_path, prefix=".km_vms_write_test.")
            opened.append(self.file)
            return self

        def write(self, _data):
            raise OSError("synthetic write failure")

        def __exit__(self, *args):
            self.file.close()

    monkeypatch.setattr(service.tempfile, "TemporaryFile", lambda **kwargs: FailingWrite())
    result = service.validate_storage_path(str(tmp_path))
    assert not result["ok"] and not result["writable"]
    assert all(file.closed for file in opened)
    assert list(tmp_path.iterdir()) == []
