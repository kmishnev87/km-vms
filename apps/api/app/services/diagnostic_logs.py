"""Request narrow, read-only log collection by the existing privileged helper."""
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from app.core.config import settings
from app.services.maintenance_admission import write_bounded_json_atomic

SERVICES = ("api", "recorder", "web", "nginx", "postgres", "redis")


def collect_diagnostic_logs(mode: str) -> dict:
    root = Path(settings.update_control_root) / "diagnostic-logs"
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / "collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {}
        request_id = uuid.uuid4().hex
        request_path = root / "request.json"
        write_bounded_json_atomic(request_path, {"request_id": request_id, "mode": mode, "created_at": time.time()})
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                try:
                    descriptor = os.open(root / "response.json", os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(descriptor, "rb") as handle:
                        raw = handle.read(256*1024+1)
                    if len(raw) <= 256*1024:
                        response = json.loads(raw)
                        if response.get("request_id") == request_id and isinstance(response.get("services"), dict):
                            return {service: response["services"][service] for service in SERVICES if isinstance(response["services"].get(service), dict)}
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            return {}
        finally:
            request_path.unlink(missing_ok=True)
            (root / "response.json").unlink(missing_ok=True)
