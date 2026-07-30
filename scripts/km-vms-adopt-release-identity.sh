#!/usr/bin/env sh
set -eu

APP_DIR="${KM_VMS_APP_DIR:-}"
APPLY=0

usage() {
  cat <<'EOF'
KM VMS release-cycle closeout identity sync

Usage:
  sh scripts/km-vms-adopt-release-identity.sh [--app-dir <path>] [--apply]

Without --apply the command is a dry-run. With --apply it writes
.km-vms-release.json only after descriptor, tag and current source evidence
match. This is a release/publication closeout helper, not a normal user update.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app-dir)
      [ "$#" -ge 2 ] || fail "--app-dir requires a value"
      APP_DIR="$2"
      shift 2
      ;;
    --apply)
      APPLY=1
      shift
      ;;
    --check|--dry-run)
      APPLY=0
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

if [ -z "$APP_DIR" ]; then
  cwd=$(pwd -P)
  if [ -f "$cwd/docker-compose.yml" ] && [ -d "$cwd/apps/api" ]; then
    APP_DIR="$cwd"
  else
    APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
  fi
fi

[ -f "$APP_DIR/docker-compose.yml" ] || fail "App dir is not a KM VMS installation: $APP_DIR"

python3 - "$APP_DIR" "$APPLY" <<'PY'
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

root = Path(sys.argv[1]).resolve()
apply = sys.argv[2] == "1"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
SAFE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
MAX_PATCH_VERSION = 29


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def git_out(*args: str) -> str:
    try:
        return git(*args).stdout.strip()
    except subprocess.CalledProcessError as exc:
        fail(exc.stderr.strip() or exc.stdout.strip() or "git command failed")


def load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        fail(f"Invalid JSON: {path}")
    if not isinstance(payload, dict):
        fail(f"JSON root must be object: {path}")
    return payload


def parse_version(value: str) -> tuple[int, int, int]:
    base = value[1:] if value.startswith("v") else value
    main = re.split(r"[-+]", base, 1)[0]
    return tuple(int(part) for part in main.split("."))


def validate_descriptor() -> dict:
    descriptor_path = root / "release" / "km-vms-release.json"
    if not descriptor_path.is_file():
        fail("Release descriptor is missing: release/km-vms-release.json")
    descriptor = load_json(descriptor_path)
    version = str(descriptor.get("version") or "")
    if not SEMVER_RE.fullmatch(version):
        fail("release descriptor version must be semantic X.Y.Z")
    major, minor, patch = parse_version(version)
    if patch > MAX_PATCH_VERSION:
        fail(f"release descriptor patch must be <= {MAX_PATCH_VERSION}; use {major}.{minor + 1}.0 after {major}.{minor}.{MAX_PATCH_VERSION}")
    expected_tag = f"v{version}"
    tag = descriptor.get("tag") or descriptor.get("source_ref")
    if tag != expected_tag or descriptor.get("source_ref") != expected_tag or not SAFE_TAG_RE.fullmatch(expected_tag):
        fail("release descriptor tag/source_ref must match v<version>")
    repo = str(descriptor.get("source_repo") or "kmishnev87/km-vms")
    if not SAFE_REPO_RE.fullmatch(repo):
        fail("release descriptor source_repo must be OWNER/REPO")
    commit = descriptor.get("commit_sha")
    if commit is not None and not (isinstance(commit, str) and SHA_RE.fullmatch(commit)):
        fail("release descriptor commit_sha must be null or a full SHA")
    return descriptor


def tag_commit(tag: str) -> str:
    result = git("rev-parse", "--verify", f"{tag}^{{commit}}", check=False)
    if result.returncode != 0:
        fail(f"local release tag is missing or invalid: {tag}")
    commit = result.stdout.strip().lower()
    if not SHA_RE.fullmatch(commit):
        fail("local tag commit evidence is invalid")
    return commit


def remote_tag_commit(tag: str):
    result = git("ls-remote", "--tags", "origin", f"{tag}^{{}}", check=False)
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not line:
        result = git("ls-remote", "--tags", "origin", tag, check=False)
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not line:
        return None
    commit = line.split()[0].lower()
    return commit if SHA_RE.fullmatch(commit) else None


def github_release_exists(repo: str, tag: str):
    token_file = os.environ.get("KM_VMS_GITHUB_RELEASE_TOKEN_FILE", "").strip()
    token = os.environ.get("KM_VMS_GITHUB_RELEASE_TOKEN", "").strip()
    if token_file:
        path = Path(token_file)
        if not path.is_file():
            fail("GitHub release token file is not readable")
        token = path.read_text(encoding="utf-8").strip()
    if not token:
        return None
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "User-Agent": "km-vms-release-closeout",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            return response.status == 200 and payload.get("tag_name") == tag
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        fail(f"GitHub Release validation returned HTTP {exc.code}")
    except urllib.error.URLError:
        fail("GitHub Release validation request failed")


def write_identity(payload: dict) -> None:
    identity = root / ".km-vms-release.json"
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if identity.is_dir():
        fail(".km-vms-release.json is a directory")
    if identity.exists():
        identity.write_text(rendered, encoding="utf-8")
    else:
        identity.write_text(rendered, encoding="utf-8")


descriptor = validate_descriptor()
version = descriptor["version"]
tag = f"v{version}"
repo = descriptor.get("source_repo") or "kmishnev87/km-vms"
head = git_out("rev-parse", "HEAD").lower()
local_tag_commit = tag_commit(tag)
if head != local_tag_commit:
    fail("current Git HEAD does not match release tag commit; closeout identity sync is blocked")
remote_commit = remote_tag_commit(tag)
if remote_commit and remote_commit != local_tag_commit:
    fail("remote tag commit does not match local release tag commit")
descriptor_commit = descriptor.get("commit_sha")
if descriptor_commit and descriptor_commit.lower() != local_tag_commit:
    fail("descriptor commit_sha does not match release tag commit")
release_exists = github_release_exists(repo, tag)
if release_exists is False:
    fail("GitHub Release object is missing for the release tag")

identity_builder = root / "scripts" / "km-vms-release-identity.py"
if not identity_builder.is_file():
    fail("release identity builder is missing")
identity_result = subprocess.run(
    [
        sys.executable,
        str(identity_builder),
        "--descriptor",
        str(root / "release" / "km-vms-release.json"),
        "--commit",
        local_tag_commit,
        "--installed-at",
        datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "--installed-by",
        "release_cycle_closeout",
        "--metadata-status",
        "complete",
        "--metadata-source",
        "release_cycle_closeout",
    ],
    cwd=root,
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
if identity_result.returncode != 0:
    fail(
        identity_result.stderr.strip()
        or "release identity builder failed"
    )
try:
    identity_payload = json.loads(identity_result.stdout)
except Exception:
    fail("release identity builder returned invalid JSON")

print("KM VMS release-cycle closeout identity sync")
print(f"App dir: {root}")
print(f"Version: {version}")
print(f"Source repo: {repo}")
print(f"Source ref: {tag}")
print(f"Commit evidence: {local_tag_commit}")
print(f"GitHub Release validation: {'checked' if release_exists is True else 'skipped_no_token'}")

if not apply:
    print("Dry-run complete. No files were written.")
else:
    write_identity(identity_payload)
    print(f"Release identity written in-place: {root / '.km-vms-release.json'}")

ignored = git("check-ignore", ".km-vms-release.json", check=False)
if ignored.returncode != 0:
    fail(".km-vms-release.json is not ignored by git")
print("Closeout identity sync PASS")
PY
