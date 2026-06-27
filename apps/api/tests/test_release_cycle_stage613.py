import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_stage613_versions_are_consistent():
    version_py = (ROOT / "apps/api/app/core/version.py").read_text(encoding="utf-8")
    package = _json("apps/web/package.json")
    lock = _json("apps/web/package-lock.json")
    descriptor = _json("release/km-vms-release.json")

    assert 'APP_VERSION = "0.7.2"' in version_py
    assert package["version"] == "0.7.2"
    assert lock["version"] == "0.7.2"
    assert lock["packages"][""]["version"] == "0.7.2"
    assert descriptor["version"] == "0.7.2"


def test_stage613_release_descriptor_uses_semver_tag_evidence_model():
    descriptor = _json("release/km-vms-release.json")

    assert descriptor["schema_version"] == 1
    assert descriptor["product"] == "KM VMS"
    assert descriptor["version"] == "0.7.2"
    assert descriptor["tag"] == "v0.7.2"
    assert descriptor["source_ref"] == "v0.7.2"
    assert descriptor["source_repo"] == "kmishnev87/km-vms"
    assert descriptor["evidence_model"] == "semver_tag_resolves_to_commit"
    assert descriptor["commit_sha"] is None
    assert descriptor["published_at"] is None
    assert len(descriptor["changelog"]) <= 20
    assert all(isinstance(item, str) and len(item) <= 180 for item in descriptor["changelog"])


def test_stage613_release_cycle_script_check_and_dry_run_do_not_modify_files():
    tracked = [
        ROOT / "apps/api/app/core/version.py",
        ROOT / "apps/web/package.json",
        ROOT / "apps/web/package-lock.json",
        ROOT / "release/km-vms-release.json",
    ]
    before = {path: path.read_bytes() for path in tracked}

    check = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--check", "--allow-dirty"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    dry_run = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", "0.7.3", "--allow-dirty"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "release-cycle check PASS" in check.stdout
    assert "DRY-RUN: would prepare release version 0.7.3" in dry_run.stdout
    assert before == {path: path.read_bytes() for path in tracked}


def test_stage613_release_cycle_script_rejects_unsafe_and_equal_versions():
    equal = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", "0.7.2", "--allow-dirty"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    unsafe = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", "main", "--allow-dirty"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert equal.returncode != 0
    assert "must be greater" in equal.stderr
    assert unsafe.returncode != 0
    assert "semantic" in unsafe.stderr


def test_stage613_release_cycle_script_prints_publication_preview_only():
    result = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--print-github-release-commands", "--version", "0.7.2"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "git tag -a v0.7.2" in result.stdout
    assert "gh release create v0.7.2" in result.stdout
    assert "run only after operator acceptance" in result.stdout
