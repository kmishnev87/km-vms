import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _current_version() -> str:
    return _json("release/km-vms-release.json")["version"]


def test_stage613_versions_are_consistent():
    version_py = (ROOT / "apps/api/app/core/version.py").read_text(encoding="utf-8")
    package = _json("apps/web/package.json")
    lock = _json("apps/web/package-lock.json")
    descriptor = _json("release/km-vms-release.json")
    version = descriptor["version"]

    assert f'APP_VERSION = "{version}"' in version_py
    assert package["version"] == version
    assert lock["version"] == version
    assert lock["packages"][""]["version"] == version


def test_stage613_release_descriptor_uses_semver_tag_evidence_model():
    descriptor = _json("release/km-vms-release.json")
    version = descriptor["version"]

    assert descriptor["schema_version"] == 1
    assert descriptor["product"] == "KM VMS"
    assert descriptor["tag"] == f"v{version}"
    assert descriptor["source_ref"] == f"v{version}"
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
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", "0.7.29", "--allow-dirty"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "release-cycle check PASS" in check.stdout
    assert "DRY-RUN: would prepare release version 0.7.29" in dry_run.stdout
    assert before == {path: path.read_bytes() for path in tracked}


def test_stage613_release_cycle_script_rejects_unsafe_and_equal_versions():
    current = _current_version()
    equal = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", current, "--allow-dirty"],
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
        ["sh", "scripts/km-vms-release-cycle.sh", "--print-github-release-commands", "--version", "0.7.29"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "git tag -a v0.7.29" in result.stdout
    assert "gh release create" not in result.stdout
    assert "sh scripts/km-vms-publish-github-release.sh --check --tag v0.7.29" in result.stdout
    assert "sh scripts/km-vms-publish-github-release.sh --publish --tag v0.7.29" in result.stdout
    assert "run only after operator acceptance" in result.stdout


def test_stage630_release_cycle_script_enforces_patch_cap_for_prepare_and_print():
    too_high_prepare = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", "0.7.30", "--allow-dirty"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    too_high_print = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--print-github-release-commands", "--version", "0.7.30"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    next_minor = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--print-github-release-commands", "--version", "0.8.0"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert too_high_prepare.returncode != 0
    assert "after 0.7.29 use 0.8.0" in too_high_prepare.stderr
    assert too_high_print.returncode != 0
    assert "after 0.7.29 use 0.8.0" in too_high_print.stderr
    assert "git tag -a v0.8.0" in next_minor.stdout


def test_stage630_release_cycle_script_check_rejects_descriptor_patch_above_cap(tmp_path):
    script = tmp_path / "scripts" / "km-vms-release-cycle.sh"
    script.parent.mkdir(parents=True)
    script.write_text((ROOT / "scripts/km-vms-release-cycle.sh").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "apps/api/app/core").mkdir(parents=True)
    (tmp_path / "apps/web").mkdir(parents=True)
    (tmp_path / "release").mkdir(parents=True)
    (tmp_path / "apps/api/app/core/version.py").write_text('APP_VERSION = "0.7.30"\n', encoding="utf-8")
    package = {"version": "0.7.30"}
    (tmp_path / "apps/web/package.json").write_text(json.dumps(package), encoding="utf-8")
    (tmp_path / "apps/web/package-lock.json").write_text(json.dumps({"version": "0.7.30", "packages": {"": {"version": "0.7.30"}}}), encoding="utf-8")
    (tmp_path / "release/km-vms-release.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "0.7.30",
                "tag": "v0.7.30",
                "source_ref": "v0.7.30",
                "source_repo": "kmishnev87/km-vms",
                "evidence_model": "semver_tag_resolves_to_commit",
                "commit_sha": None,
                "changelog": [],
                "requires_backup": False,
                "requires_manual_action": False,
                "requires_migration": False,
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--check", "--allow-dirty"],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode != 0
    assert "after 0.7.29 use 0.8.0" in result.stderr
