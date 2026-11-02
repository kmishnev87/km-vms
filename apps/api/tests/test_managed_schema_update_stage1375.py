from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import update_check as check

ROOT = Path(__file__).resolve().parents[3]


def installed():
    return SimpleNamespace(status="installed", installed_version="0.8.22", installed_commit="a" * 40, git_head="a" * 40, identity_validity="validated")


def candidate(**overrides):
    payload = json.loads((ROOT / "release/km-vms-release.json").read_text(encoding="utf-8"))
    payload.update(version="0.8.23", tag="v0.8.23", source_ref="v0.8.23", commit_sha="b" * 40)
    payload.update(overrides)
    return payload


def frozen_compare():
    # Verbatim _compare from published f49dc769f9aaf066b81a74ba1fcba488b36d3d55.
    # Do not replace this fixture with current code to make a regression pass.
    raw = (ROOT / "apps/api/tests/fixtures/update/v0_8_22/update_compare.py.txt").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == "81d2e695b48ae4197de58ba8c008f5599023986d519109c000526cf142e4cb7c"
    namespace = dict(vars(check))
    exec(compile(raw, "published_v0_8_22_compare", "exec"), namespace)
    return namespace["_compare"]


def test_published_0822_blocks_out_of_band_but_admits_managed_schema_change():
    compare = frozen_compare()
    blocked_payload = candidate(requires_migration=True)
    blocked_payload.pop("schema_update")
    blocked = check._manifest_from_release_payload(blocked_payload)
    assert compare(installed(), blocked)[0] == "blocked"
    assert {entry.code for entry in compare(installed(), blocked)[1]} == {"requires_migration"}
    managed = check._manifest_from_release_payload(candidate())
    assert managed.schema_update == {"mode": "managed", "target_version": 9}
    assert managed.requires_migration is False
    assert compare(installed(), managed)[:2] == ("update_available", [])
    assert check._compare(installed(), managed)[:2] == ("update_available", [])


@pytest.mark.parametrize("flag", ("requires_backup", "requires_manual_action"))
def test_managed_metadata_does_not_bypass_other_release_guards(flag):
    manifest = check._manifest_from_release_payload(candidate(**{flag: True}))
    assert check._compare(installed(), manifest)[0] == "blocked"
    assert flag in {entry.code for entry in check._compare(installed(), manifest)[1]}


@pytest.mark.parametrize("value", ({"mode": "managed", "target_version": True}, {"mode": "skip", "target_version": 10}, {"mode": "managed", "target_version": 0}, {"mode": "managed", "target_version": 10, "bypass": True}, []))
def test_invalid_managed_schema_metadata_is_rejected(value):
    with pytest.raises(check.UpdateCheckBlocked):
        check._manifest_from_release_payload(candidate(schema_update=value))


def test_conflicting_migration_flags_do_not_silently_allow_apply():
    with pytest.raises(check.UpdateCheckBlocked):
        check._manifest_from_release_payload(candidate(requires_migration=True))


def test_next_release_registers_actual_tagged_schema_not_previous_family(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    services = tmp_path / "apps/api/app/services"
    services.mkdir(parents=True)
    schema_source = services / "schema_update_control.py"
    schema_source.write_text('TARGET_SCHEMA_VERSION = 10\nTARGET_SHAPE_FINGERPRINT = "' + 'c' * 64 + '"\nTARGET_SHAPE_FINGERPRINT_ALTERNATES = frozenset({"' + 'd' * 64 + '"})\n')
    lineage = {"schema_version": 1, "product": "KM VMS", "tag_commits": {"0.8.22": "a" * 40}, "schema_versions": {"0.8.22": 9}, "shape_fingerprints": {"0.8.22": "b" * 64}, "shape_alternates": {}}
    (release / "km-vms-update-lineage.json").write_text(json.dumps(lineage))
    for args in (["init", "-q"], ["config", "user.email", "test@example.invalid"], ["config", "user.name", "KM VMS test"], ["add", "."], ["commit", "-qm", "isolated tagged fixture"], ["tag", "v0.8.23"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    # Load the real helper's pure Python definitions, without running its CLI.
    script = (ROOT / "scripts/km-vms-release-cycle.sh").read_text(encoding="utf-8")
    python = script.split("<<'PY'\n", 1)[1].split("\nif prepare_version:", 1)[0]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["release-helper-test", "0", "0", "", "0", "1", ""])
    namespace = {}
    exec(compile(python, "release_helper_definitions", "exec"), namespace)
    descriptor = {"requires_migration": False, "schema_update": {"mode": "managed", "target_version": 10}}
    # Dirty future target must not replace the immutable released evidence.
    schema_source.write_text(schema_source.read_text().replace('"' + 'c' * 64 + '"', '"' + 'e' * 64 + '"'))
    result = namespace["register_current_release"]("0.8.23", descriptor, apply=True)
    assert result["schema_versions"]["0.8.23"] == 10
    assert result["shape_fingerprints"]["0.8.23"] == "c" * 64
    assert result["shape_alternates"]["0.8.23"] == ["d" * 64]
    assert result["schema_versions"]["0.8.22"] == 9
