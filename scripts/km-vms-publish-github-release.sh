#!/usr/bin/env sh
set -eu

usage() {
  cat <<'EOF'
KM VMS GitHub Release publication helper

Usage:
  sh scripts/km-vms-publish-github-release.sh --check [--tag vX.Y.Z]
  sh scripts/km-vms-publish-github-release.sh --publish [--tag vX.Y.Z]
  sh scripts/km-vms-publish-github-release.sh --help

Token sources for --publish:
  KM_VMS_GITHUB_RELEASE_TOKEN_FILE
  KM_VMS_GITHUB_RELEASE_TOKEN

This helper validates release descriptor, tag and commit evidence. It does not
create commits or tags, and it never prints token values.
EOF
}

MODE=""
TAG_OVERRIDE=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check)
      [ -z "$MODE" ] || { printf '%s\n' "Only one mode can be selected" >&2; exit 2; }
      MODE="check"; shift ;;
    --publish)
      [ -z "$MODE" ] || { printf '%s\n' "Only one mode can be selected" >&2; exit 2; }
      MODE="publish"; shift ;;
    --tag)
      [ "$#" -ge 2 ] || { printf '%s\n' "--tag requires a value" >&2; exit 2; }
      TAG_OVERRIDE="$2"; shift 2 ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2 ;;
  esac
done

[ -n "$MODE" ] || { usage >&2; exit 2; }

python3 - "$MODE" "$TAG_OVERRIDE" <<'PY'
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

mode = sys.argv[1]
tag_override = sys.argv[2]
root = Path.cwd()

SAFE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def warn(message: str) -> None:
    print(f"WARN: {message}")


def info(message: str) -> None:
    print(f"INFO: {message}")


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def git_out(*args: str) -> str:
    try:
        return run_git(*args).stdout.strip()
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "git command failed"
        fail(message)


def load_descriptor() -> dict:
    path = root / "release/km-vms-release.json"
    if not path.is_file():
        fail("release descriptor is missing: release/km-vms-release.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        fail("release descriptor is not valid JSON")


def resolve_repo() -> str:
    env_repo = os.environ.get("KM_VMS_GITHUB_REPO", "").strip()
    if env_repo:
        if not SAFE_REPO_RE.fullmatch(env_repo):
            fail("KM_VMS_GITHUB_REPO must be OWNER/REPO")
        return env_repo

    remote = git_out("remote", "get-url", "origin")
    patterns = [
        r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$",
        r"^git@github\.com-[^:]+:([^/]+)/(.+?)(?:\.git)?$",
        r"^https://github\.com/([^/]+)/(.+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/]+)/(.+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, remote)
        if match:
            repo = f"{match.group(1)}/{match.group(2)}"
            if SAFE_REPO_RE.fullmatch(repo):
                return repo
    fail("cannot determine GitHub repo from KM_VMS_GITHUB_REPO or origin remote")


def token_from_env() -> str | None:
    token_file = os.environ.get("KM_VMS_GITHUB_RELEASE_TOKEN_FILE", "").strip()
    if token_file:
        path = Path(token_file)
        if not path.is_file():
            fail("GitHub release token file is not readable")
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            fail("GitHub release token file is empty")
        return token
    token = os.environ.get("KM_VMS_GITHUB_RELEASE_TOKEN", "").strip()
    return token or None


def api_request(repo: str, path: str, token: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict | None]:
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "User-Agent": "km-vms-release-helper",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content = response.read().decode("utf-8")
            return response.status, json.loads(content) if content else None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, None
        if exc.code in {401, 403, 500, 502, 503, 504}:
            fail(f"GitHub API returned HTTP {exc.code}")
        fail(f"GitHub API returned HTTP {exc.code}")
    except urllib.error.URLError:
        fail("GitHub API request failed")


def tag_commit(tag: str) -> str:
    local = run_git("rev-parse", "--verify", f"{tag}^{{commit}}", check=False)
    if local.returncode != 0:
        fail(f"local tag is missing or invalid: {tag}")
    commit = local.stdout.strip()
    if not SHA_RE.fullmatch(commit):
        fail("local tag commit evidence is invalid")
    return commit


def remote_tag_commit(tag: str) -> str:
    deref = git_out("ls-remote", "--tags", "origin", f"{tag}^{{}}")
    if deref:
        commit = deref.split()[0]
    else:
        direct = git_out("ls-remote", "--tags", "origin", tag)
        if not direct:
            fail(f"remote tag is missing: {tag}")
        commit = direct.split()[0]
        obj_type = run_git("cat-file", "-t", commit, check=False)
        if obj_type.returncode == 0 and obj_type.stdout.strip() == "tag":
            fail("remote tag did not provide dereferenced commit evidence")
    if not SHA_RE.fullmatch(commit):
        fail("remote tag commit evidence is invalid")
    return commit


def origin_main_contains(commit: str) -> bool:
    result = run_git("merge-base", "--is-ancestor", commit, "origin/main", check=False)
    return result.returncode == 0


def validate_descriptor(descriptor: dict, tag: str) -> str | None:
    version = descriptor.get("version")
    descriptor_tag = descriptor.get("tag") or descriptor.get("source_ref")
    if not isinstance(version, str) or not SAFE_TAG_RE.fullmatch(f"v{version}"):
        fail("release descriptor version is invalid")
    if descriptor_tag != f"v{version}" or descriptor.get("source_ref") != f"v{version}":
        fail("release descriptor tag/source_ref must match version")
    if tag != descriptor_tag:
        fail("requested tag must match current release descriptor tag/source_ref")
    info(f"descriptor version {version} matches {tag}")
    commit = descriptor.get("commit_sha")
    if commit is not None and not (isinstance(commit, str) and SHA_RE.fullmatch(commit)):
        fail("release descriptor commit_sha must be null or a full SHA")
    return commit


def validate_release_object(repo: str, tag: str, token: str, expected_commit: str) -> bool:
    api_request(repo, "", token)
    status, release = api_request(repo, f"/releases/tags/{tag}", token)
    if status == 404:
        return False
    if not release or release.get("tag_name") != tag:
        fail("GitHub Release object tag_name does not match expected tag")
    remote_commit = remote_tag_commit(tag)
    if remote_commit != expected_commit:
        fail("GitHub Release tag commit evidence does not match expected commit")
    info(f"existing GitHub Release validated for {tag}")
    return True


def create_release(repo: str, tag: str, token: str, descriptor: dict, expected_commit: str) -> None:
    title = descriptor.get("title") or f"KM VMS {tag}"
    summary = descriptor.get("summary") or f"KM VMS {tag}"
    body_lines = [str(summary), "", f"Commit evidence: {expected_commit}"]
    changelog = descriptor.get("changelog")
    if isinstance(changelog, list) and changelog:
        body_lines.extend(["", "Changes:"])
        body_lines.extend(f"- {item}" for item in changelog if isinstance(item, str))
    payload = {
        "tag_name": tag,
        "name": title,
        "body": "\n".join(body_lines),
        "draft": False,
        "prerelease": False,
    }
    api_request(repo, "/releases", token, method="POST", payload=payload)
    info(f"GitHub Release published for {tag}")


def main() -> None:
    descriptor = load_descriptor()
    repo = resolve_repo()
    descriptor_tag = descriptor.get("tag") or descriptor.get("source_ref")
    tag = tag_override or descriptor_tag
    if not isinstance(tag, str) or not SAFE_TAG_RE.fullmatch(tag):
        fail("tag must match vX.Y.Z")

    descriptor_commit = validate_descriptor(descriptor, tag)
    local_commit = tag_commit(tag)
    remote_commit = remote_tag_commit(tag)
    if local_commit != remote_commit:
        fail("local and remote tag commit evidence do not match")
    expected_commit = descriptor_commit or remote_commit
    if descriptor_commit and descriptor_commit != remote_commit:
        fail("descriptor commit_sha does not match tag commit evidence")
    if descriptor_commit is None:
        info("descriptor commit_sha is null; using dereferenced tag commit evidence")

    token = token_from_env()
    if mode == "publish":
        if not token:
            fail("GitHub release token is required for --publish")
        if not origin_main_contains(expected_commit):
            fail("tag commit is not reachable from origin/main")
        if not tag_override:
            head = git_out("rev-parse", "HEAD")
            if head != expected_commit:
                fail("current HEAD does not match release tag commit")
        exists = validate_release_object(repo, tag, token, expected_commit)
        if exists:
            info(f"GitHub Release already exists and is valid for {tag}")
        else:
            create_release(repo, tag, token, descriptor, expected_commit)
        print(f"PASS: publish validation complete for {repo} {tag} {expected_commit}")
        return

    if token:
        exists = validate_release_object(repo, tag, token, expected_commit)
        if exists:
            print(f"PASS: check complete for {repo} {tag} {expected_commit}")
        else:
            warn(f"GitHub Release is not published yet for {tag}")
            print(f"PASS: release is ready to publish for {repo} {tag} {expected_commit}")
    else:
        warn("authenticated GitHub Release validation skipped because token is not configured")
        print(f"PASS: local/public release checks complete for {repo} {tag} {expected_commit}")


main()
PY
