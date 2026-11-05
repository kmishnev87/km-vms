"""Run the real installer acquisition function against an offline moving ref."""
import os
import re
import subprocess
from pathlib import Path

import pytest


INSTALLER = Path(__file__).resolve().parents[3] / "scripts" / "install.sh"


def function(name):
    script = INSTALLER.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", script, re.M | re.S)
    assert match
    return match.group(0)


def acquire(tmp_path, *, sha="a" * 40):
    # The second API call would return B: only one lookup is permitted. No
    # network, Docker, user paths, private credentials, or real archive involved.
    harness = r'''
set -eu
BRANCH=main
GITHUB_REPO=example/project
APP_DIR="$TEST_TMP/app"
mkdir "$APP_DIR"
km_vms_command_exists() { command -v "$1" >/dev/null; }
validate_github_repo() { printf '%s' "$1"; }
load_github_token() { :; }
prepare_github_token_config() { :; }
clear_github_token() { printf 'clear\n' >> "$TEST_TMP/events"; }
fail() { printf '%s\n' "$1" >&2; exit 9; }
acquisition_fail() { fail "$1"; }
github_api_text() {
  printf 'lookup\n' >> "$TEST_TMP/events"
  if [ "$(wc -l < "$TEST_TMP/events")" -eq 1 ]; then
    printf '  "sha": "%s",\n' "$TEST_SHA"
  else
    printf '  "sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",\n'
  fi
}
http_download() { printf '%s\n' "$1" >> "$TEST_TMP/events"; : > "$2"; }
safe_extract_tarball() { printf 'extract:%s\n' "$SOURCE_COMMIT_SHA" >> "$TEST_TMP/events"; }
'''
    harness += function("resolve_github_commit_sha") + "\n" + function("acquire_github_tarball")
    harness += '\nacquire_github_tarball\nprintf "identity:%s\\n" "$SOURCE_COMMIT_SHA"\n'
    return subprocess.run(["sh", "-c", harness], env={**os.environ,
        "TEST_TMP": str(tmp_path), "TMPDIR": str(tmp_path), "TEST_SHA": sha}, text=True, capture_output=True)


def test_moving_branch_is_resolved_before_download_and_fetched_by_exact_sha(tmp_path):
    result = acquire(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "events").read_text().splitlines() == [
        "lookup", "https://api.github.com/repos/example/project/tarball/" + "a" * 40,
        "extract:" + "a" * 40, "clear"]
    assert result.stdout.strip() == "identity:" + "a" * 40
    assert list(tmp_path.glob("km-vms-source*")) == []


@pytest.mark.parametrize("sha", ["", "bad", "a" * 39, "a" * 41, "z" * 40])
def test_unresolved_or_invalid_commit_is_not_downloaded_or_claimed(tmp_path, sha):
    result = acquire(tmp_path, sha=sha)
    assert result.returncode != 0
    assert (tmp_path / "events").read_text().splitlines() == ["lookup", "clear"]
    assert "identity:" not in result.stdout
