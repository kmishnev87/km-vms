#!/usr/bin/env python3
"""Turn archive *intent*, never API-writable Compose, into narrow host binds.

Only this privileged side writes archive-authority/ and its generated override.
Existing daemon mounts and the installer-owned .env preserve installed archives;
new folders are independently inspected through a fixed, read-only host probe.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

MAX_JSON = 256 * 1024
PYTHON_IMAGE = "python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159"
PROBE_IMAGE_PULL_TIMEOUT_SECONDS = 120
ROOT_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")
PROJECT = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
PRIMARY_ARCHIVE = "/storage/archive"
AUTHORITY_DIRECTORY = "data/update-runtime/archive-authority"
ARCHIVE_EVIDENCE = "mount-evidence.json"
OFFLINE_PRIMARY = "unavailable-primary"


class ArchivePolicyError(RuntimeError):
    code = "archive_roots_manifest_invalid"


class ArchiveProbeImageError(ArchivePolicyError):
    code = "archive_probe_image_unavailable"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArchivePolicyError("Archive intent contains a duplicate key.")
        result[key] = value
    return result


def read_json(path: Path, *, missing_ok=False):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if missing_ok and not path.is_symlink():
            return None
        raise ArchivePolicyError("Archive intent is unavailable.") from None
    except OSError:
        raise ArchivePolicyError("Archive intent is not a regular file.") from None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_JSON:
                raise ArchivePolicyError("Archive intent size or type is invalid.")
            data = stream.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise ArchivePolicyError("Archive intent is too large.")
        return json.loads(data, object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ArchivePolicyError("Archive intent is invalid JSON.") from None


def host_path(value):
    if (not isinstance(value, str) or not value.startswith("/")
            or len(value) > 1024 or any(ord(c) < 32 for c in value)
            or ".." in value.split("/") or "\\" in value):
        raise ArchivePolicyError("Archive host path is invalid.")
    return str(Path(value))


def parse_intent(value):
    if (type(value) is not dict or set(value) != {"schema_version", "runtime_base", "compose_override_file", "items", "raw_runtime_paths_user_visible"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["runtime_base"] != "/storage/archive-roots"
            or value["compose_override_file"] != "docker-compose.archive-roots.yml"
            or value["raw_runtime_paths_user_visible"] is not False
            or type(value["items"]) is not list or len(value["items"]) > 128):
        raise ArchivePolicyError("Archive intent contract is invalid.")
    result, seen_ids, seen_targets = [], set(), set()
    for item in value["items"]:
        if type(item) is not dict or set(item) != {"root_id", "user_display_path", "backend_runtime_path", "physical_volume_id", "storage_namespace", "active_write_target"}:
            raise ArchivePolicyError("Archive intent item is invalid.")
        root_id, target = item["root_id"], item["backend_runtime_path"]
        if (not isinstance(root_id, str) or not 0 < len(root_id) <= 128
                or any(ord(c) < 32 for c in root_id)
                or not isinstance(target, str) or not target.startswith("/storage/archive-roots/")
                or not ROOT_ID.fullmatch(target.removeprefix("/storage/archive-roots/"))
                or root_id in seen_ids or target in seen_targets
                or item["storage_namespace"] != "kmvms/recordings"
                or type(item["active_write_target"]) is not bool
                or not isinstance(item["physical_volume_id"], str)
                or not 0 < len(item["physical_volume_id"]) <= 1024):
            raise ArchivePolicyError("Archive intent item is unsafe.")
        seen_ids.add(root_id)
        seen_targets.add(target)
        result.append({"source": host_path(item["user_display_path"]), "target": target, "active": item["active_write_target"]})
    return result


def _host_probe(payload, host_root="/host", mountinfo="/proc/self/mountinfo"):
    # This function is shipped verbatim to a fixed read-only Python container.
    # Absolute host symlinks must be rebased, not resolved in its own rootfs.
    import hashlib
    import json
    import os
    import re
    import stat
    from pathlib import Path, PurePosixPath

    root = Path(host_root)

    def canonical(value):
        pending, parts, links, steps = list(PurePosixPath(value).parts[1:]), [], 0, 0
        while pending:
            steps += 1
            if steps > 512:
                raise ValueError("Archive path resolution exceeded its limit.")
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise ValueError("Archive path escaped the host root.")
                parts.pop()
                continue
            candidate = root.joinpath(*parts, part)
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                links += 1
                if links > 40:
                    raise ValueError("Archive path contains a symlink loop.")
                destination = os.readlink(candidate)
                if destination.startswith("/"):
                    parts = []
                pending = list(PurePosixPath(destination).parts[1:] if destination.startswith("/") else PurePosixPath(destination).parts) + pending
            else:
                parts.append(part)
        resolved = root.joinpath(*parts)
        if not resolved.is_dir():
            raise ValueError("Archive path is not a directory.")
        return "/" + "/".join(parts)

    def overlaps(a, b):
        return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")

    app = canonical(payload["app_dir"])
    default = app + "/data/archive"
    docker_root = canonical(payload["docker_root"])
    trusted_raw = set(payload["trusted"])
    previous = payload.get("previous", {})
    trusted = set()
    for path in payload["trusted"]:
        try:
            trusted.add(canonical(path))
        except OSError:
            pass  # A removed, no-longer-requested archive must not block cleanup.
    blocked = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run", "/sbin", "/sys", "/usr", "/var", "/root", "/tmp", "/home")
    mounts = {}
    with open(mountinfo, encoding="utf-8") as stream:
        for line in stream:
            fields = line.split()
            if len(fields) < 7:
                continue
            point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
            host_prefix = str(root).rstrip("/")
            if point == host_prefix or point.startswith(host_prefix + "/"):
                point = point[len(host_prefix):] or "/"
                separator = fields.index("-")
                filesystem = fields[separator + 1]
                if filesystem not in {"proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "tmpfs", "overlay", "squashfs", "aufs", "debugfs", "tracefs", "securityfs", "pstore", "configfs", "mqueue", "hugetlbfs", "fusectl", "autofs"}:
                    try:
                        # The isolated host probe deliberately sees read-only
                        # view mounts; only their superblock flags carry the
                        # underlying filesystem's read-only state.
                        super_read_only = "ro" in fields[separator + 3].split(",")
                        mount_read_only = root == Path("/") and "ro" in fields[5].split(",")
                        mounts[canonical(point)] = (filesystem, fields[3], super_read_only or mount_read_only)
                    except (OSError, ValueError):
                        continue

    def check_location(path, installer_default):
        if (path == "/" or (not installer_default and any(path == p or path.startswith(p + "/") for p in blocked))
                or overlaps(path, docker_root) or (overlaps(path, app) and not installer_default)
                or path in mounts):
            raise ValueError("Archive path overlaps a protected host location.")

    def filesystem_identity(source, expected_nas_volume):
        points = [point for point in mounts if source.startswith(point.rstrip("/") + "/")]
        point = max(points, key=len, default=None)
        if point is None or (expected_nas_volume and canonical(expected_nas_volume) not in points):
            return None, False  # A leftover directory is not a mounted NAS volume.
        filesystem, fs_root, read_only = mounts[point]
        local = root / source.lstrip("/")
        fsid = getattr(os.statvfs(local), "f_fsid", 0)
        if not fsid:
            # Do not invent a durable device identity on an unsupported FS.
            return None, read_only
        return {
            "volume": hashlib.sha256(f"{filesystem}:{fsid}".encode()).hexdigest(),
            "mountpoint": point,
            "mount_root": fs_root,
            "inode": local.stat().st_ino,
        }, read_only

    result = []
    for item in payload["items"]:
        configured, target = item["source"], item["target"]
        prior = previous.get(configured) or {}
        known = bool(prior or configured in trusted_raw or item.get("installer"))
        is_default = configured == payload["app_dir"].rstrip("/") + "/data/archive"
        check_location(configured, is_default)
        expected_nas_volume = None
        if re.fullmatch(r"/[Vv]olume[0-9]+", "/" + configured.split("/")[1]):
            expected_nas_volume = "/" + configured.split("/")[1]

        def unavailable():
            return {"configured": configured, "source": prior.get("source") or configured,
                    "target": target, "available": False, "identity": prior.get("identity")}

        try:
            source = canonical(configured)
        except OSError as exc:
            if known:
                result.append(unavailable())
                continue
            # Add-root may publish an inactive intent before the existing
            # activation helper creates its folder. Never bind/approve it yet.
            if (isinstance(exc, FileNotFoundError) and item.get("active") is False and target != "/storage/archive"
                    and str(PurePosixPath(configured).parent) in mounts):
                continue
            raise
        is_default = source == default and is_default
        check_location(source, is_default)
        try:
            identity, read_only = filesystem_identity(source, expected_nas_volume)
        except OSError:
            if not known:
                raise
            result.append(unavailable())
            continue
        if (expected_nas_volume and identity is None) or (
            prior.get("identity") and (prior["source"] != source or prior["identity"] != identity)
        ):
            if not known:
                raise ValueError("Archive volume identity cannot be verified.")
            result.append(unavailable())
            continue
        if prior and not prior.get("available") and not prior.get("identity") and not is_default:
            # First start on a newer runtime may already be missing the disk.
            # No previous fingerprint can prove a replacement is the old disk.
            # Do not silently enroll one merely because its path is occupied.
            result.append(unavailable())
            continue
        if prior and not prior.get("available") and read_only:
            result.append(unavailable())
            continue
        if source not in trusted and not is_default:
            # A marker in an arbitrary personal folder is not approval.
            if str(PurePosixPath(source).parent) not in mounts:
                raise ValueError("New archive must be a dedicated folder on a discovered volume.")
            if any(overlaps(source, approved) for approved in trusted):
                raise ValueError("New archive overlaps an existing writable archive.")
            entries = []
            with os.scandir(root / source.lstrip("/")) as iterator:
                for entry in iterator:
                    entries.append(entry.name)
                    if len(entries) > 5 or entry.is_symlink():
                        raise ValueError("New archive contains unrelated files or symlinks.")
            allowed = {".km-vms-storage-root.json", "kmvms", ".km-vms-internal", ".km-vms-internal-quarantine", ".kmvms_retention_apply.lock"}
            if set(entries) - allowed:
                raise ValueError("New archive contains unrelated files.")
            for name in set(entries) & {".km-vms-internal", ".km-vms-internal-quarantine"}:
                if not (root / source.lstrip("/") / name).is_dir():
                    raise ValueError("New archive internal namespace is invalid.")
            if ".kmvms_retention_apply.lock" in entries and not (root / source.lstrip("/") / ".kmvms_retention_apply.lock").is_file():
                raise ValueError("New archive lock is invalid.")
            if "kmvms" in entries:
                namespace = root / source.lstrip("/") / "kmvms"
                if not namespace.is_dir() or any(child.name != "recordings" or child.is_symlink() or not child.is_dir() for child in namespace.iterdir()):
                    raise ValueError("New archive namespace is invalid.")
            if ".km-vms-storage-root.json" in entries:
                marker = root / source.lstrip("/") / ".km-vms-storage-root.json"
                if not marker.is_file() or marker.stat().st_size > 8192:
                    raise ValueError("New archive marker is invalid.")
                with marker.open(encoding="utf-8") as stream:
                    metadata = json.load(stream)
                if type(metadata) is not dict or metadata.get("product") != "KM VMS" or metadata.get("schema_version") != 1:
                    raise ValueError("New archive marker is invalid.")
        result.append({"configured": configured, "source": source, "target": target,
                       "available": True, "identity": identity})
    return result


def docker_command(arguments, *, data=None, timeout=20):
    executable = shutil.which("docker")
    if executable is None:
        for candidate in ("/Volume1/@apps/DockerEngine/dockerd/bin/docker", "/usr/local/bin/docker"):
            if Path(candidate).is_file():
                executable = candidate
                break
    if executable is None:
        raise ArchivePolicyError("Docker is required to validate archive bindings.")
    try:
        result = subprocess.run([executable, *arguments], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, text=True)
    except (OSError, subprocess.TimeoutExpired):
        raise ArchivePolicyError("Archive binding validation is temporarily unavailable.") from None
    if result.returncode or len(result.stdout) > MAX_JSON:
        raise ArchivePolicyError("Archive binding validation failed.")
    return result.stdout.strip()


def ensure_probe_image():
    # A previous installation need not have the new release's exact base image.
    # Provision only this source-owned digest; the isolated probe stays offline.
    inspect_args = ["image", "inspect", "--format", "{{.Id}}", PYTHON_IMAGE]
    try:
        docker_command(inspect_args)
        return
    except ArchivePolicyError:
        pass
    try:
        docker_command(["pull", "--quiet", PYTHON_IMAGE], timeout=PROBE_IMAGE_PULL_TIMEOUT_SECONDS)
        docker_command(inspect_args)
    except ArchivePolicyError:
        raise ArchiveProbeImageError("Required archive validation image is unavailable.") from None


def installed_sources(project_name):
    identifiers = docker_command(["ps", "-aq", "--filter", f"label=com.docker.compose.project={project_name}"])
    trusted = set()
    for identifier in identifiers.splitlines():
        if not re.fullmatch(r"[0-9a-f]{12,64}", identifier):
            raise ArchivePolicyError("Container archive authority is invalid.")
        metadata = json.loads(docker_command(["inspect", "--format", '{{json .Config.Labels}}', identifier]))
        if metadata.get("com.docker.compose.project") != project_name or metadata.get("com.docker.compose.service") not in {"api", "schema-update"}:
            continue
        mounts = json.loads(docker_command(["inspect", "--format", '{{json .Mounts}}', identifier]))
        for mount in mounts:
            if (mount.get("Type") == "bind" and mount.get("RW") is True
                    and (mount.get("Destination") == "/storage/archive" or re.fullmatch(r"/storage/archive-roots/[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", mount.get("Destination", "")))):
                trusted.add(host_path(mount["Source"]))
    return trusted


def installer_paths(app_dir):
    values = {}
    env_file = app_dir / ".env"
    if env_file.is_symlink() or not env_file.is_file() or env_file.stat().st_size > MAX_JSON:
        raise ArchivePolicyError("Installer environment is unsafe.")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key in {"KM_VMS_HOST_APP_DIR", "SURVEILLANCE_ROOT"}:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == "'":
                value = value[1:-1].replace("\\'", "'").replace("\\\\", "\\")
            else:
                if "$" in value or "#" in value:
                    raise ArchivePolicyError("Installer archive path requires literal quoting.")
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    try:
                        value = json.loads(value)
                    except ValueError:
                        raise ArchivePolicyError("Installer archive quoting is invalid.") from None
            if key in values:
                raise ArchivePolicyError("Installer archive path is ambiguous.")
            values[key] = host_path(value)
    host_app = values.get("KM_VMS_HOST_APP_DIR", str(app_dir))
    return host_app, values.get("SURVEILLANCE_ROOT")


def _protected_directory(path, owner):
    for parent in reversed((path, *path.parents)):
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise ArchivePolicyError("Archive authority directory is unsafe.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.geteuid() == 0:
        os.chown(path, *owner)
    path.chmod(0o700)


def _atomic(path, content, mode, owner):
    if (not path.is_symlink() and path.is_file()
            and path.stat().st_size == len(content) and path.stat().st_mode & 0o777 == mode
            and path.read_bytes() == content):
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), *owner)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_evidence(value):
    if (type(value) is not dict or set(value) != {"schema_version", "roots"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["roots"]) is not list or len(value["roots"]) > 129):
        raise ArchivePolicyError("Protected archive mount evidence is invalid.")
    targets = set()
    for item in value["roots"]:
        if type(item) is not dict or set(item) != {"configured", "source", "target", "available", "identity"}:
            raise ArchivePolicyError("Protected archive mount entry is invalid.")
        host_path(item["configured"])
        host_path(item["source"])
        target = item["target"]
        if not isinstance(target, str) or (target != PRIMARY_ARCHIVE and (
                not target.startswith("/storage/archive-roots/") or not ROOT_ID.fullmatch(target.rsplit("/", 1)[-1])
                or target.count("/") != 3)) or target in targets or type(item["available"]) is not bool:
            raise ArchivePolicyError("Protected archive mount target is invalid.")
        targets.add(target)
        identity = item["identity"]
        if identity is None:
            continue
        if (type(identity) is not dict or set(identity) != {"volume", "mountpoint", "mount_root", "inode"}
                or not isinstance(identity["volume"], str) or not re.fullmatch(r"[0-9a-f]{64}", identity["volume"])
                or type(identity["inode"]) is not int or identity["inode"] <= 0):
            raise ArchivePolicyError("Protected archive filesystem evidence is invalid.")
        host_path(identity["mountpoint"])
        host_path(identity["mount_root"])
    return value["roots"]


def mount_evidence(app_dir: Path):
    value = read_json(app_dir / AUTHORITY_DIRECTORY / ARCHIVE_EVIDENCE, missing_ok=True)
    return [] if value is None else _validated_evidence(value)


def render_runtime_plan(app_dir, host_app, evidence, directory, owner):
    offline = sorted(item["target"] for item in evidence if not item["available"])
    devices = {item["target"]: item["identity"]["volume"] for item in evidence if item["identity"]}
    planned = {item["target"]: (item["source"], False) for item in evidence if item["available"]}
    if PRIMARY_ARCHIVE in offline:
        # Compose needs a replacement for the primary target, including on
        # previous immutable slots with an unconditional primary bind. Mount an
        # empty, read-only sentinel; it is never a recording destination.
        unavailable = directory / OFFLINE_PRIMARY
        _protected_directory(unavailable, owner)
        if next(unavailable.iterdir(), None) is not None:
            raise ArchivePolicyError("Unavailable primary archive sentinel is not empty.")
        planned[PRIMARY_ARCHIVE] = (str(Path(host_app) / AUTHORITY_DIRECTORY / OFFLINE_PRIMARY), True)

    def quoted(value):
        return json.dumps(value.replace("$", "$$"), ensure_ascii=True)

    lines = ["# Generated by the privileged KM VMS archive policy.", "services:"] if planned else ["services: {}"]
    for service in ("api", "schema-update", "recorder") if planned else ():
        lines.append(f"  {service}:")
        if service in {"api", "recorder"}:
            lines.extend(["    environment:", f"      KMVMS_OFFLINE_ARCHIVE_MOUNTS: {quoted(json.dumps(offline, separators=(',', ':')))}"])
            if service == "recorder":
                lines.append(f"      KMVMS_ARCHIVE_VOLUME_EVIDENCE: {quoted(json.dumps(devices, sort_keys=True, separators=(',', ':')))}")
        lines.append("    volumes:")
        for target, (source, read_only) in sorted(planned.items()):
            lines.extend(["      - type: bind", f"        source: {quoted(source)}", f"        target: {quoted(target)}",
                          f"        read_only: {str(read_only).lower()}", "        bind:", "          create_host_path: false"])
    return ("\n".join(lines) + "\n").encode()


def materialize(app_dir: Path, project_name: str):
    if not PROJECT.fullmatch(project_name):
        raise ArchivePolicyError("Archive Compose project is invalid.")
    intent_path = app_dir / "data/install-control/archive-roots-runtime.json"
    value = read_json(intent_path, missing_ok=True)
    host_app, default = installer_paths(app_dir)
    initial_items = parse_intent(value) if value is not None else []
    if not initial_items and (default is None or default == host_app.rstrip("/") + "/data/archive"):
        # Before images/services exist, only the exact installer default needs
        # no extra mount. Deleting intent never exempts an arbitrary primary.
        return None
    directory = app_dir / AUTHORITY_DIRECTORY
    owner_info = app_dir.stat()
    owner = (owner_info.st_uid, owner_info.st_gid)
    _protected_directory(directory, owner)
    descriptor = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ArchivePolicyError("Archive authority lock is unsafe.")
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), *owner)
        fcntl.flock(stream, fcntl.LOCK_EX)
        value = read_json(intent_path, missing_ok=True)
        items = parse_intent(value) if value is not None else []
        host_app, default = installer_paths(app_dir)
        registry = read_json(directory / "approved.json", missing_ok=True)
        if registry is not None and (type(registry) is not dict or set(registry) != {"schema_version", "sources"} or registry["schema_version"] != 1 or type(registry["sources"]) is not list or len(registry["sources"]) > 129):
            raise ArchivePolicyError("Protected archive authority is invalid.")
        trusted = {host_path(path) for path in (registry or {}).get("sources", [])}
        trusted.update(installed_sources(project_name))
        previous = {item["configured"]: item for item in mount_evidence(app_dir)}
        probe_items = [
            {**item, "installer": bool(default and item["source"] == default)}
            for item in items
        ]
        if default:
            probe_items.append({"source": default, "target": PRIMARY_ARCHIVE, "installer": True})
        docker_root = host_path(docker_command(["info", "--format", "{{.DockerRootDir}}"] ))
        probe = inspect.getsource(_host_probe) + "\nimport json,sys\ntry:\n print(json.dumps(_host_probe(json.loads(sys.stdin.buffer.read(262145)))))\nexcept Exception:\n sys.exit(2)\n"
        payload = {"app_dir": host_app, "docker_root": docker_root, "trusted": sorted(trusted),
                   "items": probe_items, "previous": previous}
        if not Path("/.dockerenv").exists() and not Path("/run/.containerenv").exists():
            # The canonical installer already requires host Python. Do not add
            # a base-image provisioning dependency before its first build.
            approved = _host_probe(payload, host_root="/")
        else:
            ensure_probe_image()
            output = docker_command(["run", "--rm", "--pull=never", "--network", "none", "--read-only", "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH", "--security-opt", "no-new-privileges:true", "--memory", "64m", "--pids-limit", "32", "--mount", "type=bind,source=/,target=/host,readonly,bind-propagation=rslave", "-i", PYTHON_IMAGE, "python3", "-B", "-c", probe], data=json.dumps(payload), timeout=30)
            approved = json.loads(output)
        evidence_document = {"schema_version": 1, "roots": approved}
        evidence = _validated_evidence(evidence_document)
        requested = {item["target"]: item["source"] for item in probe_items}
        if len(evidence) > len(probe_items) or any(requested.get(item["target"]) != item["configured"] for item in evidence):
            raise ArchivePolicyError("Host archive validation returned invalid evidence.")
        if read_json(intent_path, missing_ok=True) != value or installer_paths(app_dir) != (host_app, default):
            raise ArchivePolicyError("Archive intent changed during validation; retry the operation.")
        content = render_runtime_plan(app_dir, host_app, evidence, directory, owner)
        # Do not retain an ever-growing history of old archive approvals.
        registry_bytes = (json.dumps({"schema_version": 1, "sources": sorted({item["source"] for item in evidence
                          if item["available"] or item["configured"] in previous or item["source"] in trusted})}, sort_keys=True) + "\n").encode()
        _atomic(directory / ARCHIVE_EVIDENCE, (json.dumps(evidence_document, sort_keys=True) + "\n").encode(), 0o600, owner)
        _atomic(directory / "approved.json", registry_bytes, 0o600, owner)
        # One deterministic protected file; no archive/configuration history tail.
        target = directory / "docker-compose.archive-roots.yml"
        _atomic(target, content, 0o400, owner)
        return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    args = parser.parse_args()
    try:
        path = materialize(args.app_dir, args.project_name)
        if path:
            print(path)
        return 0
    except ArchiveProbeImageError as exc:
        print(f"ERROR [{exc.code}]: {exc} Check Docker registry connectivity and retry.", file=sys.stderr)
        return 1
    except (ArchivePolicyError, OSError, ValueError, TypeError, KeyError):
        print("Archive bindings could not be validated safely; no Compose input was accepted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
