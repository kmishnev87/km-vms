"""Read the API's bounded, private, exact-lens motion fact."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Sync with app.services.ai_camera_view; API and Recorder build separately.
_KEYS = ("id", "protocol", "host", "port", "onvif_path", "onvif_channel_id",
         "onvif_profile_token", "onvif_sub_profile_token", "rtsp_main_url", "rtsp_sub_url")


def view_kind(row, root):
    protocol = getattr(row, "protocol", None)
    if protocol == "rtsp":
        return "fixed"
    if protocol != "onvif" or not root:
        return "unknown"
    try:
        identifier = int(row.id)
        if identifier <= 0:
            return "unknown"
        path = Path(root) / "view-facts" / f"{identifier}.json"
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            return "unknown"
        with path.open("rb") as source:
            data = source.read(513)
        if len(data) > 512:
            return "unknown"
        fact = json.loads(data)
        values = [str(getattr(row, key, None) or "") for key in _KEYS]
        bound = hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
        if fact.get("v") == 1 and fact.get("binding") == bound and fact.get("kind") in ("fixed", "ptz"):
            return fact["kind"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return "unknown"
