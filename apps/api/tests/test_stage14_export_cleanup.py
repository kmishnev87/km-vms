from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import settings
from app.models.recording import ArchiveExportJob
from app.services.archive_exports import cleanup_archive_export_artifacts, EXPORT_OUTPUT_DIR
from test_archive_export_stage11 import api_db, add_user


def test_cleanup_progresses_beyond_first_hundred_and_rotates_failures(api_db, monkeypatch):
    db, root = api_db
    actor = add_user(db)
    expired = datetime.utcnow() - timedelta(days=2)
    paths = []
    for index in range(205):
        path = Path(settings.storage_exports) / EXPORT_OUTPUT_DIR / f"stage14-{index}.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"clip")
        paths.append(path)
        db.add(ArchiveExportJob(id=f"stage14-{index:03}", camera_label_snapshot="Camera", start_ts=expired,
                               end_ts=expired + timedelta(seconds=2), status="expired", expires_at=expired,
                               created_at=expired, updated_at=expired,
                               internal_output_path=f"{EXPORT_OUTPUT_DIR}/stage14-{index}.mkv"))
    # An unsafe path must remain protected without starving valid later rows.
    db.add(ArchiveExportJob(id="stage14-unsafe", camera_label_snapshot="Camera", start_ts=expired,
                           end_ts=expired + timedelta(seconds=2), status="expired", expires_at=expired,
                           created_at=expired, updated_at=expired - timedelta(days=1), internal_output_path="../outside.mkv"))
    db.commit()
    for _ in range(4):
        result = cleanup_archive_export_artifacts(db, actor=actor, limit=100)
        assert result["jobs_considered"] <= 100
    assert all(not path.exists() for path in paths)
    assert db.get(ArchiveExportJob, "stage14-unsafe").internal_output_path == "../outside.mkv"
    assert db.query(ArchiveExportJob).count() == 206
