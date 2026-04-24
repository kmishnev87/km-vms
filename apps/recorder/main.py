import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/storage/archive"))
FFMPEG_LOGLEVEL = os.getenv("FFMPEG_LOGLEVEL", "warning")
DEFAULT_RECORD_STREAM = os.getenv("DEFAULT_RECORD_STREAM", "main").lower()

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

processes = {}
restart_state = {}
running = True


def safe_name(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r'[\\\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value[:120].strip("_") or "camera"


def camera_archive_dir(folder_name: str) -> Path:
    path = STORAGE_ROOT / folder_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_segment_dir(folder_name: str) -> Path:
    now = datetime.now()
    path = camera_archive_dir(folder_name) / now.strftime("%Y-%m-%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_segment_pattern(camera_name: str, folder_name: str) -> str:
    dir_path = current_segment_dir(folder_name)
    safe_camera = safe_name(camera_name)
    return str(dir_path / f"{safe_camera}-%Y-%m-%d-%H-%M-%S.mp4")


def choose_input_url(row) -> str | None:
    preferred = (getattr(row, "default_record_stream", None) or DEFAULT_RECORD_STREAM).lower()
    if preferred == "sub":
        primary = row.rtsp_sub_url or row.rtsp_main_url
    else:
        primary = row.rtsp_main_url or row.rtsp_sub_url

    protocol = (row.protocol or "").lower()
    if protocol in {"rtsp", "onvif"}:
        return primary
    return None


def ffmpeg_cmd(camera):
    input_url = choose_input_url(camera)
    if not input_url:
        return None

    segment_minutes = int(camera.segment_minutes or 5)
    segment_seconds = max(segment_minutes, 1) * 60
    segment_pattern = build_segment_pattern(camera.name, camera.storage_folder_name)

    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel", FFMPEG_LOGLEVEL,
        "-rtsp_transport", (camera.rtsp_transport or "tcp"),
        "-i", input_url,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "copy",
        "-c:a", "aac",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        "-strftime", "1",
        "-segment_format", "mp4",
        "-movflags", "+faststart",
        segment_pattern,
    ]


def mark_camera_status(camera_id: int, status: str, last_error: str | None = None):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE cameras
                SET status = :status,
                    last_error = :last_error,
                    updated_at = NOW()
                WHERE id = :camera_id
                """
            ),
            {
                "camera_id": camera_id,
                "status": status,
                "last_error": last_error,
            },
        )


def start_camera(camera):
    if camera.id in processes:
        return

    restart_info = restart_state.get(camera.id)
    if restart_info and restart_info.get("blocked_until", 0) > time.time():
        return

    cmd = ffmpeg_cmd(camera)
    if not cmd:
        mark_camera_status(camera.id, "error", "Не задан URL потока для записи")
        return

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    processes[camera.id] = {
        "proc": proc,
        "camera_name": camera.name,
        "folder_name": camera.storage_folder_name,
        "started_at": time.time(),
    }
    restart_state.pop(camera.id, None)
    mark_camera_status(camera.id, "recording", None)
    print(f"[RECORDER] START {camera.id} {camera.name}")


def stop_camera(camera_id: int, reason: str = "stopped"):
    info = processes.get(camera_id)
    if not info:
        return

    proc = info["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    processes.pop(camera_id, None)
    mark_camera_status(camera_id, reason, None)
    print(f"[RECORDER] STOP {camera_id} reason={reason}")


def enforce_retention(camera_id: int, folder_name: str, retention_days: int, storage_quota_gb: int):
    root = camera_archive_dir(folder_name)
    if not root.exists():
        return

    files = sorted([p for p in root.rglob("*.mp4") if p.is_file()], key=lambda p: p.stat().st_mtime)

    if retention_days and retention_days > 0:
        cutoff = time.time() - (retention_days * 86400)
        for file_path in list(files):
            try:
                if file_path.stat().st_mtime < cutoff:
                    file_path.unlink(missing_ok=True)
            except Exception:
                pass
        files = sorted([p for p in root.rglob("*.mp4") if p.is_file()], key=lambda p: p.stat().st_mtime)

    quota_bytes = max(int(storage_quota_gb), 50) * 1024 * 1024 * 1024
    total = 0
    for file_path in files:
        try:
            total += file_path.stat().st_size
        except Exception:
            pass

    while total > quota_bytes and files:
        oldest = files.pop(0)
        try:
            size = oldest.stat().st_size
            oldest.unlink(missing_ok=True)
            total -= size
        except Exception:
            pass


def sync_cameras():
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT
                    id,
                    name,
                    storage_folder_name,
                    enabled,
                    protocol,
                    host,
                    port,
                    rtsp_main_url,
                    rtsp_sub_url,
                    rtsp_transport,
                    recording_mode,
                    default_record_stream,
                    segment_minutes,
                    retention_days,
                    storage_quota_gb
                FROM cameras
                ORDER BY id ASC
                """
            )
        ).fetchall()

        active_ids = set()

        for row in rows:
            enforce_retention(
                row.id,
                row.storage_folder_name,
                int(row.retention_days or 30),
                int(row.storage_quota_gb or 50),
            )

            if not row.enabled:
                restart_state.pop(row.id, None)
                if row.id in processes:
                    stop_camera(row.id, "disabled")
                continue

            if row.recording_mode != "always":
                restart_state.pop(row.id, None)
                if row.id in processes:
                    stop_camera(row.id, "idle")
                continue

            active_ids.add(row.id)

            if row.id not in processes:
                start_camera(row)

        current_ids = set(processes.keys())
        for camera_id in current_ids - active_ids:
            restart_state.pop(camera_id, None)
            stop_camera(camera_id, "removed_or_disabled")

    finally:
        db.close()


def check_children():
    for camera_id, info in list(processes.items()):
        proc = info["proc"]
        if proc.poll() is not None:
            err = ""
            try:
                err = proc.stderr.read()[-2000:]
            except Exception:
                pass

            processes.pop(camera_id, None)
            attempt = int(restart_state.get(camera_id, {}).get("attempt", 0)) + 1
            backoff = min(30, 10 * attempt)
            restart_state[camera_id] = {
                "attempt": attempt,
                "blocked_until": time.time() + backoff,
            }
            mark_camera_status(camera_id, "error", err or "ffmpeg stopped unexpectedly")
            print(f"[RECORDER] EXIT {camera_id} backoff={backoff}s")


def shutdown_handler(signum, frame):
    global running
    running = False
    print("[RECORDER] shutdown signal received")


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

print("[RECORDER] service started")

while running:
    try:
        sync_cameras()
        check_children()
    except Exception as exc:
        print(f"[RECORDER] loop error: {exc}")
    time.sleep(5)

for camera_id in list(processes.keys()):
    stop_camera(camera_id, "shutdown")

print("[RECORDER] stopped")
