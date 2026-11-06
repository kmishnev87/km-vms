#!/usr/bin/env python3
"""Select optional DRM passthrough without making it an installation prerequisite.

Host launchers can inspect /dev/dri directly. The update/setup helpers cannot:
their /dev belongs to the helper, not the Docker host. In that case a bounded,
networkless device check in the helper's exact existing image asks Docker to check
the device mapping. It never mounts application data or downloads an image.
There is no daemon, timer, permanent probe container, or generated source file.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import uuid
from pathlib import Path

OVERRIDE = Path("deploy/compose/optional-gpu.yml")
OFF_MODES = {"off", "disabled", "false", "0"}


def acceleration_disabled(app_dir: Path) -> bool:
    # Only read this one non-secret setting; never source/evaluate the .env.
    env_file = app_dir / ".env"
    if not env_file.is_file():
        return False
    with env_file.open(encoding="utf-8") as stream:
        for line in stream:
            key, separator, value = line.partition("=")
            if separator and key.strip() == "LIVE_HWACCEL_MODE":
                return value.strip().strip("\"'").lower() in OFF_MODES
    return False


def container_context() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def local_drm_devices() -> bool:
    try:
        return any(stat.S_ISCHR(item.stat().st_mode) for item in Path("/dev/dri").iterdir())
    except OSError:
        return False


def docker_host_drm_devices() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    name = "km-vms-gpu-probe-" + uuid.uuid4().hex
    try:
        inspected = subprocess.run(
            [docker, "inspect", "--format", "{{.Image}}", socket.gethostname()],
            capture_output=True, text=True, timeout=10, check=False,
        )
        image = inspected.stdout.strip()
        if inspected.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            return False
        checked = subprocess.run(
            [docker, "run", "--rm", "--pull=never", "--name", name,
             "--network", "none", "--no-healthcheck", "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--pids-limit", "16",
             "--memory", "32m", "--cpus", "0.1",
             "--device", "/dev/dri:/dev/dri", "--entrypoint", "/bin/sh", image,
             "-c", 'for d in /dev/dri/*; do [ -c "$d" ] && exit 0; done; exit 1'],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return checked.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        # An unavailable optional accelerator must never block CPU operation.
        return False
    finally:
        # --rm handles the normal path; also reclaim a creation/start timeout.
        try:
            subprocess.run(
                [docker, "rm", "--force", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def select_override(app_dir: Path, source_dir: Path) -> Path | None:
    override = source_dir / OVERRIDE
    if not override.exists() and not override.is_symlink():
        # Historical rollback sources retain their exact original contract.
        return None
    if override.is_symlink() or not override.is_file():
        raise ValueError("Optional GPU Compose source is unsafe.")
    if acceleration_disabled(app_dir):
        return None
    available = docker_host_drm_devices() if container_context() else local_drm_devices()
    return override if available else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    args = parser.parse_args()
    if not args.app_dir.is_absolute() or not args.source_dir.is_absolute():
        parser.error("Application and source directories must be absolute.")
    try:
        selected = select_override(args.app_dir, args.source_dir)
    except (OSError, ValueError):
        print("Optional GPU configuration could not be read safely.", file=sys.stderr)
        return 1
    if selected is not None:
        print(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
