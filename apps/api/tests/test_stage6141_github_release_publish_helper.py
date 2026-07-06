import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts/km-vms-publish-github-release.sh"
DOCS = ROOT / "docs/INSTALL.md"
SHA = "8fcc5d8a56e16613069edbb5ac796db62bddb4c0"


def _current_tag():
    descriptor = json.loads((ROOT / "release/km-vms-release.json").read_text(encoding="utf-8"))
    return descriptor["tag"]


def _fake_git_env(tmp_path, *, tag: str | None = None, sha: str = SHA):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    tag = tag or _current_tag()
    git.write_text(
        f"""#!/usr/bin/env sh
set -eu
case "$1 $2 $3" in
  "remote get-url origin") printf '%s\\n' 'https://github.com/kmishnev87/km-vms.git' ;;
  "rev-parse --verify {tag}^{{commit}}") printf '%s\\n' '{sha}' ;;
  "ls-remote --tags origin")
    if [ "$4" = "{tag}^{{}}" ]; then
      printf '%s\\t%s\\n' '{sha}' 'refs/tags/{tag}^{{}}'
    elif [ "$4" = "{tag}" ]; then
      printf '%s\\t%s\\n' '{sha}' 'refs/tags/{tag}'
    fi
    ;;
  "merge-base --is-ancestor {sha}") exit 0 ;;
  "rev-parse HEAD") printf '%s\\n' '{sha}' ;;
  *) printf 'unexpected git args: %s\\n' "$*" >&2; exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    git.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env.pop("KM_VMS_GITHUB_RELEASE_TOKEN_FILE", None)
    env.pop("KM_VMS_GITHUB_RELEASE_TOKEN", None)
    env.pop("KM_VMS_GITHUB_REPO", None)
    return env


def _write_descriptor(root: Path, *, version: str = "0.7.4", tag: str = "v0.7.4", commit_sha=None) -> Path:
    release_dir = root / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    path = release_dir / "km-vms-release.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "tag": tag,
                "source_ref": tag,
                "source_repo": "kmishnev87/km-vms",
                "commit_sha": commit_sha,
                "changelog": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _fake_github_sitecustomize(tmp_path: Path, *, tag: str = "v0.7.4") -> dict[str, str]:
    site_dir = tmp_path / "sitecustomize"
    site_dir.mkdir()
    (site_dir / "sitecustomize.py").write_text(
        f"""
import json
import urllib.request

class Response:
    status = 200
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def read(self):
        return json.dumps(self.payload).encode("utf-8")

def fake_urlopen(request, timeout=20):
    url = getattr(request, "full_url", str(request))
    if url.endswith("/releases/tags/{tag}"):
        return Response({{"tag_name": "{tag}"}})
    return Response({{"ok": True}})

urllib.request.urlopen = fake_urlopen
""",
        encoding="utf-8",
    )
    return {"PYTHONPATH": str(site_dir)}


def test_stage6141_publish_helper_exists_and_exposes_expected_modes():
    text = HELPER.read_text(encoding="utf-8")

    assert HELPER.is_file()
    assert "--check" in text
    assert "--publish" in text
    assert "--tag" in text
    assert "--help" in text
    assert "KM_VMS_GITHUB_RELEASE_TOKEN_FILE" in text
    assert "KM_VMS_GITHUB_RELEASE_TOKEN" in text


def test_stage6141_publish_helper_has_no_jq_dependency_or_token_print_contracts():
    text = HELPER.read_text(encoding="utf-8")

    assert " jq" not in text
    assert "jq " not in text
    assert "echo $KM_VMS_GITHUB_RELEASE_TOKEN" not in text
    assert "printf \"$KM_VMS_GITHUB_RELEASE_TOKEN" not in text
    assert "print(token" not in text
    assert "never prints token values" in text


def test_stage6141_publish_helper_help_works():
    result = subprocess.run(
        ["sh", "scripts/km-vms-publish-github-release.sh", "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "GitHub Release publication helper" in result.stdout
    assert "--check" in result.stdout
    assert "--publish" in result.stdout


def test_stage6141_check_does_not_require_token(tmp_path):
    result = subprocess.run(
        ["sh", "scripts/km-vms-publish-github-release.sh", "--check"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_fake_git_env(tmp_path),
    )

    assert result.returncode == 0
    assert "authenticated GitHub Release validation skipped" in result.stdout
    assert "PASS: local/public release checks complete" in result.stdout


def test_stage6141_check_with_matching_tag_works(tmp_path):
    tag = _current_tag()
    result = subprocess.run(
        ["sh", "scripts/km-vms-publish-github-release.sh", "--check", "--tag", tag],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_fake_git_env(tmp_path),
    )

    assert result.returncode == 0
    assert f"matches {tag}" in result.stdout


def test_stage6141_check_with_mismatched_tag_fails_before_release_lookup(tmp_path):
    mismatched_tag = "v0.0.1" if _current_tag() != "v0.0.1" else "v0.0.2"
    result = subprocess.run(
        ["sh", "scripts/km-vms-publish-github-release.sh", "--check", "--tag", mismatched_tag],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_fake_git_env(tmp_path),
    )

    assert result.returncode != 0
    assert "requested tag must match current release descriptor" in result.stderr


def test_stage6141_publish_requires_token_fail_closed_without_printing_secret(tmp_path):
    result = subprocess.run(
        ["sh", "scripts/km-vms-publish-github-release.sh", "--publish"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_fake_git_env(tmp_path),
    )

    assert result.returncode != 0
    assert "GitHub release token is required for --publish" in result.stderr
    assert "Authorization" not in result.stdout + result.stderr


def test_stage6141_release_validation_uses_tag_commit_evidence_not_target_commitish_only():
    text = HELPER.read_text(encoding="utf-8")

    assert "target_commitish" not in text
    assert "remote_tag_commit" in text
    assert "tag commit evidence" in text
    assert "immutable semver tag" in text


def test_stage650_publish_helper_uses_tag_evidence_without_descriptor_mutation():
    text = HELPER.read_text(encoding="utf-8")

    assert "write_descriptor_commit_evidence" not in text
    assert "release descriptor commit_sha stamped" not in text
    assert "trusted commit evidence is resolved from the immutable semver tag" in text


def test_stage650_check_accepts_explicit_descriptor_commit(tmp_path):
    tag = "v0.7.4"
    _write_descriptor(tmp_path, tag=tag, commit_sha=SHA)
    env = _fake_git_env(tmp_path, tag=tag)

    result = subprocess.run(
        ["sh", str(HELPER), "--check", "--tag", tag],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    assert result.returncode == 0
    assert "descriptor commit_sha is null" not in result.stdout
    assert "PASS: local/public release checks complete" in result.stdout


def test_stage650_check_accepts_null_descriptor_commit_only_with_tag_evidence(tmp_path):
    tag = "v0.7.4"
    descriptor = _write_descriptor(tmp_path, tag=tag, commit_sha=None)
    before = descriptor.read_text(encoding="utf-8")
    result = subprocess.run(
        ["sh", str(HELPER), "--check", "--tag", tag],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_fake_git_env(tmp_path, tag=tag),
    )

    assert result.returncode == 0
    assert "trusted commit evidence is resolved from the immutable semver tag" in result.stdout
    assert descriptor.read_text(encoding="utf-8") == before


def test_stage650_explicit_descriptor_commit_mismatch_blocks(tmp_path):
    tag = "v0.7.4"
    _write_descriptor(tmp_path, tag=tag, commit_sha="a" * 40)
    result = subprocess.run(
        ["sh", str(HELPER), "--check", "--tag", tag],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_fake_git_env(tmp_path, tag=tag, sha=SHA),
    )

    assert result.returncode != 0
    assert "descriptor commit_sha does not match tag commit evidence" in result.stderr


def test_stage650_publish_validation_leaves_descriptor_clean_with_tag_fallback(tmp_path):
    tag = "v0.7.4"
    descriptor = _write_descriptor(tmp_path, tag=tag, commit_sha=None)
    before = descriptor.read_text(encoding="utf-8")
    env = _fake_git_env(tmp_path, tag=tag)
    env.update(_fake_github_sitecustomize(tmp_path, tag=tag))
    env["KM_VMS_GITHUB_RELEASE_TOKEN"] = "dummy-token-value"

    result = subprocess.run(
        ["sh", str(HELPER), "--publish", "--tag", tag],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    assert result.returncode == 0
    assert "PASS: publish validation complete" in result.stdout
    assert descriptor.read_text(encoding="utf-8") == before


def test_stage6141_docs_mention_publish_helper_and_token_file_contract():
    docs = DOCS.read_text(encoding="utf-8")

    assert "km-vms-publish-github-release.sh" in docs
    assert "KM_VMS_GITHUB_RELEASE_TOKEN_FILE" in docs
    assert "after commit/tag/push" in docs
    assert "--tag` is used, it must match the current" in docs
    assert "does not mutate product files after publication" in docs
    assert "commit_sha` is present, it must match the resolved tag commit" in docs
