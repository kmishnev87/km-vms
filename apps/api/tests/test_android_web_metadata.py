"""The web download is a snapshot of its installed NAS release, never moving main."""
import ast
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _fixture(tmp_path):
    source = tmp_path / "apps/android/release.json"
    source.parent.mkdir(parents=True)
    value = {"format": 1, "package_name": "ru.kmvms.mobile", "published": False}
    source.write_text(json.dumps(value), encoding="utf8")
    script = ROOT / "scripts/km-vms-release-cycle.sh"
    return source, value, script


def test_android_snapshot_sync_and_dry_run(tmp_path):
    source, value, script = _fixture(tmp_path)
    snapshot = tmp_path / "apps/web/public/android-release.json"
    result = subprocess.run(["sh", str(script), "--sync-android-metadata", "--dry-run"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not snapshot.exists()
    result = subprocess.run(["sh", str(script), "--sync-android-metadata"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(snapshot.read_text(encoding="utf8")) == value
    value.update(published=True, version_name="1.2.3")
    source.write_text(json.dumps(value), encoding="utf8")
    result = subprocess.run(["sh", str(script), "--sync-android-metadata"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(snapshot.read_text(encoding="utf8")) == value


def test_release_check_detects_a_stale_android_web_snapshot(tmp_path):
    source, value, script = _fixture(tmp_path)
    embedded = script.read_text(encoding="utf8").split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    wanted = {"fail", "load_json", "android_metadata", "check_android_web_metadata"}
    nodes = [node for node in ast.parse(embedded).body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    import sys
    scope = {"ROOT": tmp_path, "Path": Path, "json": json, "sys": sys}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "release-helper-functions", "exec"), scope)
    with pytest.raises(SystemExit):
        scope["check_android_web_metadata"]()
    snapshot = tmp_path / "apps/web/public/android-release.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps(value), encoding="utf8")
    scope["check_android_web_metadata"]()
    source.write_text(json.dumps({**value, "published": True}), encoding="utf8")
    with pytest.raises(SystemExit):
        scope["check_android_web_metadata"]()


def test_preparing_and_checking_releases_include_the_snapshot_contract():
    text = (ROOT / "scripts/km-vms-release-cycle.sh").read_text(encoding="utf8")
    prepare = text.split("def prepare(version: str)", 1)[1].split("def print_release_commands", 1)[0]
    check = text.split("if check:\n", 1)[1].split("if print_commands:", 1)[0]
    assert "sync_android_web_metadata()" in prepare
    assert "check_android_web_metadata()" in check
