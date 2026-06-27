import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts/km-vms-publish-github-release.sh"
DOCS = ROOT / "docs/INSTALL.md"
SHA = "8fcc5d8a56e16613069edbb5ac796db62bddb4c0"


def _fake_git_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        f"""#!/usr/bin/env sh
set -eu
case "$1 $2 $3" in
  "remote get-url origin") printf '%s\\n' 'https://github.com/kmishnev87/km-vms.git' ;;
  "rev-parse --verify v0.7.3^{{commit}}") printf '%s\\n' '{SHA}' ;;
  "ls-remote --tags origin")
    if [ "$4" = "v0.7.3^{{}}" ]; then
      printf '%s\\t%s\\n' '{SHA}' 'refs/tags/v0.7.3^{{}}'
    elif [ "$4" = "v0.7.3" ]; then
      printf '%s\\t%s\\n' '{SHA}' 'refs/tags/v0.7.3'
    fi
    ;;
  "merge-base --is-ancestor {SHA}") exit 0 ;;
  "rev-parse HEAD") printf '%s\\n' '{SHA}' ;;
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
    result = subprocess.run(
        ["sh", "scripts/km-vms-publish-github-release.sh", "--check", "--tag", "v0.7.3"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_fake_git_env(tmp_path),
    )

    assert result.returncode == 0
    assert "descriptor version 0.7.3 matches v0.7.3" in result.stdout


def test_stage6141_check_with_mismatched_tag_fails_before_release_lookup(tmp_path):
    result = subprocess.run(
        ["sh", "scripts/km-vms-publish-github-release.sh", "--check", "--tag", "v0.7.2"],
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
    assert "dereferenced tag commit evidence" in text


def test_stage6141_docs_mention_publish_helper_and_token_file_contract():
    docs = DOCS.read_text(encoding="utf-8")

    assert "km-vms-publish-github-release.sh" in docs
    assert "KM_VMS_GITHUB_RELEASE_TOKEN_FILE" in docs
    assert "after commit/tag/push" in docs
    assert "--tag` is used, it must match the current" in docs
