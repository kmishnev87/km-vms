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
  sh scripts/km-vms-release-cycle.sh --sync-local-release-identity [--app-dir <path>] [--apply]

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
SYNC_LOCAL_IDENTITY=0
SYNC_APPLY=0
APP_DIR_ARG=""

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
    --sync-local-release-identity|--closeout-local-identity) SYNC_LOCAL_IDENTITY=1; shift ;;
    --apply) SYNC_APPLY=1; shift ;;
    --app-dir)
      [ "$#" -ge 2 ] || { printf '%s\n' "--app-dir requires a value" >&2; exit 2; }
      APP_DIR_ARG="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$SYNC_LOCAL_IDENTITY" = "1" ]; then
  cmd="scripts/km-vms-adopt-release-identity.sh"
  set -- "$cmd"
  if [ -n "$APP_DIR_ARG" ]; then
    set -- "$@" --app-dir "$APP_DIR_ARG"
  fi
  if [ "$SYNC_APPLY" = "1" ]; then
    set -- "$@" --apply
  fi
  exec sh "$@"
fi

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
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SENSITIVE_RE = re.compile(
    r"(github_pat_|ghp_|Bearer\s+|rtsp://[^@\s]+@|"
    r"postgresql://[^:\s]+:[^@\s]+@|"
    r"-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
RELEASE_NOTE_LOCALES = {"en", "ru", "zh-CN"}
MAX_PATCH_VERSION = 29
CURRENT_PRODUCT_DB_SCHEMA_VERSION = 9
VERSION_FILES = [
    Path("apps/api/app/core/version.py"),
    Path("apps/web/package.json"),
    Path("apps/web/package-lock.json"),
    Path("release/km-vms-release.json"),
    Path("release/km-vms-update-lineage.json"),
]
LINEAGE_PATH = Path("release/km-vms-update-lineage.json")


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run_git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def parse_version(value: str) -> tuple[int, int, int]:
    base = value[1:] if value.startswith("v") else value
    main = re.split(r"[-+]", base, 1)[0]
    return tuple(int(part) for part in main.split("."))


def validate_patch_cap(version: str, context: str) -> None:
    major, minor, patch = parse_version(version)
    if patch > MAX_PATCH_VERSION:
        fail(
            f"{context} patch version must be 0..{MAX_PATCH_VERSION}; "
            f"after {major}.{minor}.{MAX_PATCH_VERSION} use {major}.{minor + 1}.0"
        )


def replace_app_version(text: str, version: str) -> str:
    return re.sub(r'APP_VERSION = "[^"]+"', f'APP_VERSION = "{version}"', text, count=1)


def load_json(path: Path) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    (ROOT / path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_release_note_text(value: object, field: str, max_length: int) -> str:
    if type(value) is not str:
        fail(f"release descriptor {field} must be plain text")
    text = value.strip()
    if (
        not text
        or len(text) > max_length
        or CONTROL_RE.search(text)
        or SENSITIVE_RE.search(text)
    ):
        fail(f"release descriptor {field} is invalid")
    return text


def validate_release_changelog(value: object, field: str) -> None:
    if type(value) is not list or len(value) > 20:
        fail(f"release descriptor {field} must be a bounded list")
    for index, item in enumerate(value):
        validate_release_note_text(item, f"{field}[{index}]", 180)


def validate_release_note_map(
    value: object,
    field: str,
    max_length: int,
) -> None:
    if type(value) is not dict or set(value) != RELEASE_NOTE_LOCALES:
        fail(
            f"release descriptor {field} must contain exactly "
            "en, ru and zh-CN"
        )
    for locale in ("en", "ru", "zh-CN"):
        validate_release_note_text(
            value[locale],
            f"{field}.{locale}",
            max_length,
        )


def validate_release_changelog_map(value: object) -> None:
    if type(value) is not dict or set(value) != RELEASE_NOTE_LOCALES:
        fail(
            "release descriptor changelog_i18n must contain exactly "
            "en, ru and zh-CN"
        )
    for locale in ("en", "ru", "zh-CN"):
        validate_release_changelog(
            value[locale],
            f"changelog_i18n.{locale}",
        )


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
    validate_patch_cap(version, "release descriptor")
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
    validate_release_note_text(descriptor.get("title"), "title", 160)
    validate_release_note_text(descriptor.get("summary"), "summary", 800)
    validate_release_changelog(descriptor.get("changelog"), "changelog")
    validate_release_note_map(
        descriptor.get("title_i18n"),
        "title_i18n",
        160,
    )
    validate_release_note_map(
        descriptor.get("summary_i18n"),
        "summary_i18n",
        800,
    )
    if "changelog_i18n" in descriptor:
        validate_release_changelog_map(descriptor["changelog_i18n"])
    for key in ("requires_backup", "requires_manual_action", "requires_migration"):
        if not isinstance(descriptor.get(key), bool):
            fail(f"release descriptor {key} must be boolean")
    return descriptor


def validate_update_lineage() -> dict:
    payload = load_json(LINEAGE_PATH)
    required = {
        "schema_version",
        "product",
        "tag_commits",
        "schema_versions",
        "shape_fingerprints",
        "shape_alternates",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or payload.get("product") != "KM VMS"
    ):
        fail("update lineage contract is invalid")
    tag_commits = payload.get("tag_commits")
    schema_versions = payload.get("schema_versions")
    shapes = payload.get("shape_fingerprints")
    alternates = payload.get("shape_alternates")
    if (
        not isinstance(tag_commits, dict)
        or not tag_commits
        or len(tag_commits) > 256
        or not isinstance(schema_versions, dict)
        or not isinstance(shapes, dict)
        or not isinstance(alternates, dict)
        or set(tag_commits) != set(schema_versions)
        or set(tag_commits) != set(shapes)
        or not set(alternates).issubset(tag_commits)
    ):
        fail("update lineage maps are inconsistent")
    ordered = list(tag_commits)
    if ordered != sorted(ordered, key=parse_version):
        fail("update lineage versions must be unique and ordered")
    for version in ordered:
        commit = tag_commits.get(version)
        schema_version = schema_versions.get(version)
        shape = shapes.get(version)
        variants = alternates.get(version, [])
        if (
            not SEMVER_RE.fullmatch(version)
            or not isinstance(commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or type(schema_version) is not int
            or not 1 <= schema_version <= CURRENT_PRODUCT_DB_SCHEMA_VERSION
            or not isinstance(shape, str)
            or not re.fullmatch(r"[0-9a-f]{64}", shape)
            or not isinstance(variants, list)
            or len(variants) > 4
            or len(variants) != len(set(variants))
            or shape in variants
            or any(
                not isinstance(item, str)
                or not re.fullmatch(r"[0-9a-f]{64}", item)
                for item in variants
            )
        ):
            fail(f"update lineage evidence is invalid for {version}")
    return payload


def tagged_commit(version: str) -> str:
    tag = f"v{version}"
    try:
        return run_git(
            "rev-parse",
            "--verify",
            f"refs/tags/{tag}^{{commit}}",
        ).lower()
    except subprocess.CalledProcessError:
        fail(f"immediate previous public release tag is missing: {tag}")


def register_current_release(
    version: str,
    descriptor: dict,
    *,
    apply: bool,
) -> dict:
    """Append one schema-equivalent release without four manual edits."""

    lineage = validate_update_lineage()
    commits = lineage["tag_commits"]
    tagged = tagged_commit(version)
    if version in commits:
        if commits[version] != tagged:
            fail(
                "immediate previous public release tag commit does not "
                "match the canonical update lineage"
            )
        return lineage
    previous = list(commits)[-1]
    if parse_version(previous) >= parse_version(version):
        fail(
            "update lineage ends at a release that is not older than "
            f"the current release {version}"
        )
    if descriptor.get("requires_migration") is not False:
        fail(
            "a schema-changing current release needs explicit lineage "
            "metadata and cannot inherit the previous schema family"
        )
    lineage["tag_commits"][version] = tagged
    lineage["schema_versions"][version] = lineage[
        "schema_versions"
    ][previous]
    lineage["shape_fingerprints"][version] = lineage[
        "shape_fingerprints"
    ][previous]
    previous_alternates = lineage["shape_alternates"].get(previous)
    if previous_alternates:
        lineage["shape_alternates"][version] = list(
            previous_alternates
        )
    if apply:
        write_json(LINEAGE_PATH, lineage)
        print(
            "Registered current schema-equivalent release "
            f"{version} in update lineage"
        )
    else:
        print(
            "DRY-RUN: would register current schema-equivalent release "
            f"{version} in update lineage"
        )
    return lineage


def validate_immediate_previous_release(
    version: str,
    lineage: dict,
) -> None:
    commits = lineage["tag_commits"]
    if list(commits)[-1] != version:
        fail(
            "update lineage must end at the immediate previous public "
            f"release {version}"
        )
    if tagged_commit(version) != commits[version]:
        fail(
            "immediate previous public release tag commit does not match "
            "the canonical update lineage"
        )


def check_versions() -> None:
    values = versions()
    unique = {value for value in values.values() if value}
    if len(unique) != 1:
        fail(f"version mismatch: {values}")
    version = unique.pop()
    if not SEMVER_RE.fullmatch(version):
        fail(f"version is not semantic: {version}")
    validate_patch_cap(version, "--check")


def check_dirty() -> None:
    if allow_dirty:
        return
    dirty = run_git("status", "--porcelain")
    if dirty:
        fail("working tree is dirty; rerun with --allow-dirty for local validation only")


def check_permission_policy() -> None:
    gate = ROOT / "scripts/km-vms-permission-gate.sh"
    if not gate.is_file():
        fail("scripts/km-vms-permission-gate.sh is missing")
    result = subprocess.run(
        ["sh", str(gate), "--check", "--app-dir", str(ROOT)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        fail(f"product-source permission gate failed: {detail or 'unknown error'}")
    if result.stdout.strip():
        print(result.stdout.strip())


def prepare(version: str) -> None:
    if not SEMVER_RE.fullmatch(version):
        fail("--prepare-version must be semantic x.y.z")
    validate_patch_cap(version, "--prepare-version")
    current = versions()["release_descriptor"]
    if parse_version(version) <= parse_version(current):
        fail(f"target version {version} must be greater than current {current}")
    descriptor = validate_descriptor()
    lineage = register_current_release(
        current,
        descriptor,
        apply=not dry_run,
    )
    validate_immediate_previous_release(current, lineage)
    if dry_run:
        print(f"DRY-RUN: would prepare release version {version}")
        print(
            "DRY-RUN: would invalidate title, summary, changelog and "
            "localized release notes for the new version"
        )
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
    descriptor["title"] = ""
    descriptor["summary"] = ""
    descriptor["changelog"] = []
    descriptor.pop("title_i18n", None)
    descriptor.pop("summary_i18n", None)
    descriptor.pop("changelog_i18n", None)
    write_json(descriptor_path, descriptor)
    print(
        f"Prepared KM VMS release version {version}; release notes were "
        "invalidated and must be written before --check"
    )


def print_release_commands(version: str) -> None:
    if not version:
        version = versions()["release_descriptor"]
    if not SEMVER_RE.fullmatch(version):
        fail("--version must be semantic x.y.z")
    validate_patch_cap(version, "--print-github-release-commands")
    tag = f"v{version}"
    print("Release publication commands preview; run only after operator acceptance:")
    print("git status --short --branch")
    print("git diff --check")
    print(f"git tag -a {tag} -m \"KM VMS {tag}\"")
    print("git push origin main")
    print(f"git push origin {tag}")
    print(f"sh scripts/km-vms-publish-github-release.sh --check --tag {tag}")
    print("KM_VMS_GITHUB_RELEASE_TOKEN_FILE=data/update-control/.github-release-token \\")
    print(f"  sh scripts/km-vms-publish-github-release.sh --publish --tag {tag}")
    print("# release/km-vms-release.json commit_sha may remain null; trusted commit evidence is the validated semver tag commit.")
    print(f"sh scripts/km-vms-release-cycle.sh --sync-local-release-identity --apply")
    print("curl -fsS http://127.0.0.1:${HTTP_PORT:-8088}/api/health")
    print("curl -fsS http://127.0.0.1:${HTTP_PORT:-8088}/api/system/update/status")
    print("# Verify installed_release.version/title/commit_sha match release/km-vms-release.json and the release tag commit.")
    print("# If update status requires authentication, validate it through an authenticated operator session or Settings -> Maintenance.")


if prepare_version:
    check_dirty()
    check_permission_policy()
    prepare(prepare_version)

if check:
    check_dirty()
    check_versions()
    validate_descriptor()
    validate_update_lineage()
    check_permission_policy()
    print("release-cycle check PASS")

if print_commands:
    print_release_commands(version_arg)

if not any([check, prepare_version, print_commands]):
    fail("no action selected; use --help")
PY
