from __future__ import annotations

import copy
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

from app.services import update_apply


ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts/km-vms-permission-gate.sh"
SHELL = Path(shutil.which("sh") or "/bin/sh")

EXECUTABLES = (
    "scripts/install.sh",
    "scripts/km-vms-adopt-release-identity.sh",
    "scripts/km-vms-compose-common.sh",
    "scripts/km-vms-permission-gate.sh",
    "scripts/km-vms-publish-github-release.sh",
    "scripts/km-vms-release-cycle.sh",
    "scripts/km-vms-restart.sh",
    "scripts/km-vms-setup-activation-helper.sh",
    "scripts/km-vms-storage-apply.sh",
    "scripts/km-vms-storage-discovery.sh",
    "scripts/run_backend_tests.sh",
    "scripts/update.sh",
)

TOP_FILES = (
    ".dockerignore",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "docker-compose.pytest.yml",
    "docker-compose.yml",
)


def load_stage125():
    path = Path(__file__).with_name("test_update_apply_admission_stage660125.py")
    spec = importlib.util.spec_from_file_location(f"stage660127_base_{uuid.uuid4().hex}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def base():
    return load_stage125()


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def product_mode_snapshot(app: Path) -> dict[str, int]:
    result = {".": mode(app)}
    for relative in ("apps", "deploy", "docs", "release", "scripts"):
        root = app / relative
        for path in (root, *root.rglob("*")):
            if path.is_file() or path.is_dir():
                result[path.relative_to(app).as_posix()] = mode(path)
    for relative in TOP_FILES:
        path = app / relative
        if path.is_file():
            result[relative] = mode(path)
    return result


def write(path: Path, text: str = "fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def permission_tree(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    for relative in (
        "apps/api/app/services",
        "apps/update-helper",
        "apps/web",
        "deploy/nginx",
        "docs",
        "release",
        "scripts",
        "data/private",
        ".git",
    ):
        (app / relative).mkdir(parents=True, exist_ok=True)

    for relative in TOP_FILES:
        write(app / relative)
    write(app / "apps/update-helper/Dockerfile", "FROM docker:27-cli\n")
    write(app / "apps/api/app/services/runtime.py")
    write(app / "deploy/nginx/default.conf")
    write(app / "docs/INSTALL.md")
    write(app / "release/km-vms-release.json", "{}\n")
    write(app / "scripts/km-vms-update-helper.py")
    write(app / "scripts/km-vms-update-helper-bridge.py", "# fixture\n")
    write(app / "scripts/km-vms-storage-candidate-validate.sh")
    write(app / "scripts/km-vms-storage-root-cleanup.sh")
    write(app / "scripts/README-backend-pytest.md")
    for relative in EXECUTABLES:
        write(app / relative, "#!/usr/bin/env sh\n")

    write(app / ".env", "SECRET=fixture\n")
    write(app / "data/private/runtime.control")
    write(app / ".git/local-state")

    for path in app.rglob("*"):
        if path.is_dir():
            os.chmod(path, 0o777)
        elif path.is_file():
            os.chmod(path, 0o666)
    for relative in EXECUTABLES:
        os.chmod(app / relative, 0o777)
    os.chmod(app / ".env", 0o600)
    os.chmod(app / "data/private/runtime.control", 0o660)
    os.chmod(app / ".git/local-state", 0o666)
    os.chmod(app, 0o777)
    return app


def write_executable(path: Path, text: str) -> None:
    write(path, text)
    os.chmod(path, 0o755)


def safe_acl_tool_dir(base: Path) -> Path:
    tool_dir = base / ".km-vms-safe-acl-tools"
    tool_dir.mkdir(parents=True, exist_ok=True)
    write_executable(
        tool_dir / "getfacl",
        "#!/bin/sh\n"
        "printf 'user::rwx\ngroup::r-x\nother::r-x\n'\n",
    )
    return tool_dir


def selective_acl_tool_dir(base: Path) -> Path:
    tool_dir = base / ".km-vms-selective-acl-tools"
    tool_dir.mkdir(parents=True, exist_ok=True)
    write_executable(
        tool_dir / "getfacl",
        """#!/bin/sh
path=''
for arg in "$@"; do path=$arg; done
printf 'user::rwx\ngroup::r-x\nother::r-x\n'
if [ "$path" = "${KMVMS_TEST_ACL_PATH:-}" ]; then
  printf '%s\n' "${KMVMS_TEST_ACL_LINE:-}"
fi
""",
    )
    return tool_dir


def run_gate(
    app: Path,
    action: str = "--check",
    *,
    tool_dirs: tuple[Path, ...] = (),
    use_safe_getfacl: bool = True,
    replace_path: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    path_parts = [str(path) for path in tool_dirs]
    if use_safe_getfacl:
        path_parts.append(str(safe_acl_tool_dir(app.parent)))
    if not replace_path:
        path_parts.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join(part for part in path_parts if part)
    return subprocess.run(
        [str(SHELL), str(GATE), action, "--app-dir", str(app)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def assert_fail_closed(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0
    assert "permission_gate=PASS" not in result.stdout
    assert "acl_check=PASS" not in result.stdout


CANONICAL_TIMESTAMP_CASES = (
    ("2026-07-21T00:00:10Z", True),
    ("2026-07-21T00:00:10.1Z", True),
    ("2026-07-21T00:00:10.123456Z", True),
    ("2026-07-21", False),
    ("2026-W30-2", False),
    ("20260721T000010", False),
    ("2026-07-21T00:00:10", False),
    ("2026-07-21T00:00:10+00:00", False),
    ("2026-07-21T00:00:10z", False),
    ("2026-07-21 00:00:10Z", False),
    ("2026-07-21T00:00:10.1234567Z", False),
    ("2026-02-30T00:00:10Z", False),
)


@pytest.mark.parametrize(
    "value,accepted",
    CANONICAL_TIMESTAMP_CASES,
    ids=(
        "canonical-seconds",
        "canonical-tenth",
        "canonical-microseconds",
        "date-only",
        "week-date",
        "compact",
        "timezone-less",
        "numeric-utc-offset",
        "lowercase-z",
        "space-separator",
        "fraction-over-six",
        "invalid-calendar-date",
    ),
)
def test_authority_timestamp_is_canonical_utc_in_api_and_helper(base, value, accepted):
    helper = base.load_helper()
    assert update_apply._strict_timestamp(value) is accepted
    assert helper.valid_timestamp(value) is accepted


@pytest.mark.parametrize(
    "case_name",
    (
        "request-and-entry-requested-at",
        "document-updated-at",
        "entry-updated-at",
        "audit-confirmed-at",
        "claimed-at",
        "terminal-started-at",
        "terminal-updated-at",
        "terminal-finished-at",
    ),
)
@pytest.mark.parametrize(
    "invalid_timestamp",
    (
        "2026-07-21",
        "2026-W30-2",
        "20260721T000010",
        "2026-07-21T00:00:10",
    ),
    ids=("date-only", "week-date", "compact", "timezone-less"),
)
def test_every_authority_surface_rejects_noncanonical_timestamp(
    base,
    case_name,
    invalid_timestamp,
):
    helper = base.load_helper()
    request = base.request_payload()
    if case_name == "claimed-at":
        payload = base.admission_document(base.entry_payload("claimed", request=request))
    elif case_name.startswith("terminal-"):
        terminal = base.terminal_payload(request, "failed")
        payload = base.admission_document(
            base.entry_payload("terminal", request=request, terminal=terminal)
        )
    else:
        payload = base.admission_document(
            base.entry_payload("admitted_unclaimed", request=request)
        )

    entry = payload["entries"][0]
    if case_name == "request-and-entry-requested-at":
        entry["requested_at"] = invalid_timestamp
        entry["request"]["requested_at"] = invalid_timestamp
    elif case_name == "document-updated-at":
        payload["updated_at"] = invalid_timestamp
    elif case_name == "entry-updated-at":
        entry["updated_at"] = invalid_timestamp
    elif case_name == "audit-confirmed-at":
        entry["audit"]["confirmed_at"] = invalid_timestamp
    elif case_name == "claimed-at":
        entry["claimed_at"] = invalid_timestamp
    elif case_name == "terminal-started-at":
        entry["terminal"]["started_at"] = invalid_timestamp
    elif case_name == "terminal-updated-at":
        entry["terminal"]["updated_at"] = invalid_timestamp
    elif case_name == "terminal-finished-at":
        entry["terminal"]["finished_at"] = invalid_timestamp
    else:
        raise AssertionError(case_name)

    assert base.api_accepts(copy.deepcopy(payload)) is False
    assert base.helper_accepts(helper, copy.deepcopy(payload)) is False


def test_permission_fix_is_bounded_and_idempotent(tmp_path):
    app = permission_tree(tmp_path)
    env_before = (app / ".env").read_bytes()
    runtime_before = (app / "data/private/runtime.control").read_bytes()
    git_before = (app / ".git/local-state").read_bytes()

    fixed = run_gate(app, "--fix")
    assert fixed.returncode == 0, fixed.stderr
    assert "permission_gate=PASS" in fixed.stdout
    assert "permission_action=fix" in fixed.stdout

    assert mode(app) == 0o755
    for relative in ("apps", "apps/api/app/services", "deploy", "docs", "release", "scripts"):
        assert mode(app / relative) == 0o755
    for relative in EXECUTABLES:
        assert mode(app / relative) == 0o755
    for relative in (
        *TOP_FILES,
        "apps/update-helper/Dockerfile",
        "apps/api/app/services/runtime.py",
        "scripts/km-vms-update-helper.py",
        "scripts/km-vms-storage-candidate-validate.sh",
    ):
        assert mode(app / relative) == 0o644

    assert mode(app / ".env") == 0o600
    assert mode(app / "data/private") == 0o777
    assert mode(app / "data/private/runtime.control") == 0o660
    assert mode(app / ".git/local-state") == 0o666
    assert (app / ".env").read_bytes() == env_before
    assert (app / "data/private/runtime.control").read_bytes() == runtime_before
    assert (app / ".git/local-state").read_bytes() == git_before

    before = {
        path.relative_to(app).as_posix(): (mode(path), path.read_bytes())
        for path in app.rglob("*")
        if path.is_file()
    }
    checked = run_gate(app)
    assert checked.returncode == 0, checked.stderr
    after = {
        path.relative_to(app).as_posix(): (mode(path), path.read_bytes())
        for path in app.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_permission_fix_repairs_legacy_shared_write_with_real_getfacl(tmp_path):
    if not shutil.which("getfacl"):
        pytest.skip("POSIX ACL inspection is unavailable in the isolated test container")
    app = permission_tree(tmp_path)
    target = app / "docker-compose.yml"
    os.chmod(target, 0o666)

    fixed = run_gate(app, "--fix", use_safe_getfacl=False)

    assert fixed.returncode == 0, fixed.stderr
    assert mode(target) == 0o644
    assert "permission_gate=PASS" in fixed.stdout
    checked = run_gate(app, "--check", use_safe_getfacl=False)
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize(
    "relative,initial_mode",
    (
        (".", 0o2775),
        (".", 0o4755),
        (".", 0o6755),
        (".", 0o2770),
        ("apps/api", 0o2775),
        ("apps/api", 0o4755),
        ("apps/api", 0o6755),
        ("apps/api", 0o2770),
    ),
    ids=(
        "root-setgid-shared-write",
        "root-setuid",
        "root-setuid-setgid",
        "root-setgid-no-other-access",
        "nested-setgid-shared-write",
        "nested-setuid",
        "nested-setuid-setgid",
        "nested-setgid-no-other-access",
    ),
)
def test_permission_fix_clears_directory_special_bits_when_numeric_chmod_preserves_them(
    tmp_path, relative, initial_mode
):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    target = app if relative == "." else app / relative
    os.chmod(target, initial_mode)
    if mode(target) != initial_mode:
        pytest.skip("fixture filesystem does not retain directory special mode bits")

    real_chmod = shutil.which("chmod")
    real_stat = shutil.which("stat")
    assert real_chmod and real_stat
    tool_dir = tmp_path / "gnu-directory-chmod-semantics"
    tool_dir.mkdir()
    write_executable(
        tool_dir / "chmod",
        f"""#!/bin/sh
set -eu
mode_arg=$1
path_arg=$2
before=$({shlex.quote(real_stat)} -c '%a' "$path_arg")
{shlex.quote(real_chmod)} "$@"
case "$mode_arg:$before" in
  755:[1-7][0-7][0-7][0-7])
    special=${{before%???}}
    {shlex.quote(real_chmod)} "${{special}}${{mode_arg}}" "$path_arg"
    ;;
esac
""",
    )

    fixed = run_gate(app, "--fix", tool_dirs=(tool_dir,))

    assert fixed.returncode == 0, fixed.stderr
    assert mode(target) == 0o755
    checked = run_gate(app, "--check")
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize(
    "acl_line",
    (
        "user:www-data:r--",
        "group:power-users:r-x",
    ),
    ids=("safe-user-name-containing-w", "safe-group-name-containing-w"),
)
def test_permission_gate_accepts_read_only_acl_when_subject_name_contains_w(
    tmp_path, acl_line
):
    app = permission_tree(tmp_path)
    target = app / "scripts/km-vms-update-helper.py"
    tool_dir = selective_acl_tool_dir(tmp_path)

    fixed = run_gate(
        app,
        "--fix",
        tool_dirs=(tool_dir,),
        use_safe_getfacl=False,
        extra_env={
            "KMVMS_TEST_ACL_PATH": str(target),
            "KMVMS_TEST_ACL_LINE": acl_line,
        },
    )

    assert fixed.returncode == 0, fixed.stderr
    assert "permission_gate=PASS" in fixed.stdout


@pytest.mark.parametrize(
    "relative,acl_line",
    (
        ("scripts/km-vms-update-helper.py", "user:www-data:rw-"),
        ("scripts", "default:group:users:rwx"),
    ),
    ids=("writable-user-name-containing-w", "writable-default-group"),
)
def test_permission_fix_rejects_write_in_acl_permissions_field_before_mutation(
    tmp_path, relative, acl_line
):
    app = permission_tree(tmp_path)
    target = app / relative
    tool_dir = selective_acl_tool_dir(tmp_path)
    modes_before = product_mode_snapshot(app)

    rejected = run_gate(
        app,
        "--fix",
        tool_dirs=(tool_dir,),
        use_safe_getfacl=False,
        extra_env={
            "KMVMS_TEST_ACL_PATH": str(target),
            "KMVMS_TEST_ACL_LINE": acl_line,
        },
    )

    assert_fail_closed(rejected)
    assert "acl grants non-owner write" in rejected.stderr.lower()
    assert product_mode_snapshot(app) == modes_before


@pytest.mark.parametrize(
    "relative,bad_mode",
    (
        ("scripts/km-vms-update-helper.py", 0o666),
        ("docker-compose.yml", 0o666),
        ("scripts/km-vms-release-cycle.sh", 0o777),
        ("apps/api/app/services/runtime.py", 0o664),
        ("apps/api/app/services", 0o775),
    ),
    ids=(
        "world-writable-helper",
        "world-writable-compose",
        "world-writable-release-script",
        "group-writable-product-file",
        "group-writable-product-directory",
    ),
)
def test_permission_check_rejects_shared_write(tmp_path, relative, bad_mode):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    os.chmod(app / relative, bad_mode)
    rejected = run_gate(app)
    assert rejected.returncode != 0
    assert "writable" in rejected.stderr.lower() or "mode must be" in rejected.stderr.lower()


def test_permission_check_rejects_missing_privileged_file(tmp_path):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    (app / "scripts/km-vms-update-helper.py").unlink()
    rejected = run_gate(app)
    assert rejected.returncode != 0
    assert "required privileged-chain file is missing" in rejected.stderr.lower()


def test_permission_check_rejects_product_symlink(tmp_path):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    link = app / "apps/api/app/services/linked.py"
    try:
        link.symlink_to(app / "apps/api/app/services/runtime.py")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    rejected = run_gate(app)
    assert rejected.returncode != 0
    assert "symlink is forbidden" in rejected.stderr.lower()


def test_permission_check_rejects_owner_group_drift_when_supported(tmp_path):
    if getattr(os, "geteuid", lambda: -1)() != 0:
        pytest.skip("owner/group drift test requires root in the isolated test container")
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    target = app / "scripts/km-vms-update-helper.py"
    try:
        os.chown(target, 65534, 65534)
    except OSError as exc:
        pytest.skip(f"chown is unavailable: {exc}")
    rejected = run_gate(app)
    assert rejected.returncode != 0
    assert "owner/group differs" in rejected.stderr.lower()


def test_permission_check_rejects_named_write_acl_when_supported(tmp_path):
    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        pytest.skip("POSIX ACL tools are unavailable in the isolated test container")
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    target = app / "scripts/km-vms-update-helper.py"
    acl = subprocess.run(
        ["setfacl", "-m", "u:65534:rw-,m::r--", str(target)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if acl.returncode != 0:
        pytest.skip(f"filesystem ACL mutation is unavailable: {acl.stderr}")
    rejected = run_gate(app, use_safe_getfacl=False)
    assert rejected.returncode != 0
    assert "acl grants non-owner write" in rejected.stderr.lower()


@pytest.mark.parametrize(
    "relative,acl_spec",
    (
        ("scripts/km-vms-update-helper.py", "u:65534:rw-"),
        ("scripts", "d:g:65534:rwx"),
    ),
    ids=("named-write-acl", "default-write-acl"),
)
def test_permission_fix_rejects_extended_write_acl_before_mode_mutation(
    tmp_path, relative, acl_spec
):
    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        pytest.skip("POSIX ACL tools are unavailable in the isolated test container")
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix", use_safe_getfacl=False).returncode == 0
    target = app / relative
    acl = subprocess.run(
        ["setfacl", "-m", acl_spec, str(target)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if acl.returncode != 0:
        pytest.skip(f"filesystem ACL mutation is unavailable: {acl.stderr}")
    modes_before = product_mode_snapshot(app)

    rejected = run_gate(app, "--fix", use_safe_getfacl=False)

    assert_fail_closed(rejected)
    assert "acl grants non-owner write" in rejected.stderr.lower()
    assert product_mode_snapshot(app) == modes_before


@pytest.mark.parametrize("action", ("--check", "--fix"))
def test_permission_gate_fails_when_getfacl_is_unavailable(tmp_path, action):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    target = app / "scripts/km-vms-update-helper.py"
    os.chmod(target, 0o666)
    mode_before = mode(target)
    tool_dir = tmp_path / "minimal-tools"
    tool_dir.mkdir()
    for name in ("find", "stat", "mktemp", "chmod", "rm"):
        executable = shutil.which(name)
        assert executable, name
        (tool_dir / name).symlink_to(executable)

    rejected = run_gate(
        app,
        action,
        tool_dirs=(tool_dir,),
        use_safe_getfacl=False,
        replace_path=True,
    )
    assert_fail_closed(rejected)
    assert "getfacl is required" in rejected.stderr.lower()
    assert mode(target) == mode_before


@pytest.mark.parametrize("failure_position", ("first", "middle"))
def test_permission_gate_fails_closed_when_getfacl_errors(tmp_path, failure_position):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    tool_dir = tmp_path / f"getfacl-failure-{failure_position}"
    tool_dir.mkdir()
    write_executable(
        tool_dir / "getfacl",
        """#!/bin/sh
path=''
for arg in "$@"; do path=$arg; done
if [ "$path" = "${KMVMS_TEST_GETFACL_FAIL_ON:-}" ]; then
  printf 'simulated getfacl failure\n' >&2
  exit 73
fi
printf 'user::rwx\ngroup::r-x\nother::r-x\n'
""",
    )
    fail_on = app if failure_position == "first" else app / "scripts/km-vms-update-helper.py"
    rejected = run_gate(
        app,
        tool_dirs=(tool_dir,),
        use_safe_getfacl=False,
        extra_env={"KMVMS_TEST_GETFACL_FAIL_ON": str(fail_on)},
    )
    assert_fail_closed(rejected)
    assert "cannot read acl" in rejected.stderr.lower()


def test_permission_gate_fix_does_not_mutate_modes_when_acl_inspection_errors(tmp_path):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    target = app / "scripts/km-vms-update-helper.py"
    os.chmod(target, 0o666)
    mode_before = mode(target)
    tool_dir = tmp_path / "getfacl-failure-before-fix"
    tool_dir.mkdir()
    write_executable(
        tool_dir / "getfacl",
        "#!/bin/sh\n"
        "printf 'simulated getfacl failure\\n' >&2\n"
        "exit 73\n",
    )

    rejected = run_gate(
        app,
        "--fix",
        tool_dirs=(tool_dir,),
        use_safe_getfacl=False,
    )
    assert_fail_closed(rejected)
    assert "cannot read acl" in rejected.stderr.lower()
    assert mode(target) == mode_before


def test_permission_gate_fails_closed_when_find_errors(tmp_path):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    os.chmod(app / "docker-compose.yml", 0o666)
    modes_before = product_mode_snapshot(app)
    real_find = shutil.which("find")
    assert real_find
    tool_dir = tmp_path / "find-failure"
    tool_dir.mkdir()
    find_script = f"""#!/bin/sh
if [ "${{1:-}}" = "${{KMVMS_TEST_FIND_FAIL_ON:-}}" ]; then
  printf 'simulated find failure\n' >&2
  exit 42
fi
exec {real_find} "$@"
"""
    write_executable(tool_dir / "find", find_script)
    rejected = run_gate(
        app,
        "--fix",
        tool_dirs=(tool_dir,),
        extra_env={"KMVMS_TEST_FIND_FAIL_ON": str(app / "deploy")},
    )
    assert_fail_closed(rejected)
    assert "cannot enumerate product tree" in rejected.stderr.lower()
    assert product_mode_snapshot(app) == modes_before


def test_permission_gate_fails_closed_when_stat_errors(tmp_path):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    os.chmod(app / "docker-compose.yml", 0o666)
    modes_before = product_mode_snapshot(app)
    real_stat = shutil.which("stat")
    assert real_stat
    tool_dir = tmp_path / "stat-failure"
    tool_dir.mkdir()
    stat_script = f"""#!/bin/sh
path=''
for arg in "$@"; do path=$arg; done
if [ "$path" = "${{KMVMS_TEST_STAT_FAIL_ON:-}}" ]; then
  printf 'simulated stat failure\n' >&2
  exit 44
fi
exec {real_stat} "$@"
"""
    write_executable(tool_dir / "stat", stat_script)
    rejected = run_gate(
        app,
        "--fix",
        tool_dirs=(tool_dir,),
        extra_env={
            "KMVMS_TEST_STAT_FAIL_ON": str(app / "scripts/km-vms-update-helper.py")
        },
    )
    assert_fail_closed(rejected)
    assert "cannot read mode" in rejected.stderr.lower()
    assert product_mode_snapshot(app) == modes_before


def test_permission_gate_rejects_inventory_drift_during_mode_mutation(tmp_path):
    app = permission_tree(tmp_path)
    assert run_gate(app, "--fix").returncode == 0
    os.chmod(app / "docker-compose.yml", 0o666)
    real_chmod = shutil.which("chmod")
    assert real_chmod
    tool_dir = tmp_path / "chmod-drift"
    tool_dir.mkdir()
    marker = tmp_path / "chmod-drift.marker"
    drift_path = app / "apps/api/app/services/injected-during-fix.py"
    chmod_script = f"""#!/bin/sh
if [ ! -e "${{KMVMS_TEST_CHMOD_DRIFT_MARKER}}" ]; then
  : >"${{KMVMS_TEST_CHMOD_DRIFT_MARKER}}"
  : >"${{KMVMS_TEST_CHMOD_DRIFT_PATH}}"
fi
exec {real_chmod} "$@"
"""
    write_executable(tool_dir / "chmod", chmod_script)

    rejected = run_gate(
        app,
        "--fix",
        tool_dirs=(tool_dir,),
        extra_env={
            "KMVMS_TEST_CHMOD_DRIFT_MARKER": str(marker),
            "KMVMS_TEST_CHMOD_DRIFT_PATH": str(drift_path),
        },
    )

    assert_fail_closed(rejected)
    assert "inventory changed" in rejected.stderr.lower()
    assert drift_path.exists()


def test_permission_gate_scope_and_integrations_are_fail_closed():
    gate = GATE.read_text(encoding="utf-8")
    install = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    update = (ROOT / "scripts/update.sh").read_text(encoding="utf-8")
    release_cycle = (ROOT / "scripts/km-vms-release-cycle.sh").read_text(encoding="utf-8")
    install_docs = (ROOT / "docs/INSTALL.md").read_text(encoding="utf-8")

    assert "chmod -R" not in gate
    assert "apps deploy docs release scripts" in gate
    assert "build_inventory()" in gate
    assert "prepare_verified_manifest()" in gate
    assert "verify_manifest_stable()" in gate
    assert "apply_verified_manifest()" in gate
    assert "verify_manifest_strict()" in gate
    assert 'find "$APP_DIR/$scope_relative" -print' in gate
    assert 'chmod "$desired_mode" "$path"' in gate
    assert 'chmod u-s,g-s,o-t "$path"' in gate
    assert "acl_permissions_field()" in gate
    assert "acl_permissions_has_write()" in gate
    assert "?w?" in gate
    assert "*w*" not in gate
    assert "-exec chmod" not in gate
    preflight_inventory_call = gate.rindex(
        'build_inventory "$PREFLIGHT_INVENTORY" preflight'
    )
    fix_preflight_call = gate.rindex(
        'prepare_verified_manifest "$PREFLIGHT_INVENTORY" "$PREFLIGHT_MANIFEST" any fix-preflight'
    )
    stable_call = gate.rindex('verify_manifest_stable "$PREFLIGHT_MANIFEST"')
    mutation_call = gate.rindex('apply_verified_manifest "$PREFLIGHT_MANIFEST"')
    post_inventory_call = gate.rindex('build_inventory "$POST_INVENTORY" post')
    strict_call = gate.rindex('verify_manifest_strict "$PREFLIGHT_MANIFEST"')
    assert (
        preflight_inventory_call
        < fix_preflight_call
        < stable_call
        < mutation_call
        < post_inventory_call
        < strict_call
    )
    assert "getfacl -cp" in gate
    assert "getfacl is required; critical ACL state must not be skipped" in gate
    assert "Cannot enumerate product tree" in gate
    assert "ROOT_OWNER_GROUP" in gate
    assert ".env" not in gate.split("PRODUCT_TOP_FILES=", 1)[1].split(
        "EXECUTABLE_FILES=", 1
    )[0].splitlines()[1:]

    install_call = install.rindex("\napply_permission_policy\n")
    assert install.index("copy_source_dir", install.index("if [ ! -f")) < install_call
    assert install_call < install.rindex("\nload_compose_common\n")
    assert install_call < install.rindex("compose_cmd --env-file")

    preflight_call = update.rindex("\npreflight_permission_policy\n")
    overlay_call = update.rindex("\noverlay_source\n")
    permission_call = update.rindex("\napply_permission_policy\n")
    assert preflight_call < overlay_call
    assert overlay_call < permission_call
    assert permission_call < update.rindex('write_release_identity "precompose"')
    assert permission_call < update.rindex("\ncompose_config\n")
    assert 'trusted_gate="$TMP_ROOT/source/scripts/km-vms-permission-gate.sh"' in update
    assert "Source tree is missing scripts/km-vms-permission-gate.sh." in update

    assert "def check_permission_policy()" in release_cycle
    assert release_cycle.count("    check_permission_policy()\n") == 2
    assert "product-source permission gate failed" in release_cycle
    assert "must not be group- or world-writable" in install_docs
    assert "does not touch `.env`, `data`, `.git`" in install_docs


def test_permission_gate_runtime_images_install_acl_tools():
    update_helper_dockerfile = (ROOT / "apps/update-helper/Dockerfile").read_text(
        encoding="utf-8"
    )
    backend_test_dockerfile = (ROOT / "apps/api/Dockerfile.test").read_text(
        encoding="utf-8"
    )

    assert "RUN apk add --no-cache acl curl python3" in update_helper_dockerfile
    assert "\n      acl \\\n" in backend_test_dockerfile


def test_backend_test_runtime_provides_real_acl_tools():
    assert shutil.which("getfacl"), "backend test image must provide getfacl"
    assert shutil.which("setfacl"), "backend test image must provide setfacl"


def test_current_product_tree_passes_permission_gate():
    result = run_gate(ROOT, use_safe_getfacl=False)
    assert result.returncode == 0, result.stderr
    assert "permission_gate=PASS" in result.stdout


def load_bridge():
    path = ROOT / "scripts/km-vms-update-helper-bridge.py"
    spec = importlib.util.spec_from_file_location(f"km_vms_update_helper_bridge_{uuid.uuid4().hex}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_tree_contract_allows_target_only_files_to_be_absent(tmp_path):
    app = permission_tree(tmp_path)
    (app / "scripts/km-vms-permission-gate.sh").unlink()
    (app / "scripts/km-vms-update-helper-bridge.py").unlink()
    env = os.environ.copy()
    env["PATH"] = str(safe_acl_tool_dir(app.parent)) + os.pathsep + env.get("PATH", "")

    result = subprocess.run(
        [
            str(SHELL),
            str(GATE),
            "--preflight-existing",
            "--fix",
            "--app-dir",
            str(app),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "permission_gate=PASS" in result.stdout
    assert "permission_contract=existing" in result.stdout


def test_target_contract_requires_update_helper_bridge(tmp_path):
    app = permission_tree(tmp_path)
    (app / "scripts/km-vms-update-helper-bridge.py").unlink()

    result = run_gate(app, "--check")

    assert_fail_closed(result)
    assert "km-vms-update-helper-bridge.py" in result.stderr


def test_bridge_reads_only_top_level_status_and_rejects_duplicate_authority(tmp_path):
    bridge = load_bridge()
    request_id = "update-" + "a" * 32
    status_file = tmp_path / "update-status.json"
    status_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request_id,
                "status": "applying",
                "steps": [
                    {"name": "preflight", "status": "completed"},
                    {"name": "rebuilding", "status": "running"},
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    observed_request, observed_status, _payload = bridge.read_status(status_file)

    assert observed_request == request_id
    assert observed_status == "applying"

    status_file.write_text(
        '{"schema_version":1,"request_id":"'
        + request_id
        + '","status":"applying","status":"completed"}\n',
        encoding="utf-8",
    )
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.read_status(status_file)
    assert exc.value.code == "control_file_invalid"


def test_bridge_completed_status_requires_matching_verified_commit():
    bridge = load_bridge()
    request_id = "update-" + "b" * 32
    payload = {
        "request_id": request_id,
        "status": "completed",
        "commit_verified": True,
        "expected_commit": "c" * 40,
        "installed_commit": "c" * 40,
        "finished_at": "2026-07-23T19:00:00Z",
    }

    bridge.validate_completed_status(payload, request_id)
    payload["installed_commit"] = "d" * 40
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.validate_completed_status(payload, request_id)
    assert exc.value.code == "terminal_commit_invalid"


def test_bridge_bootstrap_schedules_active_status_with_nested_completed_step(
    tmp_path, monkeypatch, capsys
):
    bridge = load_bridge()
    app = permission_tree(tmp_path)
    control = app / "data/update-control"
    control.mkdir(parents=True)
    write(app / ".env", "COMPOSE_PROJECT_NAME=kmvmsfixture\n")
    request_id = "update-" + "e" * 32
    write(
        control / "update-status.json",
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request_id,
                "status": "applying",
                "steps": [{"name": "preflight", "status": "completed"}],
            }
        )
        + "\n",
    )
    expected_image_id = "sha256:" + "f" * 64
    captured: dict[str, object] = {}
    events: list[str] = []

    monkeypatch.setattr(bridge, "ensure_docker_runtime", lambda: None)
    monkeypatch.setattr(
        bridge,
        "run_target_permission_gate",
        lambda _app_dir: events.append("permission_gate"),
    )

    def fake_docker_image_id(_image):
        events.append("image")
        return expected_image_id

    def fake_validate_receipt_binding(*_args):
        events.append("receipt")

    monkeypatch.setattr(bridge, "docker_image_id", fake_docker_image_id)
    monkeypatch.setattr(bridge, "validate_receipt_binding", fake_validate_receipt_binding)

    def fake_schedule_refresh(**kwargs):
        events.append("schedule")
        captured.update(kwargs)
        return "scheduled"

    monkeypatch.setattr(bridge, "schedule_refresh", fake_schedule_refresh)
    args = type(
        "Args",
        (),
        {
            "app_dir": str(app),
            "project_name": "kmvmsfixture",
            "helper_image": "km-vms-kmvmsfixture-update-helper:local",
            "require_request_id": request_id,
            "timeout_seconds": 7800,
        },
    )()

    assert bridge.bootstrap(args) == 0
    assert events == ["permission_gate", "image", "receipt", "schedule"]
    assert captured["request_id"] == request_id
    assert captured["expected_image_id"] == expected_image_id
    output = capsys.readouterr().out
    assert output.count("permission_gate=PASS") == 1
    assert "update_helper_bootstrap=PASS" in output


def test_bridge_permission_pair_buffers_fix_pass_until_check_succeeds(
    tmp_path, monkeypatch, capsys
):
    bridge = load_bridge()
    app = permission_tree(tmp_path)
    calls: list[str] = []

    def fake_run_command(args, **_kwargs):
        action = args[2]
        calls.append(action)
        output = (
            "permission_gate=PASS\n"
            f"permission_action={action.removeprefix('--')}\n"
            f"permission_app_dir={app}\n"
            "permission_owner_group=0:0\n"
            "permission_contract=target\n"
        )
        if action == "--check":
            return subprocess.CompletedProcess(args, 1, "", "simulated strict failure\n")
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    with pytest.raises(bridge.BridgeError) as exc:
        bridge.run_target_permission_gate(app)

    assert exc.value.code == "target_permission_check_failed"
    assert calls == ["--fix", "--check"]
    assert "permission_gate=PASS" not in capsys.readouterr().out


def test_bridge_permission_pair_runs_real_gate_without_forwarding_child_pass(
    tmp_path, capsys
):
    if not shutil.which("getfacl"):
        pytest.skip("POSIX ACL inspection is unavailable in the isolated test container")
    bridge = load_bridge()
    app = permission_tree(tmp_path)
    shutil.copy2(GATE, app / "scripts/km-vms-permission-gate.sh")
    target = app / "docker-compose.yml"
    os.chmod(target, 0o666)

    bridge.run_target_permission_gate(app)

    assert mode(target) == 0o644
    assert capsys.readouterr().out == ""


def test_bridge_bootstrap_gate_failure_precedes_image_receipt_and_coordinator(
    tmp_path, monkeypatch, capsys
):
    bridge = load_bridge()
    app = permission_tree(tmp_path)
    control = app / "data/update-control"
    control.mkdir(parents=True)
    write(app / ".env", "COMPOSE_PROJECT_NAME=kmvmsfixture\n")
    request_id = "update-" + "9" * 32
    write(
        control / "update-status.json",
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request_id,
                "status": "applying",
            }
        )
        + "\n",
    )
    receipt = control / "update-helper-refresh.json"
    receipt.write_bytes(b"existing-receipt\n")

    monkeypatch.setattr(bridge, "ensure_docker_runtime", lambda: None)

    def reject_gate(_app_dir):
        raise bridge.BridgeError(
            "target_permission_fix_failed",
            "simulated permission failure",
        )

    monkeypatch.setattr(bridge, "run_target_permission_gate", reject_gate)
    monkeypatch.setattr(
        bridge,
        "docker_image_id",
        lambda _image: pytest.fail("image inspection must follow the permission pair"),
    )
    monkeypatch.setattr(
        bridge,
        "validate_receipt_binding",
        lambda *_args: pytest.fail("receipt inspection must follow the permission pair"),
    )
    monkeypatch.setattr(
        bridge,
        "schedule_refresh",
        lambda **_kwargs: pytest.fail("coordinator scheduling must follow the permission pair"),
    )
    args = type(
        "Args",
        (),
        {
            "app_dir": str(app),
            "project_name": "kmvmsfixture",
            "helper_image": "km-vms-kmvmsfixture-update-helper:local",
            "require_request_id": request_id,
            "timeout_seconds": 7800,
        },
    )()

    with pytest.raises(bridge.BridgeError) as exc:
        bridge.bootstrap(args)

    assert exc.value.code == "target_permission_fix_failed"
    assert receipt.read_bytes() == b"existing-receipt\n"
    assert "permission_gate=PASS" not in capsys.readouterr().out


def test_bridge_bootstrap_ignores_inert_precanonical_terminal_status(
    tmp_path, monkeypatch, capsys
):
    bridge = load_bridge()
    app = tmp_path / "app"
    control = app / "data/update-control"
    scripts = app / "scripts"
    control.mkdir(parents=True)
    scripts.mkdir()
    write(app / "docker-compose.yml", "services: {}\n")
    write(app / ".env", "COMPOSE_PROJECT_NAME=kmvmsfixture\n")
    shutil.copy2(ROOT / "scripts/km-vms-permission-gate.sh", scripts)
    shutil.copy2(ROOT / "scripts/km-vms-update-helper-bridge.py", scripts)
    write(
        control / "update-status.json",
        json.dumps(
            {
                "schema_version": 1,
                "request_id": "stage609-81115785d882423293369548374fd642",
                "status": "completed",
                "phase": "completed",
            }
        )
        + "\n",
    )

    monkeypatch.setattr(bridge, "ensure_docker_runtime", lambda: None)
    monkeypatch.setattr(
        bridge,
        "run_target_permission_gate",
        lambda _app_dir: pytest.fail("an inert terminal record must not mutate product permissions"),
    )
    monkeypatch.setattr(
        bridge,
        "docker_image_id",
        lambda _image: pytest.fail("an inert terminal record must not inspect the helper image"),
    )
    args = type(
        "Args",
        (),
        {
            "app_dir": str(app),
            "project_name": "kmvmsfixture",
            "helper_image": "km-vms-kmvmsfixture-update-helper:local",
            "require_request_id": None,
            "timeout_seconds": 7800,
        },
    )()

    assert bridge.bootstrap(args) == 0
    assert "update_helper_bootstrap=NO_ACTIVE_UPDATE" in capsys.readouterr().out


def test_bridge_rejects_precanonical_identity_for_active_status(tmp_path):
    bridge = load_bridge()
    status_file = tmp_path / "update-status.json"
    write(
        status_file,
        json.dumps(
            {
                "schema_version": 1,
                "request_id": "stage609-81115785d882423293369548374fd642",
                "status": "applying",
            }
        )
        + "\n",
    )

    with pytest.raises(bridge.BridgeError) as exc:
        bridge.read_status(status_file)
    assert exc.value.code == "status_request_invalid"


def test_bridge_coordinator_runs_from_exact_prepared_image_without_external_pull(
    tmp_path, monkeypatch
):
    bridge = load_bridge()
    request_id = "update-" + "1" * 32
    expected_image_id = "sha256:" + "2" * 64
    commands: list[list[str]] = []
    monkeypatch.setattr(bridge, "inspected_container_image", lambda _identity: None)

    def fake_run_command(args, **_kwargs):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, "coordinator-id\n", "")

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    result = bridge.schedule_refresh(
        app_dir=tmp_path,
        project_name="kmvmsfixture",
        helper_image="km-vms-kmvmsfixture-update-helper:local",
        request_id=request_id,
        expected_image_id=expected_image_id,
        timeout_seconds=7800,
    )

    assert result == "scheduled"
    assert commands[0][0:3] == ["docker", "run", "-d"]
    assert commands[0][commands[0].index("--entrypoint") + 1] == "python3"
    assert commands[0][commands[0].index(expected_image_id) + 1].endswith(
        "km-vms-update-helper-bridge.py"
    )
    assert expected_image_id in commands[0]
    assert "docker:27-cli" not in commands[0]
