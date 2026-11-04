"""Shared immutable recorder contracts; no database or process ownership."""

import hashlib
import json

OWNERSHIP_KM_VMS = "KM VMS"
METADATA_SOURCE_RECORDER = "recorder"
SEGMENT_STATUS_WRITING = "writing"
SEGMENT_STATUS_FINALIZED = "finalized"
SEGMENT_STATUS_FAILED = "failed"
SEGMENT_STATUS_STALE_WRITING = "stale_writing"
STORAGE_NAMESPACE = "kmvms/recordings"
DEFAULT_ARCHIVE_ROOT_ID = "default"
RECORDING_FORMATS = {"mkv", "mp4"}
DEFAULT_RECORDING_FORMAT = "mkv"
FORMAT_METADATA = {
    "mkv": {"container_format": "mkv", "file_extension": ".mkv", "mime_type": "video/x-matroska", "segment_format": "matroska"},
    "mp4": {"container_format": "mp4", "file_extension": ".mp4", "mime_type": "video/mp4", "segment_format": "mp4"},
}
RETENTION_SIGNAL_TYPE = "retention_evaluate"
RETENTION_SIGNAL_SCOPE = {
    "camera_ids": [],
    "global": True,
    "physical_volume_ids": [],
    "root_ids": [],
    "scope_escalated": False,
    "segment_ids": [],
}
RETENTION_SIGNAL_SCOPE_JSON = json.dumps(
    RETENTION_SIGNAL_SCOPE,
    sort_keys=True,
    ensure_ascii=True,
    separators=(",", ":"),
)
RETENTION_SIGNAL_SCOPE_KEY = (
    "scope:"
    + hashlib.sha256(RETENTION_SIGNAL_SCOPE_JSON.encode("utf-8")).hexdigest()[:32]
)
RECORDER_RECEIPT_CONTRACT_VERSION = 1
RECEIPT_FINGERPRINT_CHUNK_BYTES = 64 * 1024
SEGMENT_METADATA_BATCH_SIZE = 64
SEGMENT_DISCOVERY_REFRESH_SECONDS = 60


class RecorderState:
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    STOPPED = "stopped"
    RESTARTING = "restarting"
    ERROR = "error"
    DISABLED = "disabled"


UNSET = object()
ERROR_CLEARING_STATES = {
    RecorderState.STARTING,
    RecorderState.RECORDING,
    RecorderState.STOPPING,
    RecorderState.STOPPED,
    RecorderState.IDLE,
    RecorderState.DISABLED,
}
EXIT_CODE_CLEARING_STATES = {
    RecorderState.STARTING,
    RecorderState.RECORDING,
    RecorderState.STOPPED,
    RecorderState.IDLE,
    RecorderState.DISABLED,
}
