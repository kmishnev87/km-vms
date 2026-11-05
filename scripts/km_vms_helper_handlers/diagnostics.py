"""Read-only, bounded logs of this Compose installation; no command dispatcher."""
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time

SERVICES = ("api", "recorder", "web", "nginx", "postgres", "redis")
MAX_LOG_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 256 * 1024


def _bounded_command(command, *, limit, deadline):
    output = bytearray()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(min(0.1, max(0, deadline-time.monotonic()))):
                    continue
                chunk = os.read(process.stdout.fileno(), min(4096, limit+1-len(output)))
                if not chunk:
                    return output.decode("utf-8", errors="replace"), process.wait(timeout=1), False
                output.extend(chunk)
                if len(output) > limit:
                    break
        return output[:limit].decode("utf-8", errors="replace"), None, True
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        process.stdout.close()


def collect_logs(project, mode):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project or "") or mode not in {"normal", "extended"}:
        raise ValueError("invalid_log_scope")
    deadline = time.monotonic() + 5
    listing, code, truncated = _bounded_command([
        "docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
        "--filter", "label=com.docker.compose.oneoff=False", "--format",
        '{{.ID}}\t{{.Label "com.docker.compose.service"}}',
    ], limit=16*1024, deadline=deadline)
    if code != 0 or truncated:
        raise RuntimeError("docker_inventory_unavailable")
    containers = {}
    for line in listing.splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and re.fullmatch(r"[a-f0-9]{12,64}", fields[0]) and fields[1] in SERVICES:
            containers.setdefault(fields[1], fields[0])
    result = {}
    for service in SERVICES:
        container = containers.get(service)
        if not container or time.monotonic() >= deadline:
            result[service] = {"status": "unavailable", "text": "Service logs unavailable\n"}
            continue
        text, code, truncated = _bounded_command([
            "docker", "logs", "--since", "30m" if mode == "extended" else "10m",
            "--tail", "200", container,
        ], limit=MAX_LOG_BYTES, deadline=deadline)
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
        result[service] = {"status": "partial" if truncated else "available" if code == 0 else "unavailable",
                           "text": text, "truncated": truncated}
    return result


def service_log_request(control_root: Path, project: str):
    root = control_root / "diagnostic-logs"
    request_path = root / "request.json"
    if not request_path.exists() or request_path.is_symlink():
        return
    if request_path.stat().st_size > 1024:
        return
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if set(request) != {"request_id", "mode", "created_at"} or not re.fullmatch(r"[a-f0-9]{32}", str(request.get("request_id"))):
        return
    if not -10 <= time.time() - float(request["created_at"]) <= 30:
        return
    try:
        previous = root / "response.json"
        if previous.is_file() and not previous.is_symlink() and previous.stat().st_size <= MAX_RESPONSE_BYTES:
            if json.loads(previous.read_text(encoding="utf-8")).get("request_id") == request["request_id"]:
                return
    except (OSError, ValueError):
        pass
    try:
        logs = collect_logs(project, request["mode"])
    except Exception:
        logs = {}
    response = {"request_id": request["request_id"], "services": logs}
    rendered = json.dumps(response, ensure_ascii=False).encode("utf-8")
    if len(rendered) > MAX_RESPONSE_BYTES:
        raise ValueError("log_response_too_large")
    descriptor, temporary = tempfile.mkstemp(prefix=".response-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
        os.replace(temporary, root / "response.json")
        # The requesting API removes its own request after reading this reply.
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
