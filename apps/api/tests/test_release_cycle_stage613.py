import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _release_env_with_safe_getfacl(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "permission-tools"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / "getfacl"
    tool.write_text(
        "#!/usr/bin/env sh\n"
        "printf 'user::rwx\ngroup::r-x\nother::r-x\n'\n",
        encoding="utf-8",
    )
    os.chmod(tool, 0o755)
    return {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}


def _json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _current_version() -> str:
    return _json("release/km-vms-release.json")["version"]


def _next_patch(version: str) -> str:
    major, minor, patch = (int(item) for item in version.split("."))
    assert patch < 29
    return f"{major}.{minor}.{patch + 1}"


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
    assert set(descriptor["title_i18n"]) == {"en", "ru", "zh-CN"}
    assert set(descriptor["summary_i18n"]) == {"en", "ru", "zh-CN"}
    assert set(descriptor["changelog_i18n"]) == {"en", "ru", "zh-CN"}
    assert len(descriptor["changelog"]) <= 20
    assert all(isinstance(item, str) and len(item) <= 180 for item in descriptor["changelog"])


def test_stage613_release_cycle_script_check_and_dry_run_do_not_modify_files(tmp_path):
    env = _release_env_with_safe_getfacl(tmp_path)
    tracked = [
        ROOT / "apps/api/app/core/version.py",
        ROOT / "apps/web/package.json",
        ROOT / "apps/web/package-lock.json",
        ROOT / "release/km-vms-release.json",
        ROOT / "release/km-vms-update-lineage.json",
    ]
    before = {path: path.read_bytes() for path in tracked}
    current = _current_version()
    target = _next_patch(current)

    check = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--check", "--allow-dirty"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    dry_run = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", target, "--allow-dirty"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "release-cycle check PASS" in check.stdout
    assert f"DRY-RUN: would register current schema-equivalent release {current}" in dry_run.stdout
    assert f"DRY-RUN: would prepare release version {target}" in dry_run.stdout
    assert "would invalidate title, summary, changelog and localized release notes" in dry_run.stdout
    assert before == {path: path.read_bytes() for path in tracked}


def test_stage1379_prepare_invalidates_old_notes_until_new_payload_is_written(
    tmp_path: Path,
):
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "apps/api/app/core").mkdir(parents=True)
    (tmp_path / "apps/web").mkdir(parents=True)
    (tmp_path / "release").mkdir(parents=True)
    script = tmp_path / "scripts/km-vms-release-cycle.sh"
    script.write_text(
        (ROOT / "scripts/km-vms-release-cycle.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    gate = tmp_path / "scripts/km-vms-permission-gate.sh"
    gate.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    os.chmod(script, 0o755)
    os.chmod(gate, 0o755)

    current = "0.1.1"
    target = "0.1.2"
    (tmp_path / "apps/api/app/core/version.py").write_text(
        f'APP_VERSION = "{current}"\n',
        encoding="utf-8",
    )
    (tmp_path / "apps/web/package.json").write_text(
        json.dumps({"version": current}),
        encoding="utf-8",
    )
    (tmp_path / "apps/web/package-lock.json").write_text(
        json.dumps(
            {
                "version": current,
                "packages": {"": {"version": current}},
            }
        ),
        encoding="utf-8",
    )
    notes = {
        "title": "Old release title",
        "summary": "Old release summary",
        "changelog": ["Old release change"],
        "title_i18n": {locale: f"{locale} old title" for locale in ("en", "ru", "zh-CN")},
        "summary_i18n": {locale: f"{locale} old summary" for locale in ("en", "ru", "zh-CN")},
        "changelog_i18n": {locale: [f"{locale} old change"] for locale in ("en", "ru", "zh-CN")},
    }
    descriptor = {
        "schema_version": 1,
        "product": "KM VMS",
        "version": current,
        "tag": f"v{current}",
        **notes,
        "release_channel": "public-github",
        "source_kind": "github-release",
        "source_repo": "kmishnev87/km-vms",
        "source_ref": f"v{current}",
        "evidence_model": "semver_tag_resolves_to_commit",
        "commit_sha": None,
        "published_at": None,
        "requires_backup": False,
        "requires_manual_action": False,
        "requires_migration": False,
    }
    (tmp_path / "release/km-vms-release.json").write_text(
        json.dumps(descriptor, ensure_ascii=False),
        encoding="utf-8",
    )
    lineage = {
        "schema_version": 1,
        "product": "KM VMS",
        "tag_commits": {"0.1.0": "a" * 40},
        "schema_versions": {"0.1.0": 9},
        "shape_fingerprints": {"0.1.0": "b" * 64},
        "shape_alternates": {},
    }
    (tmp_path / "release/km-vms-update-lineage.json").write_text(
        json.dumps(lineage),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "KM VMS Test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "tag", f"v{current}"],
        cwd=tmp_path,
        check=True,
    )

    subprocess.run(
        [
            "sh",
            "scripts/km-vms-release-cycle.sh",
            "--prepare-version",
            target,
            "--allow-dirty",
        ],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    prepared = json.loads(
        (tmp_path / "release/km-vms-release.json").read_text(
            encoding="utf-8",
        )
    )
    assert prepared["title"] == ""
    assert prepared["summary"] == ""
    assert prepared["changelog"] == []
    assert not {
        "title_i18n",
        "summary_i18n",
        "changelog_i18n",
    }.intersection(prepared)

    blocked = subprocess.run(
        [
            "sh",
            "scripts/km-vms-release-cycle.sh",
            "--check",
            "--allow-dirty",
        ],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert blocked.returncode != 0
    assert "title is invalid" in blocked.stderr

    prepared.update(
        {
            "title": "New release title",
            "summary": "New release summary",
            "changelog": ["New release change"],
            "title_i18n": {
                locale: f"{locale} new title"
                for locale in ("en", "ru", "zh-CN")
            },
            "summary_i18n": {
                locale: f"{locale} new summary"
                for locale in ("en", "ru", "zh-CN")
            },
            "changelog_i18n": {
                locale: [f"{locale} new change"]
                for locale in ("en", "ru", "zh-CN")
            },
        }
    )
    (tmp_path / "release/km-vms-release.json").write_text(
        json.dumps(prepared, ensure_ascii=False),
        encoding="utf-8",
    )
    accepted = subprocess.run(
        [
            "sh",
            "scripts/km-vms-release-cycle.sh",
            "--check",
            "--allow-dirty",
        ],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "release-cycle check PASS" in accepted.stdout

    rejected_lineage = json.loads(
        (tmp_path / "release/km-vms-update-lineage.json").read_text(
            encoding="utf-8",
        )
    )
    latest = list(rejected_lineage["schema_versions"])[-1]
    rejected_lineage["schema_versions"][latest] = 10
    (tmp_path / "release/km-vms-update-lineage.json").write_text(
        json.dumps(rejected_lineage),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [
            "sh",
            "scripts/km-vms-release-cycle.sh",
            "--check",
            "--allow-dirty",
        ],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert rejected.returncode != 0
    assert "update lineage evidence is invalid" in rejected.stderr


def test_stage613_release_cycle_script_rejects_unsafe_and_equal_versions(tmp_path):
    env = _release_env_with_safe_getfacl(tmp_path)
    current = _current_version()
    equal = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", current, "--allow-dirty"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    unsafe = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", "main", "--allow-dirty"],
        cwd=ROOT,
        env=env,
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
        ["sh", "scripts/km-vms-release-cycle.sh", "--print-github-release-commands", "--version", "0.8.0"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "git tag -a v0.8.0" in result.stdout
    assert "gh release create" not in result.stdout
    assert "sh scripts/km-vms-publish-github-release.sh --check --tag v0.8.0" in result.stdout
    assert "sh scripts/km-vms-publish-github-release.sh --publish --tag v0.8.0" in result.stdout
    assert "KM_VMS_GITHUB_RELEASE_TOKEN_FILE=data/update-control/.github-release-token" in result.stdout
    assert "/secure/path/km-vms-github-release-token" not in result.stdout
    assert "trusted commit evidence is the validated semver tag commit" in result.stdout
    assert "sh scripts/km-vms-release-cycle.sh --sync-local-release-identity --apply" in result.stdout
    assert "http://127.0.0.1:${HTTP_PORT:-8088}/api/system/update/status" in result.stdout
    assert "installed_release.version/title/commit_sha" in result.stdout
    assert "run only after operator acceptance" in result.stdout


def test_stage630_release_cycle_script_enforces_patch_cap_for_prepare_and_print(tmp_path):
    env = _release_env_with_safe_getfacl(tmp_path)
    too_high_prepare = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--dry-run", "--prepare-version", "0.7.30", "--allow-dirty"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    too_high_print = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--print-github-release-commands", "--version", "0.7.30"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    next_minor = subprocess.run(
        ["sh", "scripts/km-vms-release-cycle.sh", "--print-github-release-commands", "--version", "0.8.0"],
        cwd=ROOT,
        env=env,
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
