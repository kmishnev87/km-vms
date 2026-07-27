from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts/km-vms-permission-gate.sh"
SHELL = Path(shutil.which("sh") or "/bin/sh")

PRIVILEGED_EXECUTABLES = (
    "scripts/install.sh",
    "scripts/update.sh",
    "scripts/km-vms-compose-common.sh",
    "scripts/km-vms-setup-activation-helper.sh",
    "scripts/km-vms-release-cycle.sh",
    "scripts/km-vms-adopt-release-identity.sh",
    "scripts/km-vms-restart.sh",
    "scripts/km-vms-storage-apply.sh",
    "scripts/km-vms-storage-discovery.sh",
    "scripts/km-vms-permission-gate.sh",
    "scripts/km-vms-publish-github-release.sh",
)

PRIVILEGED_NON_EXECUTABLES = (
    "docker-compose.yml",
    "apps/update-helper/Dockerfile",
    "scripts/km-vms-update-helper.py",
    "scripts/km-vms-update-helper-bridge.py",
    "scripts/km-vms-storage-candidate-validate.sh",
    "scripts/km-vms-storage-root-cleanup.sh",
)

UNLISTED_PRODUCT_FILES = (
    "apps/api/app/runtime.py",
    "apps/api/tests/fixture.py",
    "apps/web/static/app.js",
    "deploy/nginx/default.conf",
    "docs/INSTALL.md",
    "release/km-vms-release.json",
    "scripts/run_backend_tests.sh",
)


def write(path: Path, text: str = "fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_executable(path: Path, text: str) -> None:
    write(path, text)
    os.chmod(path, 0o755)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def permission_tree(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    for relative in (
        "apps/update-helper",
        "apps/api/app",
        "apps/api/tests",
        "apps/web/static",
        "deploy/nginx",
        "docs",
        "release",
        "scripts",
        "data/private",
        ".git",
    ):
        (app / relative).mkdir(parents=True, exist_ok=True)

    for relative in PRIVILEGED_EXECUTABLES:
        write(app / relative, "#!/usr/bin/env sh\n")
    for relative in PRIVILEGED_NON_EXECUTABLES:
        write(app / relative)
    for relative in UNLISTED_PRODUCT_FILES:
        write(app / relative)

    write(app / ".env", "SECRET=fixture\n")
    write(app / ".km-vms-source.json", "{}\n")
    write(app / ".km-vms-release.json", "{}\n")
    write(app / "data/private/runtime.control")
    write(app / ".git/local-state")

    for path in app.rglob("*"):
        if path.is_dir():
            os.chmod(path, 0o777)
        elif path.is_file():
            os.chmod(path, 0o666)
    for relative in PRIVILEGED_EXECUTABLES:
        os.chmod(app / relative, 0o777)
    for relative in (".env", ".km-vms-source.json", ".km-vms-release.json"):
        os.chmod(app / relative, 0o640)
    os.chmod(app / "data/private/runtime.control", 0o660)
    os.chmod(app, 0o777)
    return app


def safe_acl_tool_dir(base: Path) -> Path:
    tool_dir = base / ".safe-acl-tools"
    tool_dir.mkdir(exist_ok=True)
    write_executable(
        tool_dir / "getfacl",
        "#!/bin/sh\n"
        "printf 'user::rwx\\ngroup::r-x\\nother::r-x\\n'\n",
    )
    return tool_dir


def selective_acl_tool_dir(base: Path) -> Path:
    tool_dir = base / ".selective-acl-tools"
    tool_dir.mkdir(exist_ok=True)
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


def no_acl_tool_dir(base: Path) -> Path:
    tool_dir = base / ".mode-only-tools"
    tool_dir.mkdir(exist_ok=True)
    for command in ("stat", "cut"):
        executable = shutil.which(command)
        assert executable
        write_executable(
            tool_dir / command,
            f'#!/bin/sh\nexec "{executable}" "$@"\n',
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
    preflight_existing: bool = False,
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
    command = [str(SHELL), str(GATE)]
    if preflight_existing:
        command.append("--preflight-existing")
    command.extend((action, "--app-dir", str(app)))
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def assert_pass(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr
    assert "permission_gate=PASS" in result.stdout
    assert "permission_scope=privileged_chain" in result.stdout


def test_permission_fix_is_bounded_and_idempotent(tmp_path: Path) -> None:
    app = permission_tree(tmp_path)
    untouched = {
        relative: (mode(app / relative), (app / relative).read_bytes())
        for relative in (
            *UNLISTED_PRODUCT_FILES,
            ".env",
            ".km-vms-source.json",
            ".km-vms-release.json",
            "data/private/runtime.control",
            ".git/local-state",
        )
    }
    untouched_dirs = {
        relative: mode(app / relative)
        for relative in ("apps/api", "apps/web", "deploy", "docs", "release", "data")
    }

    fixed = run_gate(app, "--fix")
    assert_pass(fixed)
    assert "permission_action=fix" in fixed.stdout

    for relative in (".", "apps", "apps/update-helper", "scripts"):
        target = app if relative == "." else app / relative
        assert mode(target) == 0o755
    for relative in PRIVILEGED_EXECUTABLES:
        assert mode(app / relative) == 0o755
    for relative in PRIVILEGED_NON_EXECUTABLES:
        assert mode(app / relative) == 0o644
    for relative, expected in untouched.items():
        assert (mode(app / relative), (app / relative).read_bytes()) == expected
    for relative, expected_mode in untouched_dirs.items():
        assert mode(app / relative) == expected_mode

    checked = run_gate(app)
    assert_pass(checked)


@pytest.mark.parametrize(
    ("relative", "bad_mode"),
    (
        ("scripts/update.sh", 0o775),
        ("scripts/km-vms-update-helper.py", 0o666),
        ("docker-compose.yml", 0o646),
    ),
)
def test_permission_gate_rejects_shared_write_on_privileged_chain(
    tmp_path: Path,
    relative: str,
    bad_mode: int,
) -> None:
    app = permission_tree(tmp_path)
    assert_pass(run_gate(app, "--fix"))
    os.chmod(app / relative, bad_mode)

    rejected = run_gate(app)

    assert rejected.returncode != 0
    assert "group/world-writable" in rejected.stderr


def test_permission_gate_ignores_unlisted_non_executable_asset_mode(
    tmp_path: Path,
) -> None:
    app = permission_tree(tmp_path)
    assert_pass(run_gate(app, "--fix"))
    os.chmod(app / "apps/web/static/app.js", 0o666)

    assert_pass(run_gate(app))


def test_permission_gate_rejects_privileged_symlink(tmp_path: Path) -> None:
    app = permission_tree(tmp_path)
    assert_pass(run_gate(app, "--fix"))
    target = app / "scripts/km-vms-update-helper-bridge.py"
    target.unlink()
    try:
        target.symlink_to(app / "scripts/km-vms-update-helper.py")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    rejected = run_gate(app)

    assert rejected.returncode != 0
    assert "must not be a symlink" in rejected.stderr


def test_permission_gate_accepts_harmless_named_read_acl(tmp_path: Path) -> None:
    app = permission_tree(tmp_path)
    assert_pass(run_gate(app, "--fix"))
    target = app / "scripts/km-vms-update-helper.py"

    result = run_gate(
        app,
        tool_dirs=(selective_acl_tool_dir(tmp_path),),
        use_safe_getfacl=False,
        extra_env={
            "KMVMS_TEST_ACL_PATH": str(target),
            "KMVMS_TEST_ACL_LINE": "user:www-data:r--",
        },
    )

    assert_pass(result)


def test_permission_gate_rejects_named_write_acl(tmp_path: Path) -> None:
    app = permission_tree(tmp_path)
    assert_pass(run_gate(app, "--fix"))
    target = app / "scripts/km-vms-update-helper.py"

    rejected = run_gate(
        app,
        tool_dirs=(selective_acl_tool_dir(tmp_path),),
        use_safe_getfacl=False,
        extra_env={
            "KMVMS_TEST_ACL_PATH": str(target),
            "KMVMS_TEST_ACL_LINE": "user:untrusted:rw-",
        },
    )

    assert rejected.returncode != 0
    assert "ACL grants non-owner write" in rejected.stderr


def test_permission_gate_passes_without_getfacl(tmp_path: Path) -> None:
    app = permission_tree(tmp_path)
    assert_pass(run_gate(app, "--fix"))

    result = run_gate(
        app,
        tool_dirs=(no_acl_tool_dir(tmp_path),),
        use_safe_getfacl=False,
        replace_path=True,
    )

    assert_pass(result)
    assert "permission_acl_check=mode_only" in result.stdout


def test_permission_fix_preserves_runtime_authority_files(tmp_path: Path) -> None:
    app = permission_tree(tmp_path)
    install_control = app / "data/install-control"
    install_control.mkdir()
    os.chmod(app / "data", 0o750)
    os.chmod(install_control, 0o750)
    write(install_control / "archive-roots-runtime.json", "{}\n")
    write(
        install_control / "docker-compose.archive-roots.yml",
        "services: {}\n",
    )
    for path in install_control.iterdir():
        os.chmod(path, 0o640)
    before = {
        path: (mode(path), path.read_bytes())
        for path in (
            app / ".env",
            app / ".km-vms-source.json",
            app / ".km-vms-release.json",
            *tuple(install_control.iterdir()),
        )
    }

    assert_pass(run_gate(app, "--fix"))

    for path, expected in before.items():
        assert (mode(path), path.read_bytes()) == expected
    assert mode(app / "data") == 0o750
    assert mode(install_control) == 0o750


def test_existing_contract_allows_target_only_files_to_be_absent(
    tmp_path: Path,
) -> None:
    app = permission_tree(tmp_path)
    (app / "scripts/km-vms-permission-gate.sh").unlink()
    (app / "scripts/km-vms-update-helper-bridge.py").unlink()

    result = run_gate(
        app,
        "--fix",
        preflight_existing=True,
    )

    assert_pass(result)
    assert "permission_contract=existing" in result.stdout


def test_target_contract_requires_update_helper_bridge(tmp_path: Path) -> None:
    app = permission_tree(tmp_path)
    (app / "scripts/km-vms-update-helper-bridge.py").unlink()

    result = run_gate(app)

    assert result.returncode != 0
    assert "km-vms-update-helper-bridge.py" in result.stderr


def test_permission_scope_and_update_integrations_are_narrow() -> None:
    gate = GATE.read_text(encoding="utf-8")
    update = (ROOT / "scripts/update.sh").read_text(encoding="utf-8")
    bridge = (ROOT / "scripts/km-vms-update-helper-bridge.py").read_text(
        encoding="utf-8"
    )
    install = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    release_cycle = (ROOT / "scripts/km-vms-release-cycle.sh").read_text(
        encoding="utf-8"
    )

    assert "PRIVILEGED_FILES=" in gate
    assert 'PRIVILEGED_DIRECTORIES=". apps apps/update-helper scripts"' in gate
    assert "build_inventory" not in gate
    assert "find " not in gate
    assert "PRODUCT_TOP_FILES" not in gate
    assert "permission_scope=privileged_chain" in gate
    assert "command -v getfacl" in gate
    assert "getfacl is required" not in gate
    assert "getfacl --version" not in update
    assert "getfacl --version" not in bridge
    assert "helper_acl_runtime_missing" not in bridge

    assert "\npreflight_permission_policy\n" in update
    assert "\napply_permission_policy\n" in update
    assert "\napply_permission_policy\n" in install
    assert "def check_permission_policy()" in release_cycle


def test_current_product_tree_passes_permission_gate() -> None:
    result = run_gate(ROOT, use_safe_getfacl=False)
    assert_pass(result)


def load_bridge():
    path = ROOT / "scripts/km-vms-update-helper-bridge.py"
    spec = importlib.util.spec_from_file_location(
        f"km_vms_update_helper_bridge_{uuid.uuid4().hex}",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_runs_permission_fix_and_check_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = load_bridge()
    app = permission_tree(tmp_path)
    calls: list[str] = []

    def fake_run_command(args, **_kwargs):
        action = args[2]
        calls.append(action)
        return subprocess.CompletedProcess(
            args,
            0,
            (
                "permission_gate=PASS\n"
                f"permission_action={action.removeprefix('--')}\n"
                f"permission_app_dir={app}\n"
                "permission_contract=target\n"
            ),
            "",
        )

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    bridge.run_target_permission_gate(app)

    assert calls == ["--fix", "--check"]


def test_bridge_does_not_publish_fix_pass_when_followup_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bridge = load_bridge()
    app = permission_tree(tmp_path)

    def fake_run_command(args, **_kwargs):
        action = args[2]
        if action == "--check":
            return subprocess.CompletedProcess(args, 1, "", "check failed\n")
        return subprocess.CompletedProcess(
            args,
            0,
            (
                "permission_gate=PASS\n"
                "permission_action=fix\n"
                f"permission_app_dir={app}\n"
                "permission_contract=target\n"
            ),
            "",
        )

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.run_target_permission_gate(app)

    assert captured.value.code == "target_permission_check_failed"
    assert "permission_gate=PASS" not in capsys.readouterr().out
