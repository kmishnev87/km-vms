from datetime import datetime
from pathlib import Path
import subprocess

import pytest

from app.services.storage import slugify_camera_name, build_unique_folder_name, ensure_camera_folder
from app.core.config import settings
from test_self_recovery_stage1374 import worker
from test_storage_recording_contract import db, add_camera


@pytest.mark.parametrize('name', ['%Y Entrance', '😀' * 60, '😀' * 80, 'К' * 120, 'Null\x00Camera', '[Entrance]'])
def test_valid_display_name_has_safe_filename_and_real_ffmpeg_discovers_it(worker, monkeypatch, tmp_path, name):
    safe = worker.safe_name(name)
    assert '%' not in safe and '\x00' not in safe
    assert len(safe.encode('utf-8')) <= 231
    assert slugify_camera_name(name) == safe
    monkeypatch.setattr(settings, 'storage_root', str(tmp_path))
    folder = Path(ensure_camera_folder(safe))
    assert folder.is_dir()
    job = worker.RecordingJob(1, name, safe, 'always', True)
    job.db_job_id = 'safe-filename-proof'
    job.archive_root_path = tmp_path
    pattern = worker.build_segment_pattern(1, name, job.db_job_id, 'mkv', root=tmp_path)
    job.current_output_path = pattern
    # A real tiny lavfi output: no camera/network/user archive.
    result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-f', 'lavfi', '-i', 'color=size=32x32:rate=5', '-t', '0.2', '-c:v', 'mpeg4',
        '-f', 'segment', '-segment_time', '60', '-strftime', '1', '-segment_format', 'matroska', pattern],
        capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    files = worker.discover_job_segments(job)
    assert len(files) == 1 and files[0].stat().st_size > 0
    assert len(files[0].name.encode('utf-8')) <= 255
    assert worker.capture_segment_baseline(pattern, 1, name, root=tmp_path)


@pytest.mark.parametrize(('name', 'expected'), [('Dahua 5MP', 'Dahua_5MP'), ('Камера у входа', 'Камера_у_входа'), ('PTZ', 'PTZ'), ('Panorama', 'Panorama')])
def test_ordinary_names_keep_existing_normalization(worker, name, expected):
    assert worker.safe_name(name) == slugify_camera_name(name) == expected


def test_long_name_folder_collision_is_bounded_and_existing_folder_is_not_renamed(db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'storage_root', str(tmp_path))
    name = '😀' * 80
    first = build_unique_folder_name(db, name)
    existing = Path(ensure_camera_folder(first))
    marker = existing / 'existing.mkv'
    marker.write_bytes(b'existing archive unchanged')
    row = add_camera(db, name='Stored display name')
    row.storage_folder_name = first
    db.commit()
    second = build_unique_folder_name(db, name)
    assert second == first + '_2'
    assert len(second.encode('utf-8')) <= 255
    assert row.name == 'Stored display name'
    assert marker.read_bytes() == b'existing archive unchanged'
