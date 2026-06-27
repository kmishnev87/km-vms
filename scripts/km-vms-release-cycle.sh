#!/usr/bin/env sh
set -eu

usage() {
  cat <<'EOF'
KM VMS release-cycle helper

Usage:
  sh scripts/km-vms-release-cycle.sh --check [--allow-dirty]
  sh scripts/km-vms-release-cycle.sh --dry-run --prepare-version <x.y.z> [--allow-dirty]
  sh scripts/km-vms-release-cycle.sh --prepare-version <x.y.z> [--allow-dirty]
  sh scripts/km-vms-release-cycle.sh --print-github-release-commands [--version <x.y.z>]

This helper prepares and validates release metadata only. It never commits,
pushes, creates tags or publishes GitHub Releases.
EOF
}

CHECK=0
DRY_RUN=0
PREPARE_VERSION=""
PRINT_COMMANDS=0
ALLOW_DIRTY=0
VERSION_ARG=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --prepare-version)
      [ "$#" -ge 2 ] || { printf '%s\n' "--prepare-version requires a value" >&2; exit 2; }
      PREPARE_VERSION="$2"; shift 2 ;;
    --print-github-release-commands) PRINT_COMMANDS=1; shift ;;
    --version)
      [ "$#" -ge 2 ] || { printf '%s\n' "--version requires a value" >&2; exit 2; }
      VERSION_ARG="$2"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

python3 - "$CHECK" "$DRY_RUN" "$PREPARE_VERSION" "$PRINT_COMMANDS" "$ALLOW_DIRTY" "$VERSION_ARG" <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

check = sys.argv[1] == "1"
dry_run = sys.argv[2] == "1"
prepare_version = sys.argv[3]
print_commands = sys.argv[4] == "1"
allow_dirty = sys.argv[5] == "1"
version_arg = sys.argv[6]

ROOT = Path.cwd()
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SAFE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_FILES = [
    Path("apps/api/app/core/version.py"),
    Path("apps/web/package.json"),
    Path("apps/web/package-lock.json"),
    Path("release/km-vms-release.json"),
]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run_git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def parse_version(value: str) -> tuple[int, int, int]:
    base = value[1:] if value.startswith("v") else value
    main = re.split(r"[-+]", base, 1)[0]
    return tuple(int(part) for part in main.split("."))


def replace_app_version(text: str, version: str) -> str:
    return re.sub(r'APP_VERSION = "[^"]+"', f'APP_VERSION = "{version}"', text, count=1)


def load_json(path: Path) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    (ROOT / path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def versions() -> dict[str, str]:
    api_text = (ROOT / "apps/api/app/core/version.py").read_text(encoding="utf-8")
    api_match = re.search(r'APP_VERSION = "([^"]+)"', api_text)
    package = load_json(Path("apps/web/package.json"))
    lock = load_json(Path("apps/web/package-lock.json"))
    descriptor = load_json(Path("release/km-vms-release.json"))
    return {
        "api": api_match.group(1) if api_match else "",
        "web_package": package.get("version", ""),
        "web_lock": lock.get("version", ""),
        "web_lock_root": lock.get("packages", {}).get("", {}).get("version", ""),
        "release_descriptor": descriptor.get("version", ""),
    }


def validate_descriptor() -> dict:
    descriptor = load_json(Path("release/km-vms-release.json"))
    if descriptor.get("schema_version") != 1:
        fail("release descriptor schema_version must be 1")
    version = descriptor.get("version")
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        fail("release descriptor version must be semantic")
    expected_tag = f"v{version}"
    tag = descriptor.get("tag") or descriptor.get("source_ref")
    if tag != expected_tag or not SAFE_TAG_RE.fullmatch(tag):
        fail("release descriptor tag/source_ref must match v<version>")
    if descriptor.get("source_ref") != expected_tag:
        fail("release descriptor source_ref must be the semantic tag")
    if not SAFE_REPO_RE.fullmatch(str(descriptor.get("source_repo", ""))):
        fail("release descriptor source_repo is unsafe")
    if descriptor.get("evidence_model") != "semver_tag_resolves_to_commit":
        fail("release descriptor evidence_model must be semver_tag_resolves_to_commit")
    commit = descriptor.get("commit_sha")
    if commit is not None and not SHA_RE.fullmatch(str(commit)):
        fail("release descriptor commit_sha must be null or a full SHA")
    changelog = descriptor.get("changelog")
    if not isinstance(changelog, list) or len(changelog) > 20:
        fail("release descriptor changelog must be a bounded list")
    for item in changelog:
        if not isinstance(item, str) or len(item) > 180:
            fail("release descriptor changelog entries must be short text")
    for key in ("requires_backup", "requires_manual_action", "requires_migration"):
        if not isinstance(descriptor.get(key), bool):
            fail(f"release descriptor {key} must be boolean")
    return descriptor


def check_versions() -> None:
    values = versions()
    unique = {value for value in values.values() if value}
    if len(unique) != 1:
        fail(f"version mismatch: {values}")
    version = unique.pop()
    if not SEMVER_RE.fullmatch(version):
        fail(f"version is not semantic: {version}")


def check_dirty() -> None:
    if allow_dirty:
        return
    dirty = run_git("status", "--porcelain")
    if dirty:
        fail("working tree is dirty; rerun with --allow-dirty for local validation only")


def prepare(version: str) -> None:
    if not SEMVER_RE.fullmatch(version):
        fail("--prepare-version must be semantic x.y.z")
    current = versions()["release_descriptor"]
    if parse_version(version) <= parse_version(current):
        fail(f"target version {version} must be greater than current {current}")
    if dry_run:
        print(f"DRY-RUN: would prepare release version {version}")
        return
    api_path = ROOT / "apps/api/app/core/version.py"
    api_path.write_text(replace_app_version(api_path.read_text(encoding="utf-8"), version), encoding="utf-8")
    package_path = Path("apps/web/package.json")
    package = load_json(package_path)
    package["version"] = version
    write_json(package_path, package)
    lock_path = Path("apps/web/package-lock.json")
    lock = load_json(lock_path)
    lock["version"] = version
    lock.setdefault("packages", {}).setdefault("", {})["version"] = version
    write_json(lock_path, lock)
    descriptor_path = Path("release/km-vms-release.json")
    descriptor = load_json(descriptor_path)
    descriptor["version"] = version
    descriptor["tag"] = f"v{version}"
    descriptor["source_ref"] = f"v{version}"
    descriptor["commit_sha"] = None
    descriptor["published_at"] = None
    write_json(descriptor_path, descriptor)
    print(f"Prepared KM VMS release version {version}")


def print_release_commands(version: str) -> None:
    if not version:
        version = versions()["release_descriptor"]
    if not SEMVER_RE.fullmatch(version):
        fail("--version must be semantic x.y.z")
    tag = f"v{version}"
    print("Release publication commands preview; run only after operator acceptance:")
    print("git status --short --branch")
    print("git diff --check")
    print(f"git tag -a {tag} -m \"KM VMS {tag}\"")
    print("git push origin main")
    print(f"git push origin {tag}")
    print(f"gh release create {tag} --title \"KM VMS {tag}\" --notes-file <release-notes.md>")


if prepare_version:
    check_dirty()
    prepare(prepare_version)

if check:
    check_dirty()
    check_versions()
    validate_descriptor()
    print("release-cycle check PASS")

if print_commands:
    print_release_commands(version_arg)

if not any([check, prepare_version, print_commands]):
    fail("no action selected; use --help")
PY
